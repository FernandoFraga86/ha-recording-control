#!/usr/bin/env python3
"""Reconstruct and verify the exact experimental Frigate distribution source."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import NoReturn


FORBIDDEN_ASSET_NAMES = {
    "apple-touch-icon.png",
    "android-chrome-192x192.png",
    "android-chrome-512x512.png",
    "favicon-16x16.png",
    "favicon-32x32.png",
    "favicon.ico",
    "favicon.png",
    "favicon.svg",
    "maskable-badge.png",
    "maskable-icon.png",
    "mstile-150x150.png",
}
FORBIDDEN_BUILD_REFERENCES = (
    "/images/branding/",
    "favicon-16x16.png",
    "favicon-32x32.png",
    "android-chrome-192x192.png",
    "android-chrome-512x512.png",
    "maskable-badge.png",
    "maskable-icon.png",
)


def fail(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        fail(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def lf_sha256(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def changed_paths(source: Path) -> set[str]:
    tracked = run(
        ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB", "HEAD"],
        cwd=source,
    )
    untracked = run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=source,
    )
    return {line for line in f"{tracked}\n{untracked}".splitlines() if line}


def assert_paths(source: Path, expected: set[str], label: str) -> None:
    actual = changed_paths(source)
    if actual != expected:
        fail(
            f"{label} path set mismatch; unexpected={sorted(actual - expected)}, "
            f"missing={sorted(expected - actual)}"
        )


def check_required_files(root: Path, paths: list[str]) -> None:
    for relative in paths:
        candidate = root / relative
        if not candidate.is_file() or candidate.stat().st_size == 0:
            fail(f"required attribution/license file missing or empty: {relative}")


def prepare(args: argparse.Namespace) -> None:
    distribution = args.distribution.resolve()
    source = args.upstream.resolve()
    lock_path = distribution / "candidate-verification.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    dist_head = run(["git", "rev-parse", "HEAD"], cwd=distribution)
    if args.expected_distribution_commit and dist_head != args.expected_distribution_commit:
        fail(
            "distribution checkout does not match workflow commit: "
            f"{dist_head} != {args.expected_distribution_commit}"
        )

    upstream_head = run(["git", "rev-parse", "HEAD"], cwd=source)
    expected_upstream = lock["upstream"]["commit"]
    if upstream_head != expected_upstream:
        fail(f"upstream commit mismatch: {upstream_head} != {expected_upstream}")
    if run(["git", "status", "--porcelain"], cwd=source):
        fail("upstream checkout is not pristine before patch application")

    overlay = distribution / lock["patches"]["source_overlay"]["path"]
    branding = distribution / lock["patches"]["distribution_branding"]["path"]
    for patch_path, patch_key in (
        (overlay, "source_overlay"),
        (branding, "distribution_branding"),
    ):
        expected_hash = lock["patches"][patch_key]["sha256"]
        actual_hash = sha256(patch_path)
        if actual_hash != expected_hash:
            fail(f"{patch_key} patch SHA-256 mismatch: {actual_hash}")

    run(["git", "apply", "--check", str(overlay)], cwd=source)
    run(["git", "apply", str(overlay)], cwd=source)
    overlay_paths = set(lock["overlay_paths"])
    assert_paths(source, overlay_paths, "source overlay")

    run(["git", "apply", "--check", str(branding)], cwd=source)
    run(["git", "apply", str(branding)], cwd=source)
    branding_paths = set(lock["branding_files_sha256_lf"]) | set(
        lock["removed_visual_assets"]
    )
    assert_paths(source, overlay_paths | branding_paths, "combined distribution")
    run(["git", "diff", "--check", "HEAD"], cwd=source)

    for relative, expected_hash in lock["runtime_sha256_lf"].items():
        path = source / relative
        actual_hash = lf_sha256(path)
        if actual_hash != expected_hash:
            fail(f"runtime source hash mismatch for {relative}: {actual_hash}")

    for relative, expected_hash in lock["branding_files_sha256_lf"].items():
        path = source / relative
        actual_hash = lf_sha256(path)
        if actual_hash != expected_hash:
            fail(f"branding source hash mismatch for {relative}: {actual_hash}")

    for relative in lock["removed_visual_assets"]:
        if (source / relative).exists():
            fail(f"protected upstream visual asset remains: {relative}")
    check_required_files(source, lock["required_attribution_files"])

    for relative in ("web/index.html", "web/login.html"):
        text = (source / relative).read_text(encoding="utf-8")
        if "<title>HA Recording Control</title>" not in text:
            fail(f"neutral product title missing from {relative}")
        if "/images/ha-recording-control.svg" not in text:
            fail(f"neutral favicon missing from {relative}")

    manifest = json.loads((source / "web/site.webmanifest").read_text(encoding="utf-8"))
    if manifest.get("name") != lock["ui_identity"]:
        fail("web manifest identity does not match the neutral product identity")
    if manifest.get("short_name") != lock["ui_identity"]:
        fail("web manifest short_name does not match the neutral product identity")
    if manifest.get("icons") != [
        {
            "src": "/BASE_PATH/images/ha-recording-control.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any",
        }
    ]:
        fail("web manifest icon declaration is unexpected")

    worker = (source / "web/public/notifications-worker.js").read_text(encoding="utf-8")
    if worker.count("/images/ha-recording-control.svg") != 2:
        fail("notification icon and badge must use the neutral original asset")
    for name in FORBIDDEN_BUILD_REFERENCES:
        if name in worker:
            fail(f"notification worker references a removed brand asset: {name}")

    print(
        json.dumps(
            {
                "status": "PASS",
                "distribution_commit": dist_head,
                "upstream_commit": upstream_head,
                "overlay_files": len(overlay_paths),
                "runtime_hashes": len(lock["runtime_sha256_lf"]),
                "branding_hashes": len(lock["branding_files_sha256_lf"]),
                "removed_visual_assets": len(lock["removed_visual_assets"]),
                "license_files": lock["required_attribution_files"],
                "image_tag": lock["candidate_version"],
            },
            indent=2,
        )
    )


def verify_built_web(args: argparse.Namespace) -> None:
    distribution = args.distribution.resolve()
    lock = json.loads(
        (distribution / "candidate-verification.json").read_text(encoding="utf-8")
    )
    web = args.built_web.resolve()
    required = (
        "index.html",
        "login.html",
        "site.webmanifest",
        "notifications-worker.js",
        "images/ha-recording-control.svg",
    )
    for relative in required:
        path = web / relative
        if not path.is_file() or path.stat().st_size == 0:
            fail(f"built UI file missing or empty: {relative}")

    for relative in ("index.html", "login.html"):
        text = (web / relative).read_text(encoding="utf-8")
        if "HA Recording Control" not in text:
            fail(f"built {relative} lost the neutral identity")
        if "ha-recording-control.svg" not in text:
            fail(f"built {relative} lost its neutral favicon reference")

    manifest = json.loads((web / "site.webmanifest").read_text(encoding="utf-8"))
    if manifest.get("name") != lock["ui_identity"]:
        fail("built manifest identity is incorrect")
    icon_path = web / "images/ha-recording-control.svg"
    if not icon_path.read_text(encoding="utf-8").lstrip().startswith("<svg"):
        fail("built neutral icon is not a valid SVG document")

    worker = (web / "notifications-worker.js").read_text(encoding="utf-8")
    if worker.count("/images/ha-recording-control.svg") != 2:
        fail("built notification worker has incorrect neutral icon references")

    for file in web.rglob("*"):
        if not file.is_file():
            continue
        if file.name.lower() in FORBIDDEN_ASSET_NAMES:
            fail(f"upstream visual brand asset present in built image: {file.relative_to(web)}")
        if file.suffix.lower() in {".html", ".js", ".json", ".css", ".webmanifest"}:
            content = file.read_bytes().lower()
            for reference in FORBIDDEN_BUILD_REFERENCES:
                if reference.encode("ascii") in content:
                    fail(
                        "built UI references removed brand asset "
                        f"{reference} in {file.relative_to(web)}"
                    )

    print(
        json.dumps(
            {
                "status": "PASS",
                "built_ui_root": str(web),
                "required_files": list(required),
                "removed_brand_assets_absent": True,
                "manifest_and_notification_branding": "PASS",
            },
            indent=2,
        )
    )


def verify_image_layers(args: argparse.Namespace) -> None:
    distribution = args.distribution.resolve()
    lock = json.loads(
        (distribution / "candidate-verification.json").read_text(encoding="utf-8")
    )
    forbidden_paths = set(lock["removed_visual_assets"])
    forbidden_hashes = set(lock["upstream_brand_asset_sha256"].values())
    image_extensions = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp"}
    layer_count = 0
    image_file_count = 0

    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as image_archive:
            for outer_member in image_archive:
                if not outer_member.isfile() or not outer_member.name.endswith("/layer.tar"):
                    continue
                layer_stream = image_archive.extractfile(outer_member)
                if layer_stream is None:
                    fail(f"could not read image layer {outer_member.name}")
                layer_count += 1
                with tarfile.open(fileobj=layer_stream, mode="r|*") as layer:
                    for member in layer:
                        normalized = member.name.replace("\\", "/").lstrip("./")
                        if any(
                            normalized == forbidden
                            or normalized.endswith("/" + forbidden)
                            for forbidden in forbidden_paths
                        ):
                            fail(f"removed brand asset path exists in image layer: {normalized}")

                        if not member.isfile() or Path(normalized).suffix.lower() not in image_extensions:
                            continue
                        stream = layer.extractfile(member)
                        if stream is None:
                            fail(f"could not read image asset in layer: {normalized}")
                        digest = hashlib.sha256()
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                        image_file_count += 1
                        if digest.hexdigest() in forbidden_hashes:
                            fail(f"upstream protected visual asset bytes found in layer: {normalized}")
    except (tarfile.TarError, OSError) as exc:
        fail(f"Docker save archive could not be completely inspected: {exc}")

    if layer_count == 0:
        fail("Docker save stream contained no image layers")

    print(
        json.dumps(
            {
                "status": "PASS",
                "layers_inspected": layer_count,
                "image_files_hashed": image_file_count,
                "upstream_brand_asset_hashes_checked": len(forbidden_hashes),
                "removed_brand_asset_paths_absent": True,
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--upstream", type=Path, required=True)
    prepare_parser.add_argument("--distribution", type=Path, required=True)
    prepare_parser.add_argument("--expected-distribution-commit")
    prepare_parser.set_defaults(func=prepare)

    ui_parser = subparsers.add_parser("verify-built-web")
    ui_parser.add_argument("--built-web", type=Path, required=True)
    ui_parser.add_argument("--distribution", type=Path, required=True)
    ui_parser.set_defaults(func=verify_built_web)

    layers_parser = subparsers.add_parser("verify-image-layers")
    layers_parser.add_argument("--distribution", type=Path, required=True)
    layers_parser.set_defaults(func=verify_image_layers)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
