"""Exercise the experimental image under a real, disposable HAOS Supervisor."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "distribution-lock.json"
TEMPLATE_PATH = ROOT / "supervisor-app-template" / "recording_control" / "config.yaml.template"

HAOS_VERSION = "18.4"
HAOS_ASSET = "haos_ova-18.4.qcow2.xz"
HAOS_ASSET_SHA256 = "756ec485b1b153099ca53e88a5eebd63a801d56b791afe5c6d63ef94a35dc02d"
SUPERVISOR_VERSION = "2026.10.5"
GHCR_IMAGE = "ghcr.io/fernandofraga86/ha-recording-control"
GHCR_TAG = "0.18.2-recording-control-1ca90f8-rebrand1-rootfs1"
GHCR_DIGEST = "sha256:e024fe700120021c710c08bd28d7e65e5f24442755a0381c028bdf3596ebf7c4"
OFFICIAL_IMAGE = "ghcr.io/blakeblackshear/frigate"
OFFICIAL_TAG = "0.18.2"
OFFICIAL_INDEX_DIGEST = "sha256:fd60f99ffd4eb6c42360ed4e14953e270586f239309ab3983e55b2e97ffac09a"
OFFICIAL_AMD64_DIGEST = "sha256:9ebab4801be69ab4ebc7f6ab1d582c859ff7412d457a3fa550056894e8444dad"

LOCAL_REPOSITORY_PATH = "/mnt/data/supervisor/apps/local"
LOCAL_CANDIDATE_SLUG = "local_recording_control"
LOCAL_PREVIOUS_SLUG = "local_recording_control_previous"
FULL_LAB_BRANCH = "codex/supervisor-app-lab-2026-10-5"
PERMISSION_LAB_BRANCH = "codex/supervisor-app-api-permission-2026-11-08"
PROMPT = "__HA_RECORDING_CONTROL_LAB_PROMPT__"
MAX_GUEST_COMMAND_CHARS = 1024
LOGIN_PROMPT = "homeassistant login: "


class LabFailure(RuntimeError):
    """A controlled lab validation failure."""


def log_event(root: Path, message: str) -> None:
    safe = re.sub(r"(?i)(token|password|secret)(\s*[:=]\s*)\S+", r"\1\2[redacted]", message)
    with (root / "lab-events.txt").open("a", encoding="utf-8") as handle:
        handle.write(safe.rstrip() + "\n")
    print(safe, flush=True)


def load_lock() -> dict[str, Any]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def validate_distribution_lock() -> None:
    lock = load_lock()
    if lock.get("production_ready") is not False:
        raise LabFailure("production_ready must remain false")
    if lock.get("installable") is not False:
        raise LabFailure("the published candidate must remain non-installable")
    if lock.get("native_recovery_enabled") is not False:
        raise LabFailure("native recovery must remain disabled")
    app = lock.get("app", {})
    if app.get("target_supervisor_version") != SUPERVISOR_VERSION:
        raise LabFailure("distribution lock Supervisor target differs from the lab pin")
    image = lock.get("image", {})
    if image.get("repository") != GHCR_IMAGE or image.get("version_tag") != GHCR_TAG:
        raise LabFailure("candidate image repository/tag differs from the reviewed lock")
    if image.get("platform_digest_amd64") != GHCR_DIGEST:
        raise LabFailure("candidate image digest differs from the reviewed lock")
    if image.get("installable") is not False:
        raise LabFailure("candidate image lock must remain non-installable")


def _run_root() -> Path:
    configured = os.environ.get("RUNNER_TEMP")
    if not configured:
        raise LabFailure("RUNNER_TEMP is not set; refusing to create lab artifacts in the repository")
    runner_temp = Path(configured).resolve()
    root = runner_temp / "ha-recording-control-supervisor-lab"
    if root.exists():
        raise LabFailure("designated per-run lab directory unexpectedly already exists")
    root.mkdir(parents=True)
    return root


def candidate_lab_manifest(template_text: str, *, homeassistant_api: bool = True) -> str:
    """Create a test-only manifest from the reviewed template with explicit lab overrides."""
    required_fragments = (
        'slug: recording_control',
        f'version: "{GHCR_TAG}"',
        f'image: "{GHCR_IMAGE}"',
        "boot: manual",
        "ingress: true",
        "ingress_port: 5000",
        "homeassistant_api: true",
        "hassio_api: true",
        "full_access: false",
        "PERFMON",
        '"media:rw"',
        '"app_config:rw"',
        '"homeassistant_config:rw"',
    )
    missing = [part for part in required_fragments if part not in template_text]
    if missing:
        raise LabFailure("Supervisor template drifted from the reviewed candidate contract")
    lab = template_text.replace(
        "name: HA Recording Control (Experimental)",
        "name: HA Recording Control (Disposable Supervisor Lab)",
        1,
    ).replace(
        "description: Experimental recording-control integration; not validated for installation.",
        "description: Disposable synthetic validation only; never use with household devices.",
        1,
    )
    lab = lab.replace("boot: manual", "boot: auto", 1)
    lab, device_replacements = re.subn(
        r"(?m)^devices:\n(?:  - [^\n]+\n)+",
        "devices: []\n",
        lab,
        count=1,
    )
    if device_replacements != 1:
        raise LabFailure("could not apply the documented lab-only hardware-device exclusion")
    lab = lab.replace("usb: true", "usb: false", 1)
    lab = lab.replace("video: true", "video: false", 1)
    if not homeassistant_api:
        lab, api_replacements = re.subn(
            r"(?m)^homeassistant_api: true$",
            "homeassistant_api: false",
            lab,
            count=1,
        )
        if api_replacements != 1:
            raise LabFailure("could not apply the single lab-only Home Assistant API permission reduction")
    if "boot: manual" in lab or "boot: auto" not in lab:
        raise LabFailure("lab-only boot:auto override did not apply exactly")
    expected_core_api_permission = f"homeassistant_api: {str(homeassistant_api).lower()}"
    if lab.count(expected_core_api_permission) != 1:
        raise LabFailure("lab manifest does not contain exactly the requested Home Assistant API permission")
    if f'image: "{GHCR_IMAGE}"' not in lab or f'version: "{GHCR_TAG}"' not in lab:
        raise LabFailure("lab overlay changed the reviewed image tag/reference")
    return lab


def previous_lab_manifest() -> str:
    """Minimal synthetic rollback peer using the official Frigate 0.18.2 image."""
    return f'''name: Frigate Previous (Disposable Rollback Lab)
version: "{OFFICIAL_TAG}"
slug: recording_control_previous
description: Disposable rollback peer using synthetic files only.
stage: experimental
image: "{OFFICIAL_IMAGE}"
legacy: true
startup: application
boot: manual
init: false
host_network: false
ports: {{}}
schema: false
arch:
  - amd64
map:
  - "media:rw"
  - "app_config:rw"
'''


def synthetic_frigate_config() -> str:
    return """mqtt:\n  enabled: false\ndetectors:\n  cpu:\n    type: cpu\n    num_threads: 1\ncameras: {}\nrecord:\n  enabled: false\nversion: 0.18-0\n"""


def _registry_token(repository: str) -> str:
    query = urllib.parse.urlencode(
        {"service": "ghcr.io", "scope": f"repository:{repository}:pull"}
    )
    request = urllib.request.Request(
        f"https://ghcr.io/token?{query}", headers={"User-Agent": "ha-recording-control-supervisor-lab"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    token = body.get("token") or body.get("access_token")
    if not isinstance(token, str) or not token:
        raise LabFailure("GHCR public registry did not return an anonymous pull token")
    return token


def registry_manifest_digest(image: str, tag: str) -> str:
    host, repository = image.split("/", 1)
    token = _registry_token(repository)
    request = urllib.request.Request(
        f"https://{host}/v2/{repository}/manifests/{tag}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": ", ".join(
                (
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                )
            ),
            "User-Agent": "ha-recording-control-supervisor-lab",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        digest = response.headers.get("Docker-Content-Digest")
    if not digest or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise LabFailure("GHCR manifest response lacked a valid Docker-Content-Digest")
    return digest


def verify_registry_tags(root: Path) -> None:
    candidate = registry_manifest_digest(GHCR_IMAGE, GHCR_TAG)
    if candidate != GHCR_DIGEST:
        raise LabFailure("candidate tag no longer resolves to the homologated OCI digest")
    official = registry_manifest_digest(OFFICIAL_IMAGE, OFFICIAL_TAG)
    if official != OFFICIAL_INDEX_DIGEST:
        raise LabFailure("official rollback image tag no longer resolves to its locked index digest")
    log_event(root, "Candidate and official rollback image tags resolve to their pinned manifest digests.")


def verify_haos_release(root: Path) -> Path:
    release_url = "https://api.github.com/repos/home-assistant/operating-system/releases/tags/18.4"
    request = urllib.request.Request(release_url, headers={"User-Agent": "ha-recording-control-supervisor-lab"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next((entry for entry in release.get("assets", []) if entry.get("name") == HAOS_ASSET), None)
    if asset is None:
        raise LabFailure("official HAOS release does not list the pinned OVA qcow2 asset")
    if asset.get("digest") != f"sha256:{HAOS_ASSET_SHA256}":
        raise LabFailure("official HAOS release asset digest differs from the pinned value")
    expected_url = f"https://github.com/home-assistant/operating-system/releases/download/{HAOS_VERSION}/{HAOS_ASSET}"
    if asset.get("browser_download_url") != expected_url:
        raise LabFailure("HAOS image URL is not the exact official release asset")
    archive = root / HAOS_ASSET
    request = urllib.request.Request(expected_url, headers={"User-Agent": "ha-recording-control-supervisor-lab"})
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=180) as response, archive.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != HAOS_ASSET_SHA256:
        raise LabFailure("downloaded official HAOS image failed its SHA-256 check")
    log_event(root, f"Official HAOS {HAOS_VERSION} qcow2.xz download hash verified ({archive.stat().st_size} bytes).")
    return archive


def extract_haos(archive: Path, root: Path) -> Path:
    image = root / "haos-ova-lab.qcow2"
    with image.open("wb") as output:
        result = subprocess.run(
            ["xz", "-dc", str(archive)],
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    if result.returncode != 0:
        raise LabFailure("xz failed to extract the verified HAOS release archive")
    info = subprocess.run(
        ["qemu-img", "info", "--output=json", str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    image_info = json.loads(info.stdout)
    if image_info.get("format") != "qcow2" or int(image_info.get("virtual-size", 0)) < 16 * 1024**3:
        raise LabFailure("verified HAOS artifact is not a plausible x86-64 qcow2 VM disk")
    subprocess.run(["qemu-img", "resize", str(image), "48G"], check=True, timeout=30)
    log_event(root, f"HAOS qcow2 verified and expanded to a disposable 48-GiB virtual disk (sparse file).")
    return image


def find_ovmf() -> tuple[Path, Path | None]:
    candidates = (
        Path("/usr/share/ovmf/OVMF.fd"),
        Path("/usr/share/OVMF/OVMF.fd"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate, None
    code_candidates = (
        Path("/usr/share/OVMF/OVMF_CODE.fd"),
        Path("/usr/share/ovmf/OVMF_CODE.fd"),
    )
    vars_candidates = (
        Path("/usr/share/OVMF/OVMF_VARS.fd"),
        Path("/usr/share/ovmf/OVMF_VARS.fd"),
    )
    code = next((path for path in code_candidates if path.is_file()), None)
    variables = next((path for path in vars_candidates if path.is_file()), None)
    if code and variables:
        return code, variables
    raise LabFailure("OVMF firmware files are unavailable after the runner package step")


class HAOSConsole:
    """A serial-console client for a disposable HAOS/QEMU guest."""

    def __init__(self, image: Path, root: Path, pexpect: Any):
        self.image = image
        self.root = root
        self.pexpect = pexpect
        firmware, variables = find_ovmf()
        args = [
            "qemu-system-x86_64",
            "-machine",
            "q35,accel=kvm",
            "-cpu",
            "host",
            "-smp",
            "4",
            "-m",
            "6144",
            "-drive",
            f"file={image},if=virtio,format=qcow2",
            "-netdev",
            "user,id=net0",
            "-device",
            "virtio-net-pci,netdev=net0",
            "-display",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
        ]
        if variables is None:
            args.extend(["-bios", str(firmware)])
        else:
            vars_copy = root / "OVMF_VARS.lab.fd"
            shutil.copyfile(variables, vars_copy)
            args.extend(
                [
                    "-drive",
                    f"if=pflash,format=raw,readonly=on,file={firmware}",
                    "-drive",
                    f"if=pflash,format=raw,file={vars_copy}",
                ]
            )
        self.child = pexpect.spawn(args[0], args[1:], encoding="utf-8", timeout=60, maxread=65536)
        self.child.delaybeforesend = 0.1
        self.ready = False

    def boot_login(self, timeout: int = 1200) -> None:
        self.child.expect_exact(LOGIN_PROMPT, timeout=timeout)
        self.child.sendline("root")
        self.child.expect_exact("# ", timeout=60)
        self.child.sendline(f"export PS1='{PROMPT}'; export TERM=dumb")
        self.child.expect_exact(PROMPT, timeout=30)
        self.ready = True

    def run(self, command: str, timeout: int = 180, allow_failure: bool = False) -> str:
        if not self.ready:
            raise LabFailure("HAOS serial shell is not ready")
        marker = "__HA_RC_COMMAND_RC__"
        self.child.sendline(f"{command}; __lab_rc=$?; printf '\\n{marker}%s\\n' \"$__lab_rc\"")
        self.child.expect_exact(PROMPT, timeout=timeout)
        output = self.child.before
        matches = re.findall(re.escape(marker) + r"(\d+)", output)
        if not matches:
            raise LabFailure("HAOS command did not return a status marker")
        code = int(matches[-1])
        if code and not allow_failure:
            summary = " ".join(command.split()[:4])
            raise LabFailure(f"HAOS command failed ({code}): {summary}")
        return output

    def json_command(self, command: str, timeout: int = 180) -> dict[str, Any]:
        output = self.run(command, timeout=timeout)
        decoder = json.JSONDecoder()
        for index, char in enumerate(output):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(output[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "result" in value:
                if value.get("result") != "ok":
                    raise LabFailure(f"Home Assistant CLI returned a non-ok result for: {' '.join(command.split()[:4])}")
                return value
        raise LabFailure(f"Home Assistant CLI did not return JSON for: {' '.join(command.split()[:4])}")

    def reboot(self) -> None:
        if not self.ready:
            raise LabFailure("cannot reboot before console readiness")
        self.child.sendline("ha host reboot --no-progress --raw-json")
        try:
            self.child.expect_exact(PROMPT, timeout=120)
        except self.pexpect.TIMEOUT:
            pass
        self.ready = False
        try:
            self.child.expect_exact(LOGIN_PROMPT, timeout=900)
        except self.pexpect.EOF as err:
            raise LabFailure("QEMU exited while HAOS was rebooting") from err
        self.child.sendline("root")
        self.child.expect_exact("# ", timeout=60)
        self.child.sendline(f"export PS1='{PROMPT}'; export TERM=dumb")
        self.child.expect_exact(PROMPT, timeout=30)
        self.ready = True

    def close(self) -> None:
        if not self.child.isalive():
            return
        if self.ready:
            try:
                self.run("ha host shutdown --no-progress --raw-json", timeout=90, allow_failure=True)
            except (LabFailure, self.pexpect.TIMEOUT, self.pexpect.EOF):
                pass
        try:
            self.child.expect(self.pexpect.EOF, timeout=120)
        except (self.pexpect.TIMEOUT, self.pexpect.EOF):
            pass
        if self.child.isalive():
            self.child.terminate(force=True)


def extract_json(output: str) -> Any:
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise LabFailure("could not parse the expected JSON from a local Docker command")


def docker_output(console: HAOSConsole, command: str, timeout: int = 120) -> str:
    output = console.run(command, timeout=timeout)
    marker = "__HA_RC_COMMAND_RC__"
    return output.rsplit(marker, 1)[0]


def list_candidate_containers(console: HAOSConsole) -> list[tuple[str, str, str]]:
    output = docker_output(
        console,
        "docker ps --no-trunc --format '{{.ID}}|{{.Image}}|{{.Names}}'",
        timeout=30,
    )
    items: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        if not line or "|" not in line:
            continue
        image_echo_removed = line.strip()
        fields = image_echo_removed.split("|", 2)
        if len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{12,64}", fields[0]):
            continue
        if fields[1].startswith(GHCR_IMAGE + ":") or fields[1].startswith(OFFICIAL_IMAGE + ":"):
            items.append((fields[0], fields[1], fields[2]))
    return items


def list_candidate_containers_all(console: HAOSConsole) -> list[dict[str, str]]:
    output = docker_output(
        console,
        "docker ps --all --no-trunc --format '{{.ID}}|{{.Image}}|{{.Names}}|{{.State}}'",
        timeout=30,
    )
    items: list[dict[str, str]] = []
    for line in output.splitlines():
        fields = line.strip().split("|", 3)
        if len(fields) != 4 or not re.fullmatch(r"[0-9a-f]{12,64}", fields[0]):
            continue
        if fields[1].startswith(GHCR_IMAGE + ":") or fields[1].startswith(OFFICIAL_IMAGE + ":"):
            items.append({"id": fields[0], "image": fields[1], "name": fields[2].lstrip("/"), "state": fields[3]})
    return items


def assert_single_writer(console: HAOSConsole, expected_image_prefix: str | None) -> tuple[str, str, str] | None:
    writers = list_candidate_containers(console)
    if expected_image_prefix is None:
        if writers:
            raise LabFailure("a Frigate container remained running when the test expected none")
        return None
    selected = [entry for entry in writers if entry[1].startswith(expected_image_prefix)]
    if len(writers) != 1 or len(selected) != 1:
        raise LabFailure("the synthetic migration did not maintain exactly one active Frigate writer")
    return selected[0]


def stop_quiescence_confirmed(
    *,
    stop_job_succeeded: bool,
    supervisor_state: str,
    container_state: dict[str, Any] | None,
    active_frigate_writers: bool,
) -> bool:
    """Confirm an acknowledged manual stop without mistaking Supervisor ERROR for a live writer.

    Supervisor 2026.10.5 maps a Docker FAILED event to app state ERROR even
    after an intentional stop. The caller still has to prove that the exact
    app container is gone or exited, and that no Frigate image container is
    running before it can proceed.
    """
    if stop_job_succeeded is not True or active_frigate_writers or supervisor_state not in {"stopped", "error"}:
        return False
    if container_state is None:
        return True
    if container_state.get("Status") != "exited" or container_state.get("OOMKilled") is True:
        return False
    if supervisor_state == "stopped":
        return True
    return (
        container_state.get("ExitCode") == 143
        and not container_state.get("Error")
    )


def wait_for_app_quiesced(
    console: HAOSConsole,
    slug: str,
    stopped_container_id: str,
    root: Path,
    *,
    timeout: int = 300,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_state: str | None = None
    last_container_state: dict[str, Any] | None = None
    expected_name = f"app_{slug}"
    while time.monotonic() < deadline:
        info = cli_info(console, slug)
        last_state = str(info.get("state"))
        records = list_candidate_containers_all(console)
        exact = [item for item in records if item["id"] == stopped_container_id or item["name"] == expected_name]
        if len(exact) > 1:
            raise LabFailure(f"multiple Frigate containers matched stopped app {slug}")
        if exact and exact[0]["id"] != stopped_container_id:
            raise LabFailure(f"Supervisor replaced the stopped container for {slug}; writer identity changed")

        if exact:
            last_container_state = inspect_container(console, stopped_container_id).get("State")
            if not isinstance(last_container_state, dict):
                raise LabFailure(f"Docker returned no state for stopped app container {slug}")
        else:
            last_container_state = None

        active = list_candidate_containers(console)
        if not active and stop_quiescence_confirmed(
            stop_job_succeeded=True,
            supervisor_state=last_state,
            container_state=last_container_state,
            active_frigate_writers=bool(active),
        ):
            if last_state == "error":
                detail = (
                    "container removed by Supervisor"
                    if last_container_state is None
                    else "container exited after default SIGTERM (143)"
                )
                log_event(
                    root,
                    f"Supervisor reported app state=error after acknowledged manual stop; Docker confirmed {detail} and no Frigate writer remains active.",
                )
            return info

        if active:
            raise LabFailure("a Frigate writer remained active after Supervisor's successful stop job")
        time.sleep(5)

    raise LabFailure(
        f"app {slug} did not become quiescent after acknowledged stop; "
        f"Supervisor state={last_state!r}, container state={last_container_state!r}"
    )


def cli_info(console: HAOSConsole, slug: str) -> dict[str, Any]:
    response = console.json_command(f"ha apps info {slug} --no-progress --raw-json")
    data = response.get("data")
    if not isinstance(data, dict):
        raise LabFailure("Supervisor returned an unexpected app-info payload")
    return data


def wait_for_app(console: HAOSConsole, slug: str, state: str, timeout: int = 600) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_state = None
    while time.monotonic() < deadline:
        try:
            info = cli_info(console, slug)
            last_state = info.get("state")
            if last_state == state:
                return info
        except LabFailure:
            pass
        time.sleep(5)
    raise LabFailure(f"app {slug} did not reach state {state}; last observed state={last_state!r}")


def wait_for_supervisor(console: HAOSConsole, root: Path) -> None:
    deadline = time.monotonic() + 1800
    last = "not-ready"
    while time.monotonic() < deadline:
        try:
            response = console.json_command("ha supervisor info --no-progress --raw-json", timeout=60)
            data = response.get("data", {})
            last = str(data.get("version"))
            if last == SUPERVISOR_VERSION:
                core = console.json_command("ha core info --no-progress --raw-json", timeout=60).get("data", {})
                if core.get("version"):
                    log_event(root, f"Real HAOS boot completed with Supervisor {last} and Core {core.get('version')}.")
                    return
        except LabFailure:
            pass
        time.sleep(10)
    raise LabFailure(f"HAOS did not reach the pinned Supervisor {SUPERVISOR_VERSION}; last version={last}")


def guest_write(console: HAOSConsole, path: str, content: str) -> None:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    if not re.fullmatch(r"/mnt/data/supervisor/(?:apps/local|app_configs)/[A-Za-z0-9_./-]+", path):
        raise LabFailure("refusing to write a lab manifest outside the disposable Supervisor data tree")
    parent = path.rsplit("/", 1)[0]
    # A full manifest command left the HAOS serial shell at its continuation
    # prompt during the first native run. Keep each command short; 512 encoded
    # characters is divisible by four, so each chunk remains independently
    # decodable and is acknowledged before the next chunk is sent.
    chunks = [encoded[index:index + 512] for index in range(0, len(encoded), 512)]
    if not chunks:
        chunks = [""]
    first = (
        f"mkdir -p {parent} && test ! -e {path} && printf '%s' '{chunks[0]}' "
        f"| base64 -d > {path} && chmod 0644 {path}"
    )
    commands = [first]
    for chunk in chunks[1:]:
        commands.append(f"printf '%s' '{chunk}' | base64 -d >> {path}")
    if any(len(command) >= MAX_GUEST_COMMAND_CHARS for command in commands):
        raise LabFailure("refusing to send an oversized command to the HAOS serial shell")
    for command in commands:
        console.run(command, timeout=30)


def store_local_manifests(console: HAOSConsole, root: Path) -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    candidate = candidate_lab_manifest(template)
    previous = previous_lab_manifest()
    guest_write(console, f"{LOCAL_REPOSITORY_PATH}/recording_control/config.yaml", candidate)
    guest_write(console, f"{LOCAL_REPOSITORY_PATH}/recording_control_previous/config.yaml", previous)
    guest_write(console, "/mnt/data/supervisor/app_configs/local_recording_control/config.yml", synthetic_frigate_config())
    guest_write(console, "/mnt/data/supervisor/app_configs/local_recording_control_previous/config.yml", synthetic_frigate_config())
    # The store-reload API may return an empty response body. In --raw-json
    # mode the CLI prints that body verbatim, so json_command() cannot parse it.
    # Use the CLI's success response, then independently inspect both store
    # entries before any app installation is attempted.
    reload_output = console.run("ha store reload --no-progress", timeout=180)
    if "Command completed successfully." not in reload_output:
        raise LabFailure("Supervisor CLI did not positively report local store reload success")
    verify_local_store_entries(console, root)
    log_event(
        root,
        "Supervisor local app manifests loaded; lab-only differences are boot:auto and omission of unavailable camera/GPU/USB devices.",
    )


def store_permission_lab_manifest(console: HAOSConsole, root: Path) -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    candidate = candidate_lab_manifest(template, homeassistant_api=False)
    guest_write(console, f"{LOCAL_REPOSITORY_PATH}/recording_control/config.yaml", candidate)
    guest_write(console, "/mnt/data/supervisor/app_configs/local_recording_control/config.yml", synthetic_frigate_config())
    # `run()` already fails on a non-zero command status. Verify the resulting
    # Supervisor store entry below instead of depending on presentation text.
    console.run("ha store reload --no-progress", timeout=180)
    info = cli_info(console, LOCAL_CANDIDATE_SLUG)
    if info.get("slug") != LOCAL_CANDIDATE_SLUG or info.get("available") is not True:
        raise LabFailure("Supervisor permission-lab entry is not available after store reload")
    if info.get("version_latest") != GHCR_TAG:
        raise LabFailure("Supervisor permission-lab entry resolved an unexpected image version")
    if info.get("homeassistant_api") is not False:
        raise LabFailure("Supervisor did not load the explicitly disabled Core API permission")
    log_event(root, "Disposable manifest loaded with homeassistant_api:false; all other reviewed grants remain unchanged.")


def verify_local_store_entries(console: HAOSConsole, root: Path) -> None:
    expected = (
        (LOCAL_PREVIOUS_SLUG, OFFICIAL_TAG),
        (LOCAL_CANDIDATE_SLUG, GHCR_TAG),
    )
    for slug, version in expected:
        info = cli_info(console, slug)
        if info.get("slug") != slug or info.get("available") is not True:
            raise LabFailure(f"Supervisor local store did not expose an available entry for {slug}")
        if info.get("version_latest") != version:
            raise LabFailure(f"Supervisor local store entry {slug} resolved an unexpected version")
    log_event(root, "Supervisor confirmed both local app entries are available at their pinned versions.")


def install_start(console: HAOSConsole, slug: str, root: Path) -> dict[str, Any]:
    console.json_command(f"ha apps install {slug} --no-progress --raw-json", timeout=1800)
    console.json_command(f"ha apps start {slug} --no-progress --raw-json", timeout=900)
    info = wait_for_app(console, slug, "started", timeout=900)
    log_event(root, f"Supervisor installed and started {slug} at version {info.get('version')}.")
    return info


def container_for_image(console: HAOSConsole, image_prefix: str) -> tuple[str, str, str]:
    writers = [entry for entry in list_candidate_containers(console) if entry[1].startswith(image_prefix)]
    if len(writers) != 1:
        raise LabFailure("could not identify exactly one running synthetic Frigate container")
    return writers[0]


def docker_image_repo_digests(console: HAOSConsole, image: str) -> list[str]:
    output = docker_output(
        console,
        f"docker image inspect {image} --format '{{{{json .RepoDigests}}}}'",
        timeout=60,
    )
    return extract_json(output)


def verify_pulled_image_digest(console: HAOSConsole, image: str, accepted: set[str]) -> None:
    repo_digests = docker_image_repo_digests(console, image)
    digests = {entry.rsplit("@", 1)[-1] for entry in repo_digests if "@" in entry}
    if not (digests & accepted):
        raise LabFailure(f"Supervisor-pulled image did not report the expected registry digest: {image}")


def inspect_container(console: HAOSConsole, container_id: str) -> dict[str, Any]:
    output = docker_output(console, f"docker inspect {container_id}", timeout=60)
    result = extract_json(output)
    if not isinstance(result, list) or len(result) != 1:
        raise LabFailure("docker inspect returned an unexpected container result")
    return result[0]


def verify_candidate_metadata(
    console: HAOSConsole,
    info: dict[str, Any],
    container: dict[str, Any],
    root: Path,
    *,
    homeassistant_api: bool = True,
) -> None:
    expected = {
        "state": "started",
        "version": GHCR_TAG,
        "boot": "auto",
        "startup": "application",
        "ingress": True,
        "ingress_port": 5000,
        "homeassistant_api": homeassistant_api,
        "hassio_api": True,
        "full_access": False,
        "usb": False,
        "video": False,
        "privileged": ["PERFMON"],
    }
    for key, value in expected.items():
        if info.get(key) != value:
            raise LabFailure(f"Supervisor app-info field {key} did not match the isolated lab contract")
    if not isinstance(info.get("ingress_url"), str) or not info["ingress_url"]:
        raise LabFailure("Supervisor did not create an ingress route for the running app")
    if not ingress_metadata_valid(info):
        raise LabFailure(f"Supervisor app UI/ingress metadata is incomplete: {ingress_metadata_diagnostics(info)}")
    panel = info.get("ingress_panel")
    if isinstance(panel, dict) and panel.get("admin") is not False:
        raise LabFailure("the lab panel unexpectedly became administrator-only")
    if info.get("devices") not in ([], None):
        raise LabFailure("lab manifest unexpectedly requested host device nodes")

    host_config = container.get("HostConfig", {})
    if host_config.get("NetworkMode") == "host":
        raise LabFailure("the candidate unexpectedly used host networking")
    port_bindings = host_config.get("PortBindings", {})
    if not port_bindings.get("8555/tcp") or not port_bindings.get("8555/udp"):
        raise LabFailure("Supervisor did not apply the candidate's explicit WebRTC 8555 port mappings")
    for unexposed in ("5000/tcp", "8554/tcp", "8971/tcp", "1984/tcp", "1935/tcp"):
        if port_bindings.get(unexposed):
            raise LabFailure(f"unexpected host port binding for {unexposed}")
    mounts = container.get("Mounts", [])
    destinations = {mount.get("Destination"): mount for mount in mounts}
    for destination in ("/media", "/config", "/homeassistant"):
        mount = destinations.get(destination)
        if not mount or mount.get("RW") is not True:
            raise LabFailure(f"expected writable Supervisor volume is missing: {destination}")
    cap_add = host_config.get("CapAdd") or []
    if "PERFMON" not in cap_add and "CAP_PERFMON" not in cap_add:
        raise LabFailure("Docker did not receive the declared PERFMON capability")
    log_event(
        root,
        f"Supervisor effective metadata verified: ingress, homeassistant_api={homeassistant_api}, remaining API grants, explicit port bindings, writable media/config/homeassistant mounts, no host network/full access, and PERFMON.",
    )


def ingress_metadata_valid(info: dict[str, Any]) -> bool:
    """Validate Supervisor's effective ingress URLs without exposing their token."""
    entry = info.get("ingress_entry")
    url = info.get("ingress_url")
    entry_match = re.fullmatch(r"/api/hassio_ingress/([^/]+)", entry) if isinstance(entry, str) else None
    url_match = re.fullmatch(r"/api/hassio_ingress/([^/]+)(?:/.*)?", url) if isinstance(url, str) else None
    webui = info.get("webui")
    webui_valid = isinstance(webui, str) and re.fullmatch(
        r"https?://\[HOST\]:(?:[0-9]+|None)/", webui
    ) is not None
    return bool(entry_match and url_match and entry_match.group(1) == url_match.group(1) and webui_valid)


def ingress_metadata_diagnostics(info: dict[str, Any]) -> str:
    """Return token-free shape diagnostics for a failed ingress metadata check."""
    entry = info.get("ingress_entry")
    url = info.get("ingress_url")
    entry_match = re.fullmatch(r"/api/hassio_ingress/([^/]+)", entry) if isinstance(entry, str) else None
    url_match = re.fullmatch(r"/api/hassio_ingress/([^/]+)(?:/.*)?", url) if isinstance(url, str) else None
    webui = info.get("webui")
    webui_valid = isinstance(webui, str) and re.fullmatch(
        r"https?://\[HOST\]:(?:[0-9]+|None)/", webui
    ) is not None
    return (
        f"entry_path={bool(entry_match)}, url_path={bool(url_match)}, "
        f"route_token_match={bool(entry_match and url_match and entry_match.group(1) == url_match.group(1))}, "
        f"webui_shape={webui_valid}"
    )


def app_api_from_core(
    console: HAOSConsole,
    container_id: str,
    root: Path,
    *,
    verify_synthetic_config: bool = False,
) -> None:
    inspected = inspect_container(console, container_id)
    network = inspected.get("NetworkSettings", {}).get("Networks", {})
    address = next((entry.get("IPAddress") for entry in network.values() if entry.get("IPAddress")), None)
    if not address:
        raise LabFailure("Supervisor app container has no address on the isolated HA network")
    if verify_synthetic_config:
        python = (
            "import json,urllib.request; "
            f'base="http://{address}:5000/api/"; '
            'version=urllib.request.urlopen(base+"version",timeout=10).read().decode().strip(); '
            'config=json.loads(urllib.request.urlopen(base+"config",timeout=10).read()); '
            'record=config.get("record"); '
            'assert isinstance(record,dict) and record.get("enabled") is False; '
            'print(version+" synthetic_record_disabled")'
        )
    else:
        python = (
            "import urllib.request; "
            f'print(urllib.request.urlopen("http://{address}:5000/api/version", timeout=10).read().decode().strip())'
        )
    output = docker_output(
        console,
        f"docker exec homeassistant python3 -c {shlex.quote(python)}",
        timeout=45,
    )
    version_lines = [line.strip() for line in output.splitlines() if re.match(r"^0\.18\.2(?:[-.]|$)", line.strip())]
    if not version_lines:
        if verify_synthetic_config:
            raise LabFailure("HA Core could not retrieve the synthetic Frigate API/config with recording disabled")
        raise LabFailure("HA Core could not retrieve the synthetic Frigate /api/version response")
    event = (
        "HA Core reached the app API and confirmed the synthetic config has recording disabled."
        if verify_synthetic_config
        else "HA Core container reached the Supervisor-managed Frigate API over the app network."
    )
    log_event(root, event)


def core_api_permission_probe_source() -> str:
    return (
        "import os,urllib.error,urllib.request; "
        'token=os.environ.get("SUPERVISOR_TOKEN"); '
        'assert token; '
        'supervisor=urllib.request.urlopen(urllib.request.Request("http://supervisor/info",headers={"Authorization":"Bearer "+token}),timeout=10).status; '
        'assert supervisor==200; '
        'request=urllib.request.Request("http://supervisor/core/api/config",headers={"Authorization":"Bearer "+token}); '
        'status=None; '
        'exec("try:\\n response=urllib.request.urlopen(request,timeout=10); status=response.status\\nexcept urllib.error.HTTPError as error:\\n status=error.code"); '
        'print("supervisor_status="+str(supervisor)+" core_api_status="+str(status))'
    )


def app_core_api_denied(console: HAOSConsole, container_id: str, root: Path) -> None:
    python = core_api_permission_probe_source()
    output = docker_output(console, f"docker exec {container_id} python3 -c {shlex.quote(python)}", timeout=45)
    if "supervisor_status=200 core_api_status=401" not in output:
        raise LabFailure("Core API proxy was not denied after the app's Supervisor token was independently accepted")
    log_event(root, "Valid app token reached Supervisor /info (HTTP 200); Core API proxy denied the app with HTTP 401.")


def ingress_probe_source(ingress_path: str) -> str:
    return "\n".join(
        (
            "import json,secrets,urllib.parse,urllib.request,urllib.error",
            'base="http://127.0.0.1:8123"; client="http://home-assistant.io"; stage="onboarding"',
            "try:",
            ' payload={"name":"Lab","username":"l"+secrets.token_hex(4),"password":secrets.token_hex(16),"client_id":client,"language":"en"}',
            ' request=urllib.request.Request(base+"/api/onboarding/users",data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})',
            ' auth_code=json.loads(urllib.request.urlopen(request,timeout=15).read())["auth_code"]',
            ' stage="token"',
            ' form=urllib.parse.urlencode({"grant_type":"authorization_code","code":auth_code,"client_id":client}).encode()',
            ' request=urllib.request.Request(base+"/auth/token",data=form,headers={"Content-Type":"application/x-www-form-urlencoded"})',
            ' access=json.loads(urllib.request.urlopen(request,timeout=15).read())["access_token"]',
            ' stage="ingress"',
            f' request=urllib.request.Request(base+{ingress_path!r},headers={{"Authorization":"Bearer "+access}})',
            ' print(urllib.request.urlopen(request,timeout=20).read().decode().strip())',
            'except urllib.error.HTTPError as error:',
            ' print("LAB_ERROR stage="+stage+" status="+str(error.code))',
            'except Exception as error:',
            ' print("LAB_ERROR stage="+stage+" kind="+type(error).__name__)',
        )
    )


def ingress_to_app_api(console: HAOSConsole, info: dict[str, Any], root: Path) -> None:
    entry = info.get("ingress_entry")
    match = re.fullmatch(r"/api/hassio_ingress/([A-Za-z0-9_-]+)", entry) if isinstance(entry, str) else None
    if not match:
        raise LabFailure("cannot test ingress because Supervisor did not provide a valid ingress entry")
    ingress_path = entry + "/api/version"
    python = ingress_probe_source(ingress_path)
    guest_script = f"{LOCAL_REPOSITORY_PATH}/recording_control/permission_ingress_probe.py"
    core_script = "/tmp/permission_ingress_probe.py"
    guest_write(console, guest_script, python)
    try:
        console.run(f"test -s {guest_script}", timeout=30)
        console.run(f"docker cp {guest_script} homeassistant:{core_script}", timeout=45)
        output = docker_output(console, f"docker exec homeassistant python3 {core_script}", timeout=90)
        diagnostic = re.search(r"LAB_ERROR stage=([a-z]+) (?:status=(\d+)|kind=([A-Za-z]+))", output)
        if diagnostic:
            detail = f"HTTP {diagnostic.group(2)}" if diagnostic.group(2) else diagnostic.group(3)
            raise LabFailure(f"authenticated ingress probe failed at {diagnostic.group(1)} ({detail})")
        if not any(re.match(r"^0\.18\.2(?:[-.]|$)", line.strip()) for line in output.splitlines()):
            raise LabFailure("authenticated Home Assistant ingress did not return the pinned Frigate API version")
    finally:
        console.run(f"rm -f {guest_script}; docker exec homeassistant rm -f {core_script}", timeout=30, allow_failure=True)
    log_event(root, "Authenticated Home Assistant ingress reached the pinned app API; temporary lab account/token were not recorded.")


def marker_in_container(console: HAOSConsole, container_id: str, action: str, root: Path) -> None:
    marker = "/media/frigate/ha-recording-control-supervisor-lab-marker.txt"
    if action == "write":
        code = (
            'from pathlib import Path; p=Path("' + marker + '"); p.parent.mkdir(parents=True, exist_ok=True); '
            'p.write_text("synthetic-persistence-marker-v1", encoding="utf-8")'
        )
        console.run(f"docker exec {container_id} python3 -c {shlex.quote(code)}", timeout=30)
    elif action == "read":
        code = 'from pathlib import Path; print(Path("' + marker + '").read_text(encoding="utf-8"))'
        output = docker_output(console, f"docker exec {container_id} python3 -c {shlex.quote(code)}", timeout=30)
        if "synthetic-persistence-marker-v1" not in output:
            raise LabFailure("the next app version did not observe the previous app's synthetic media marker")
    else:
        raise ValueError(action)
    log_event(root, f"Synthetic shared-media marker {action} verified through the running container.")


def wait_after_reboot(console: HAOSConsole, root: Path) -> None:
    wait_for_supervisor(console, root)
    deadline = time.monotonic() + 900
    last_state = None
    while time.monotonic() < deadline:
        try:
            info = cli_info(console, LOCAL_CANDIDATE_SLUG)
            last_state = info.get("state")
            if last_state == "started":
                container_for_image(console, GHCR_IMAGE + ":")
                verify_pulled_image_digest(console, GHCR_IMAGE + ":" + GHCR_TAG, {GHCR_DIGEST})
                log_event(root, "boot:auto app recovered as started after a full HAOS guest reboot.")
                return
        except LabFailure:
            pass
        time.sleep(5)
    raise LabFailure(f"candidate did not auto-recover after guest reboot; last state={last_state!r}")


def run_permission_lab() -> None:
    if os.environ.get("GITHUB_REPOSITORY") != "FernandoFraga86/ha-recording-control":
        raise LabFailure("refusing to run outside the authorized repository")
    if os.environ.get("GITHUB_REF_NAME") != PERMISSION_LAB_BRANCH:
        raise LabFailure("refusing to run the permission test outside its dedicated branch")
    root = _run_root()
    validate_distribution_lock()
    log_event(root, "Permission-only lab started; production/installability gates remain false.")
    verify_registry_tags(root)

    try:
        import pexpect
    except ImportError as err:
        raise LabFailure("runner lacks the pexpect package installed by the lab workflow") from err

    archive = verify_haos_release(root)
    image = extract_haos(archive, root)
    console = HAOSConsole(image, root, pexpect)
    try:
        log_event(root, "Booting a fresh disposable HAOS guest for the single API-permission test.")
        console.boot_login()
        wait_for_supervisor(console, root)

        store_permission_lab_manifest(console, root)
        app_info = install_start(console, LOCAL_CANDIDATE_SLUG, root)
        candidate_container = container_for_image(console, GHCR_IMAGE + ":")
        verify_pulled_image_digest(console, GHCR_IMAGE + ":" + GHCR_TAG, {GHCR_DIGEST})
        app_info = cli_info(console, LOCAL_CANDIDATE_SLUG)
        candidate_container_data = inspect_container(console, candidate_container[0])
        verify_candidate_metadata(
            console,
            app_info,
            candidate_container_data,
            root,
            homeassistant_api=False,
        )
        if app_info.get("state") != "started":
            raise LabFailure("candidate did not remain started with homeassistant_api:false")

        app_api_from_core(console, candidate_container[0], root, verify_synthetic_config=True)
        app_core_api_denied(console, candidate_container[0], root)
        ingress_to_app_api(console, app_info, root)

        # Re-resolve the tag after Supervisor pulled it to detect tag drift/TOCTOU.
        if registry_manifest_digest(GHCR_IMAGE, GHCR_TAG) != GHCR_DIGEST:
            raise LabFailure("candidate GHCR tag changed during the permission-only Supervisor test")
        log_event(root, "Post-pull GHCR tag remains bound to the pinned digest.")

        console.json_command(f"ha apps stop {LOCAL_CANDIDATE_SLUG} --no-progress --raw-json", timeout=600)
        wait_for_app_quiesced(console, LOCAL_CANDIDATE_SLUG, candidate_container[0], root, timeout=300)
        assert_single_writer(console, None)

        summary = {
            "status": "passed_disposable_homeassistant_api_permission_test",
            "haos_release": HAOS_VERSION,
            "supervisor_version": SUPERVISOR_VERSION,
            "candidate_image": f"{GHCR_IMAGE}:{GHCR_TAG}",
            "candidate_digest": GHCR_DIGEST,
            "candidate_app_slug": LOCAL_CANDIDATE_SLUG,
            "homeassistant_api": False,
            "hassio_api": True,
            "full_access": False,
            "core_to_app_api": "passed; synthetic Frigate config reported record.enabled=false",
            "app_to_core_api_proxy": "denied with HTTP 401 after the same Supervisor token received HTTP 200 from /info",
            "authenticated_ingress_to_app_api": "passed; token and disposable account not recorded",
            "mqtt": "disabled in synthetic Frigate configuration",
            "camera_feeds": "none",
            "household_endpoints": "none",
            "reboot_or_migration_test": False,
            "production_ready": False,
            "installable": False,
            "native_recovery_enabled": False,
            "notices": [
                "Only the homeassistant_api permission was reduced from the reviewed template; other grants and the exact published image digest were preserved.",
                "The test proves behavior only in a fresh disposable HAOS/Supervisor lab and does not alter the household manifest.",
                "It does not validate household configuration, camera privacy, hardware acceleration, physical behavior, or production rollback.",
            ],
        }
        (root / "lab-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        log_event(root, "PERMISSION LAB PASS: app started with Core API proxy disabled; Core-to-app API, authenticated ingress, and app-to-Core denial verified.")
    finally:
        console.close()


def run_lab() -> None:
    branch = os.environ.get("GITHUB_REF_NAME")
    if branch == PERMISSION_LAB_BRANCH:
        run_permission_lab()
        return
    if branch != FULL_LAB_BRANCH:
        raise LabFailure("refusing to run outside an explicitly authorized disposable lab branch")
    if os.environ.get("GITHUB_REPOSITORY") != "FernandoFraga86/ha-recording-control":
        raise LabFailure("refusing to run outside the authorized repository")
    root = _run_root()
    validate_distribution_lock()
    log_event(root, "Distribution lock checked; all production/installability gates remain false.")
    verify_registry_tags(root)

    try:
        import pexpect
    except ImportError as err:
        raise LabFailure("runner lacks the pexpect package installed by the lab workflow") from err

    archive = verify_haos_release(root)
    image = extract_haos(archive, root)
    console = HAOSConsole(image, root, pexpect)
    try:
        log_event(root, "Booting disposable HAOS/QEMU guest; no local VM or household network endpoint is used.")
        console.boot_login()
        log_event(root, "HAOS serial console reached the root shell.")
        wait_for_supervisor(console, root)

        # The local Supervisor store is the supported test-only ingress for a package;
        # manifests exist only on the disposable VM and are never added to the public repository root.
        store_local_manifests(console, root)
        previous_info = install_start(console, LOCAL_PREVIOUS_SLUG, root)
        old_container = container_for_image(console, OFFICIAL_IMAGE + ":")
        verify_pulled_image_digest(
            console,
            OFFICIAL_IMAGE + ":" + OFFICIAL_TAG,
            {OFFICIAL_INDEX_DIGEST, OFFICIAL_AMD64_DIGEST},
        )
        marker_in_container(console, old_container[0], "write", root)
        old_state = cli_info(console, LOCAL_PREVIOUS_SLUG).get("state")
        if old_state != "started":
            raise LabFailure("synthetic rollback app was not started before handoff")

        console.json_command(f"ha apps stop {LOCAL_PREVIOUS_SLUG} --no-progress --raw-json", timeout=600)
        wait_for_app_quiesced(console, LOCAL_PREVIOUS_SLUG, old_container[0], root, timeout=300)
        assert_single_writer(console, None)
        log_event(root, "Previous Frigate writer stopped before candidate start; zero overlapping Frigate containers.")

        candidate_info = install_start(console, LOCAL_CANDIDATE_SLUG, root)
        candidate_container = container_for_image(console, GHCR_IMAGE + ":")
        verify_pulled_image_digest(console, GHCR_IMAGE + ":" + GHCR_TAG, {GHCR_DIGEST})
        candidate_info = cli_info(console, LOCAL_CANDIDATE_SLUG)
        candidate_container_data = inspect_container(console, candidate_container[0])
        verify_candidate_metadata(console, candidate_info, candidate_container_data, root)
        assert_single_writer(console, GHCR_IMAGE + ":")
        marker_in_container(console, candidate_container[0], "read", root)
        app_api_from_core(console, candidate_container[0], root)

        # Re-resolve the public tag after Supervisor pulled it to detect tag drift/TOCTOU.
        if registry_manifest_digest(GHCR_IMAGE, GHCR_TAG) != GHCR_DIGEST:
            raise LabFailure("candidate GHCR tag changed during Supervisor installation")
        log_event(root, "Post-pull GHCR tag remains bound to the pinned digest.")

        console.reboot()
        log_event(root, "HAOS guest completed a real host reboot cycle; waiting for Supervisor/app recovery.")
        wait_after_reboot(console, root)
        candidate_after_reboot = cli_info(console, LOCAL_CANDIDATE_SLUG)
        if candidate_after_reboot.get("boot") != "auto":
            raise LabFailure("Supervisor did not retain the lab-only boot:auto setting")
        candidate_after_reboot_container = container_for_image(console, GHCR_IMAGE + ":")
        marker_in_container(console, candidate_after_reboot_container[0], "read", root)
        app_api_from_core(console, candidate_after_reboot_container[0], root)

        console.json_command(f"ha apps stop {LOCAL_CANDIDATE_SLUG} --no-progress --raw-json", timeout=600)
        wait_for_app_quiesced(
            console,
            LOCAL_CANDIDATE_SLUG,
            candidate_after_reboot_container[0],
            root,
            timeout=300,
        )
        assert_single_writer(console, None)
        rollback_info = console.json_command(f"ha apps start {LOCAL_PREVIOUS_SLUG} --no-progress --raw-json", timeout=900)
        old_after_rollback = wait_for_app(console, LOCAL_PREVIOUS_SLUG, "started", timeout=900)
        old_after_rollback_container = container_for_image(console, OFFICIAL_IMAGE + ":")
        assert_single_writer(console, OFFICIAL_IMAGE + ":")
        marker_in_container(console, old_after_rollback_container[0], "read", root)
        log_event(root, "Rollback to the previous official image succeeded with the synthetic media marker preserved and one writer active.")

        console.json_command(f"ha apps stop {LOCAL_PREVIOUS_SLUG} --no-progress --raw-json", timeout=600)
        wait_for_app_quiesced(console, LOCAL_PREVIOUS_SLUG, old_after_rollback_container[0], root, timeout=300)
        assert_single_writer(console, None)

        summary = {
            "status": "passed_supervisor_lab_overlay_only",
            "haos_release": HAOS_VERSION,
            "supervisor_version": SUPERVISOR_VERSION,
            "candidate_image": f"{GHCR_IMAGE}:{GHCR_TAG}",
            "candidate_digest": GHCR_DIGEST,
            "candidate_app_slug": LOCAL_CANDIDATE_SLUG,
            "rollback_image": f"{OFFICIAL_IMAGE}:{OFFICIAL_TAG}",
            "rollback_index_digest": OFFICIAL_INDEX_DIGEST,
            "rollback_app_slug": LOCAL_PREVIOUS_SLUG,
            "candidate_boot": "auto (temporary lab-only manifest; production template remains manual)",
            "synthetic_camera_devices": "omitted because generic QEMU guest has no household GPU/USB/video devices",
            "mqtt": "disabled in synthetic Frigate configuration",
            "camera_feeds": "none",
            "household_endpoints": "none",
            "production_ready": False,
            "installable": False,
            "native_recovery_enabled": False,
            "notices": [
                "Supervisor loaded the app from its built-in local repository; no public repository config was created.",
                "The app manifest maps homeassistant_config rw and grants Home Assistant/Supervisor APIs; test used a clean disposable HAOS instance only.",
                "No real household migration, camera privacy, hardware acceleration, production rollback, or app behavior with household config was validated.",
            ],
        }
        (root / "lab-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        log_event(root, "SUPERVISOR APP LAB PASS: install/start, Core-to-app API, effective metadata/volumes/ports, boot:auto after HAOS reboot, synthetic data preservation, exclusive writer handoff, and rollback.")
    finally:
        console.close()


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "run":
        print("usage: python3 tools/supervisor_app_lab.py run", file=sys.stderr)
        return 2
    try:
        run_lab()
    except Exception as err:
        print(f"Supervisor app lab failed safely: {type(err).__name__}: {err}", file=sys.stderr)
        try:
            root = Path(os.environ.get("RUNNER_TEMP", "/nonexistent")) / "ha-recording-control-supervisor-lab"
            if root.is_dir():
                (root / "lab-summary.json").write_text(
                    json.dumps({"status": "blocked_or_failed", "reason": f"{type(err).__name__}: {err}"}, indent=2) + "\n",
                    encoding="utf-8",
                )
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

