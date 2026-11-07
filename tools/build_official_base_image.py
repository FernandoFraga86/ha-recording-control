from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from repack_image_rootfs import repack_rootfs


def fail(message: str) -> None:
    raise SystemExit(message)


def run(command: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=capture,
        encoding="utf-8" if capture else None,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        fail(f"command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result.stdout if capture else ""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_digest(value: str, field: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        fail(f"invalid locked {field}: {value!r}")
    return value


def pinned_upstream_commit(verification: dict[str, Any]) -> str:
    upstream = verification.get("upstream")
    commit = upstream.get("commit") if isinstance(upstream, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        fail("candidate verification lock is missing a valid pinned upstream commit")
    return commit


def pinned_patch_labels(verification: dict[str, Any]) -> dict[str, str]:
    patches = verification.get("patches")
    if not isinstance(patches, dict):
        fail("candidate verification lock is missing its patch manifest")
    label_names = {
        "source_overlay": "io.ha-recording-control.source-overlay-patch-sha256",
        "distribution_branding": "io.ha-recording-control.branding-patch-sha256",
        "distribution_build_source": "io.ha-recording-control.distribution-build-source-patch-sha256",
    }
    labels: dict[str, str] = {}
    for patch_name, label_name in label_names.items():
        entry = patches.get(patch_name)
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            fail(f"candidate verification lock is missing a valid SHA-256 for {patch_name}")
        labels[label_name] = digest
    return labels


def verify_oci_index(raw: bytes, expected_digest: str, expected_child: str) -> dict[str, Any]:
    expected_digest = parse_digest(expected_digest, "OCI index digest")
    expected_child = parse_digest(expected_child, "linux/amd64 manifest digest")
    actual_index = "sha256:" + sha256(raw)
    if actual_index != expected_digest:
        fail(f"official tag index digest changed: {actual_index} != {expected_digest}")
    try:
        index = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"official OCI index is invalid JSON: {exc}")
    descriptors = [
        item
        for item in index.get("manifests", [])
        if item.get("platform", {}).get("os") == "linux"
        and item.get("platform", {}).get("architecture") == "amd64"
        and item.get("platform", {}).get("variant") in (None, "")
    ]
    if len(descriptors) != 1 or descriptors[0].get("digest") != expected_child:
        fail("official version tag does not resolve uniquely to the locked linux/amd64 image")
    return index


def verify_oci_manifest(raw: bytes, expected_digest: str, expected_config: str) -> dict[str, Any]:
    expected_digest = parse_digest(expected_digest, "platform manifest digest")
    expected_config = parse_digest(expected_config, "image config digest")
    actual_manifest = "sha256:" + sha256(raw)
    if actual_manifest != expected_digest:
        fail(f"official linux/amd64 manifest digest changed: {actual_manifest} != {expected_digest}")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"official OCI manifest is invalid JSON: {exc}")
    if manifest.get("config", {}).get("digest") != expected_config:
        fail("official image config digest does not match the pinned config")
    return manifest


def _docker_quote(value: str) -> str:
    if "\n" in value or "\r" in value or "\0" in value:
        fail("Docker image metadata contains an unsupported control character")
    return json.dumps(value, ensure_ascii=False)


def _duration(value: int, key: str) -> str:
    if not isinstance(value, int) or value < 0:
        fail(f"invalid Docker healthcheck duration {key}: {value!r}")
    if value == 0:
        return ""
    if value % 1_000_000_000 == 0:
        return f"{value // 1_000_000_000}s"
    if value % 1_000_000 == 0:
        return f"{value // 1_000_000}ms"
    return f"{value}ns"


def docker_import_changes(config: dict[str, Any], labels: dict[str, str]) -> list[str]:
    if config.get("Volumes"):
        fail("the official image declares volumes; docker export cannot prove their mounted contents")
    if config.get("OnBuild"):
        fail("cannot preserve non-empty image OnBuild metadata through docker import")
    if config.get("Shell"):
        fail("cannot preserve a custom image Shell through docker import")
    if config.get("ArgsEscaped"):
        fail("cannot preserve ArgsEscaped metadata through docker import")
    for key in ("AttachStdin", "AttachStdout", "AttachStderr", "Tty", "OpenStdin", "StdinOnce"):
        if config.get(key) not in (None, False):
            fail(f"unsupported non-default Docker image setting: {key}")
    for key in ("Hostname", "Domainname", "MacAddress"):
        if config.get(key) not in (None, ""):
            fail(f"unsupported non-empty Docker image setting: {key}")
    if config.get("NetworkDisabled") not in (None, False):
        fail("unsupported NetworkDisabled image setting")

    instructions: list[str] = []
    for entry in config.get("Env") or []:
        if "=" not in entry:
            fail(f"invalid Docker ENV entry: {entry!r}")
        key, value = entry.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            fail(f"invalid environment variable name in base image: {key!r}")
        instructions.append(f"ENV {key}={_docker_quote(value)}")

    entrypoint = config.get("Entrypoint")
    if entrypoint:
        instructions.append(f"ENTRYPOINT {json.dumps(entrypoint, ensure_ascii=False)}")
    command = config.get("Cmd")
    if command:
        instructions.append(f"CMD {json.dumps(command, ensure_ascii=False)}")

    for port in sorted((config.get("ExposedPorts") or {}).keys()):
        if not re.fullmatch(r"[0-9]+/(tcp|udp|sctp)", port):
            fail(f"invalid exposed port in base image config: {port!r}")
        instructions.append(f"EXPOSE {port}")

    working_dir = config.get("WorkingDir") or ""
    if working_dir:
        instructions.append(f"WORKDIR {_docker_quote(working_dir)}")
    user = config.get("User") or ""
    if user:
        instructions.append(f"USER {_docker_quote(user)}")
    stop_signal = config.get("StopSignal") or ""
    if stop_signal:
        instructions.append(f"STOPSIGNAL {stop_signal}")

    health = config.get("Healthcheck")
    if health:
        test = health.get("Test") or []
        if test == ["NONE"]:
            instructions.append("HEALTHCHECK NONE")
        elif test:
            options = []
            for field, flag in (
                ("Interval", "--interval"),
                ("Timeout", "--timeout"),
                ("StartPeriod", "--start-period"),
                ("StartInterval", "--start-interval"),
            ):
                value = health.get(field) or 0
                duration = _duration(value, field)
                if duration:
                    options.append(f"{flag}={duration}")
            retries = health.get("Retries") or 0
            if retries:
                options.append(f"--retries={retries}")
            prefix = "HEALTHCHECK " + " ".join(options) + " CMD"
            if test[0] == "CMD-SHELL" and len(test) == 2:
                instructions.append(f"{prefix} {test[1]}")
            elif test[0] == "CMD" and len(test) >= 2:
                instructions.append(f"{prefix} {json.dumps(test[1:], ensure_ascii=False)}")
            else:
                fail(f"unsupported Docker healthcheck form: {test!r}")
        else:
            instructions.append("HEALTHCHECK NONE")
    else:
        instructions.append("HEALTHCHECK NONE")

    merged_labels = dict(config.get("Labels") or {})
    for key, value in labels.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key):
            fail(f"invalid OCI label name: {key!r}")
        merged_labels[key] = value
    for key, value in sorted(merged_labels.items()):
        instructions.append(f"LABEL {key}={_docker_quote(str(value))}")
    return instructions


def _canonical_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "Env": config.get("Env") or [],
        "Entrypoint": config.get("Entrypoint") or [],
        "Cmd": config.get("Cmd") or [],
        "WorkingDir": config.get("WorkingDir") or "",
        "User": config.get("User") or "",
        "ExposedPorts": config.get("ExposedPorts") or {},
        "Volumes": config.get("Volumes") or {},
        "Healthcheck": config.get("Healthcheck") or None,
        "StopSignal": config.get("StopSignal") or "",
    }


def verify_python_tree_matches_commit(upstream: Path, copied_source: Path) -> int:
    archive = subprocess.run(
        ["git", "-C", str(upstream), "archive", "--format=tar", "HEAD", "frigate"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if archive.returncode:
        fail("could not read pristine pinned source tree from the upstream git commit")
    expected: dict[str, bytes] = {}
    with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as source_tar:
        for member in source_tar:
            name = member.name.replace("\\", "/")
            if not member.isfile() or not name.startswith("frigate/") or not name.endswith(".py"):
                continue
            stream = source_tar.extractfile(member)
            if stream is None:
                fail(f"cannot read pinned upstream source file: {name}")
            expected[name.removeprefix("frigate/")] = stream.read()
    actual_paths = {path.relative_to(copied_source).as_posix() for path in copied_source.rglob("*.py")}
    if actual_paths != set(expected):
        missing = sorted(set(expected) - actual_paths)[:10]
        extra = sorted(actual_paths - set(expected))[:10]
        fail(f"official image Python source tree differs from pinned commit; missing={missing}, extra={extra}")
    for relative, expected_bytes in expected.items():
        actual_bytes = (copied_source / relative).read_bytes()
        if actual_bytes != expected_bytes:
            fail(f"official image source does not match pinned upstream commit: frigate/{relative}")
    return len(expected)


def _inspect_image(reference: str) -> dict[str, Any]:
    data = json.loads(run(["docker", "image", "inspect", reference], capture=True))
    if len(data) != 1:
        fail("Docker image inspect returned an unexpected number of images")
    return data[0]


def build_image(args: argparse.Namespace) -> None:
    distribution = args.distribution.resolve(strict=True)
    upstream = args.upstream.resolve(strict=True)
    built_web = args.built_web.resolve(strict=True)
    temp_root = args.temp_root.resolve(strict=True)
    verification = json.loads((distribution / "candidate-verification.json").read_text(encoding="utf-8"))
    upstream_commit = pinned_upstream_commit(verification)
    base = verification["official_image_base"]
    tag_ref = base["tag_reference"]
    platform_ref = base["platform_reference"]
    expected_index = base["index_digest"]
    expected_manifest = base["linux_amd64_manifest_digest"]
    expected_config = base["config_digest"]

    index_bytes = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", tag_ref],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if index_bytes.returncode:
        fail(f"cannot verify the official image tag manifest: {index_bytes.stderr.decode(errors='replace')}")
    verify_oci_index(index_bytes.stdout, expected_index, expected_manifest)

    manifest_bytes = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", platform_ref],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if manifest_bytes.returncode:
        fail(f"cannot verify the pinned linux/amd64 manifest: {manifest_bytes.stderr.decode(errors='replace')}")
    verify_oci_manifest(manifest_bytes.stdout, expected_manifest, expected_config)

    run(["docker", "pull", "--platform=linux/amd64", platform_ref])
    base_image = _inspect_image(platform_ref)
    if base_image.get("Id") != expected_config:
        fail("pulled official image config digest differs from the pinned OCI config")
    if base_image.get("Os") != "linux" or base_image.get("Architecture") != "amd64":
        fail("pulled official image is not linux/amd64")
    base_config = base_image.get("Config") or {}
    labels = {
        "org.opencontainers.image.title": "HA Recording Control",
        "org.opencontainers.image.version": args.version,
        "org.opencontainers.image.source": "https://github.com/FernandoFraga86/ha-recording-control",
        "org.opencontainers.image.revision": args.workflow_sha,
        "org.opencontainers.image.description": "Experimental HA Recording Control derivative based on the official Frigate 0.18.2 linux/amd64 image",
        "org.opencontainers.image.base.name": tag_ref,
        "org.opencontainers.image.base.digest": expected_manifest,
        "io.ha-recording-control.upstream-commit": upstream_commit,
        "io.ha-recording-control.production-ready": "false",
        "io.ha-recording-control.native-recovery-enabled": "false",
    }
    labels.update(pinned_patch_labels(verification))
    changes = docker_import_changes(base_config, labels)
    changes_json = json.dumps(changes, ensure_ascii=False, indent=2)
    (temp_root / "docker-import-changes.json").write_text(changes_json, encoding="utf-8")

    container_name = f"ha-recording-control-base-{uuid4().hex[:12]}"
    container_id = run(["docker", "create", "--platform=linux/amd64", "--name", container_name, platform_ref], capture=True).strip()
    archive_path = temp_root / f"ha-recording-control-rootfs-{uuid4().hex}.tar.gz"
    copied_source = temp_root / f"ha-recording-control-base-source-{uuid4().hex}"
    copied_source.mkdir()
    try:
        # Copy the image's unmodified Python tree before the filesystem export.
        run(["docker", "cp", f"{container_id}:/opt/frigate/frigate/.", str(copied_source)])
        source_count = verify_python_tree_matches_commit(upstream, copied_source)

        export_log = temp_root / "ha-recording-control-docker-export.stderr.log"
        with export_log.open("wb") as stderr_file:
            process = subprocess.Popen(
                ["docker", "export", container_id],
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            assert process.stdout is not None
            try:
                repack_stats = repack_rootfs(
                    process.stdout,
                    archive_path,
                    frigate_source=upstream / "frigate",
                    built_web=built_web,
                    attribution_root=upstream,
                    removed_visual_assets=verification["removed_visual_assets"],
                    protected_asset_hashes=verification["upstream_brand_asset_sha256"].values(),
                )
                process.stdout.close()
                if process.wait() != 0:
                    detail = export_log.read_text(encoding="utf-8", errors="replace")
                    fail(f"docker export failed while flattening verified base filesystem: {detail}")
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                process.wait()
                raise

        run(["docker", "rm", container_id])
        container_id = ""
        # The base image is re-pulled by digest for each workflow run. Removing it
        # here frees the runner's disk before docker import materializes one flat layer.
        run(["docker", "image", "rm", platform_ref])

        import_command = ["docker", "import", "--platform=linux/amd64", "--message", "Flattened verified official image with pinned candidate source and neutral UI"]
        for instruction in changes:
            import_command.extend(["--change", instruction])
        import_command.extend([str(archive_path), args.output_image])
        run(import_command)

        final_image = _inspect_image(args.output_image)
        if final_image.get("Os") != "linux" or final_image.get("Architecture") != "amd64":
            fail("imported candidate image is not linux/amd64")
        if len((final_image.get("RootFS") or {}).get("Layers") or []) != 1:
            fail("flattened distribution must contain exactly one filesystem layer")
        if _canonical_config(final_image.get("Config") or {}) != _canonical_config(base_config):
            fail("imported image changed an operational Docker config field from the verified official base")
        actual_labels = (final_image.get("Config") or {}).get("Labels") or {}
        expected_labels = dict(base_config.get("Labels") or {})
        expected_labels.update(labels)
        if actual_labels != expected_labels:
            fail("imported image labels do not preserve the base labels plus exact candidate provenance")

        result = {
            "status": "PASS",
            "base_tag_reference": tag_ref,
            "base_index_digest": expected_index,
            "base_platform_reference": platform_ref,
            "base_platform_manifest_digest": expected_manifest,
            "base_config_digest": expected_config,
            "upstream_python_source_files_verified": source_count,
            "rootfs_transformation": repack_stats,
            "rootfs_archive_bytes": archive_path.stat().st_size,
            "rootfs_archive_sha256": file_sha256(archive_path),
            "candidate_image_id": final_image.get("Id"),
            "candidate_image_layers": len(final_image["RootFS"]["Layers"]),
            "runtime_config_preserved": True,
            "labels": labels,
        }
        print(json.dumps(result, indent=2))
        if args.result_json:
            Path(args.result_json).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "-f", container_id], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if archive_path.exists():
            archive_path.unlink()
        shutil.rmtree(copied_source, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a flattened experimental image from the digest-pinned official Frigate image")
    parser.add_argument("--distribution", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--built-web", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--output-image", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--result-json")
    args = parser.parse_args()
    build_image(args)


if __name__ == "__main__":
    main()
