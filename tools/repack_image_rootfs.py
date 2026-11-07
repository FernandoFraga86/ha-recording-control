from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import posixpath
import re
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable


REPLACED_ROOTS = (
    "opt/frigate/frigate",
    "opt/frigate/web",
    "opt/frigate/licenses",
)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp"}


def fail(message: str) -> None:
    raise SystemExit(message)


def normalize_tar_path(raw_name: str) -> str:
    name = raw_name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        fail(f"absolute path in rootfs tar: {raw_name!r}")
    parts = PurePosixPath(name).parts
    if ".." in parts:
        fail(f"parent traversal in rootfs tar: {raw_name!r}")
    normalized = posixpath.normpath(name)
    if normalized in ("", "."):
        return "."
    if normalized == ".." or normalized.startswith("../"):
        fail(f"parent traversal in rootfs tar: {raw_name!r}")
    return normalized.rstrip("/")


def is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def is_replaced_path(path: str) -> bool:
    return any(is_within(path, root) for root in REPLACED_ROOTS)


def normalize_forbidden_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalized = []
    for path in paths:
        value = path.replace("\\", "/").strip("/")
        if not value or ".." in PurePosixPath(value).parts:
            fail(f"invalid locked forbidden asset path: {path!r}")
        normalized.append(value)
    return tuple(normalized)


def is_forbidden_asset_path(path: str, forbidden_paths: tuple[str, ...]) -> bool:
    return any(path == value or path.endswith("/" + value) for value in forbidden_paths)


def link_points_to_replaced_path(member: tarfile.TarInfo, name: str) -> bool:
    if member.islnk():
        target = normalize_tar_path(member.linkname)
    elif member.issym():
        raw_target = member.linkname.replace("\\", "/")
        if raw_target.startswith("/"):
            raw_target = raw_target.lstrip("/")
        else:
            raw_target = posixpath.join(posixpath.dirname(name), raw_target)
        target = posixpath.normpath(raw_target)
        if target == ".." or target.startswith("../"):
            fail(f"symlink escapes container root: {name} -> {member.linkname}")
    else:
        return False
    return is_replaced_path(target)


def _drain(stream: BinaryIO | None) -> None:
    if stream is None:
        return
    for _ in iter(lambda: stream.read(1024 * 1024), b""):
        pass


def _copy_hashable_file(
    source: BinaryIO,
    destination: tarfile.TarFile,
    member: tarfile.TarInfo,
    forbidden_hashes: frozenset[str],
) -> tuple[bool, str]:
    digest = hashlib.sha256()
    with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b") as spool:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            spool.write(block)
        actual = digest.hexdigest()
        if actual in forbidden_hashes:
            return False, actual
        spool.seek(0)
        destination.addfile(member, spool)
        return True, actual


def _overlay_entries(root: Path, image_prefix: str) -> list[tuple[Path, str]]:
    root = root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        fail(f"overlay source must be a real directory: {root}")
    entries: list[tuple[Path, str]] = [(root, image_prefix)]
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.sort()
        filenames.sort()
        retained_directories: list[str] = []
        for directory in directories:
            child = current_path / directory
            relative = child.relative_to(root).as_posix()
            image_path = f"{image_prefix}/{relative}"
            if child.is_symlink():
                entries.append((child, image_path))
            else:
                entries.append((child, image_path))
                retained_directories.append(directory)
        directories[:] = retained_directories
        for filename in filenames:
            child = current_path / filename
            relative = child.relative_to(root).as_posix()
            entries.append((child, f"{image_prefix}/{relative}"))
    return entries


def _tar_info_from_path(path: Path, archive_name: str) -> tuple[tarfile.TarInfo, BinaryIO | None]:
    metadata = path.lstat()
    info = tarfile.TarInfo(archive_name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = stat.S_IMODE(metadata.st_mode)
    info.mtime = int(metadata.st_mtime)
    if stat.S_ISLNK(metadata.st_mode):
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
        info.size = 0
        return info, None
    if stat.S_ISDIR(metadata.st_mode):
        info.type = tarfile.DIRTYPE
        info.size = 0
        return info, None
    if stat.S_ISREG(metadata.st_mode):
        info.type = tarfile.REGTYPE
        info.size = metadata.st_size
        return info, path.open("rb")
    fail(f"unsupported filesystem entry in overlay: {path}")


def _append_tree(
    destination: tarfile.TarFile,
    root: Path,
    image_prefix: str,
    forbidden_hashes: frozenset[str],
) -> int:
    count = 0
    for path, archive_name in _overlay_entries(root, image_prefix):
        info, stream = _tar_info_from_path(path, archive_name)
        try:
            if info.isfile() and PurePosixPath(archive_name).suffix.lower() in IMAGE_EXTENSIONS:
                if stream is None:
                    fail(f"image overlay file has no content stream: {path}")
                retained, digest = _copy_hashable_file(stream, destination, info, forbidden_hashes)
                if not retained:
                    fail(f"protected upstream image bytes remain in overlay: {archive_name} ({digest})")
            else:
                destination.addfile(info, stream)
            count += 1
        finally:
            if stream is not None:
                stream.close()
    return count


def repack_rootfs(
    source_stream: BinaryIO,
    output_path: Path,
    *,
    frigate_source: Path,
    built_web: Path,
    attribution_root: Path,
    removed_visual_assets: Iterable[str],
    protected_asset_hashes: Iterable[str],
) -> dict[str, int]:
    forbidden_paths = normalize_forbidden_paths(removed_visual_assets)
    forbidden_hashes = frozenset(protected_asset_hashes)
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in forbidden_hashes):
        fail("invalid SHA-256 in protected asset hash lock")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    input_count = 0
    output_count = 0
    removed_count = 0
    image_count = 0
    try:
        with output_path.open("wb") as compressed_file:
            with gzip.GzipFile(fileobj=compressed_file, mode="wb", compresslevel=1, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as output_tar:
                    with tarfile.open(fileobj=source_stream, mode="r|*") as input_tar:
                        for member in input_tar:
                            input_count += 1
                            name = normalize_tar_path(member.name)
                            member.name = name
                            stream = input_tar.extractfile(member) if member.isfile() else None
                            if is_replaced_path(name) or is_forbidden_asset_path(name, forbidden_paths):
                                _drain(stream)
                                removed_count += 1
                                continue
                            if link_points_to_replaced_path(member, name):
                                fail(f"retained link points into replaced tree: {name} -> {member.linkname}")
                            if member.isfile() and PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS:
                                if stream is None:
                                    fail(f"could not read image asset from rootfs: {name}")
                                retained, digest = _copy_hashable_file(stream, output_tar, member, forbidden_hashes)
                                image_count += 1
                                if not retained:
                                    removed_count += 1
                                else:
                                    output_count += 1
                                stream.close()
                            else:
                                output_tar.addfile(member, stream)
                                output_count += 1
                                if stream is not None:
                                    stream.close()

                    output_count += _append_tree(output_tar, frigate_source, "opt/frigate/frigate", forbidden_hashes)
                    output_count += _append_tree(output_tar, built_web, "opt/frigate/web", forbidden_hashes)

                    license_dir = tarfile.TarInfo("opt/frigate/licenses")
                    license_dir.type = tarfile.DIRTYPE
                    license_dir.mode = 0o755
                    license_dir.uid = license_dir.gid = 0
                    license_dir.uname = license_dir.gname = ""
                    license_dir.size = 0
                    output_tar.addfile(license_dir)
                    output_count += 1

                    attribution = (
                        (attribution_root / "LICENSE", "UPSTREAM-MIT-LICENSE"),
                        (attribution_root / "TRADEMARK.md", "UPSTREAM-TRADEMARK-POLICY.md"),
                        (attribution_root / "web/images/branding/LICENSE", "UPSTREAM-WEB-BRAND-ASSETS-LICENSE"),
                    )
                    for file_path, target_name in attribution:
                        if not file_path.is_file():
                            fail(f"required upstream attribution file is missing: {file_path}")
                        info, stream = _tar_info_from_path(file_path, f"opt/frigate/licenses/{target_name}")
                        try:
                            output_tar.addfile(info, stream)
                            output_count += 1
                        finally:
                            if stream is not None:
                                stream.close()
    except (OSError, tarfile.TarError, EOFError) as exc:
        fail(f"rootfs transformation failed: {type(exc).__name__}: {exc}")
    return {
        "input_members": input_count,
        "output_members": output_count,
        "removed_members": removed_count,
        "image_files_hashed": image_count,
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="Flatten and rebrand a verified Frigate root filesystem")
    parser.add_argument("--frigate-source", type=Path, required=True)
    parser.add_argument("--built-web", type=Path, required=True)
    parser.add_argument("--attribution-root", type=Path, required=True)
    parser.add_argument("--verification-lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(args.verification_lock.read_text(encoding="utf-8"))
    result = repack_rootfs(
        sys.stdin.buffer,
        args.output,
        frigate_source=args.frigate_source,
        built_web=args.built_web,
        attribution_root=args.attribution_root,
        removed_visual_assets=lock["removed_visual_assets"],
        protected_asset_hashes=lock["upstream_brand_asset_sha256"].values(),
    )
    print(json.dumps({"status": "PASS", **result, "output_bytes": args.output.stat().st_size}, indent=2))


if __name__ == "__main__":
    _main()
