"""Run a synthetic, digest-pinned Core/Mosquitto/Frigate integration lab."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

try:
    from tools.integrated_lab.frigate_version import parse_frigate_version
    from tools.integrated_lab.synthetic_entity import select_synthetic_sensor_state
except ModuleNotFoundError:  # Script entry point places tools/ rather than the repo root on sys.path.
    from integrated_lab.frigate_version import parse_frigate_version
    from integrated_lab.synthetic_entity import select_synthetic_sensor_state


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "distribution-lock.json"
EXPECTED_IMAGES = {
    "HA_IMAGE": "ghcr.io/home-assistant/home-assistant@sha256:d30d46d24d55c43f1122ab04fdf04d2d31144815b983b07feb075f3a18814239",
    "MOSQUITTO_IMAGE": "docker.io/library/eclipse-mosquitto@sha256:22075095e8790eec5592ff3c213b1974cd51aa458275af2f8b0595af4c385df2",
    "FRIGATE_IMAGE": "ghcr.io/fernandofraga86/ha-recording-control@sha256:e024fe700120021c710c08bd28d7e65e5f24442755a0381c028bdf3596ebf7c4",
}


def load_lock() -> dict[str, Any]:
    return json.loads(LOCK_PATH.read_text(encoding="utf-8"))


def lock_image_refs(lock: dict[str, Any]) -> dict[str, str]:
    images = lock["integrated_lab"]["images"]
    return {
        "HA_IMAGE": "ghcr.io/home-assistant/home-assistant@" + images["home_assistant_core"]["linux_amd64_digest"],
        "MOSQUITTO_IMAGE": "docker.io/library/eclipse-mosquitto@" + images["mosquitto"]["linux_amd64_digest"],
        "FRIGATE_IMAGE": images["frigate_experimental"]["tag"].split(":", 1)[0]
        + "@"
        + images["frigate_experimental"]["linux_amd64_digest"],
    }


def validate_lock() -> None:
    lock = load_lock()
    if lock.get("production_ready") is not False or lock.get("installable") is not False:
        raise RuntimeError("the candidate readiness gates must remain false")
    if lock.get("native_recovery_enabled") is not False:
        raise RuntimeError("native recovery must remain disabled")
    if lock.get("integrated_lab", {}).get("status") not in {
        "pending",
        "passed_synthetic_integration_only",
    }:
        raise RuntimeError("integrated lab status is not valid for a synthetic run")
    if lock_image_refs(lock) != EXPECTED_IMAGES:
        raise RuntimeError("digest-pinned image references differ from the reviewed distribution lock")
    for name, ref in EXPECTED_IMAGES.items():
        if os.environ.get(name) != ref:
            raise RuntimeError(f"workflow image pin mismatch: {name}")
        if "@sha256:" not in ref or not re.fullmatch(r".+@sha256:[0-9a-f]{64}", ref):
            raise RuntimeError(f"image reference is not pinned by a SHA-256 digest: {name}")
    print("Distribution lock, false readiness gates, and all three exact image digests verified.")


def docker_result(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=False,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
        encoding="utf-8",
    )


def docker(*args: str, capture: bool = False, check: bool = True) -> str:
    result = docker_result(*args, capture=capture)
    if check and result.returncode != 0:
        output = result.stdout or ""
        raise RuntimeError(f"docker {' '.join(args[:4])} failed (exit {result.returncode}): {output[-4000:]}")
    return (result.stdout or "").strip()


def verify_images() -> None:
    for env_name, reference in EXPECTED_IMAGES.items():
        inspected = json.loads(docker("image", "inspect", reference, capture=True))[0]
        platform = f"{inspected['Os']}/{inspected['Architecture']}"
        if platform != "linux/amd64":
            raise RuntimeError(f"pulled image platform mismatch for {env_name}: {platform}")
        repo_digests = inspected.get("RepoDigests") or []
        digest = reference.rsplit("@", 1)[1]
        if not any(item.endswith("@" + digest) for item in repo_digests):
            raise RuntimeError(f"pulled image does not report the expected digest for {env_name}")
        print(f"Verified linux/amd64 image digest: {env_name}")


def write_lab_configs(root: Path, run_id: str) -> tuple[Path, Path, Path]:
    broker_dir = root / "mosquitto"
    ha_dir = root / "homeassistant"
    frigate_dir = root / "frigate"
    control_dir = root / "control"
    for directory in (broker_dir, ha_dir, frigate_dir, control_dir):
        directory.mkdir(parents=True, exist_ok=False)

    (broker_dir / "mosquitto.conf").write_text(
        "listener 1883 0.0.0.0\n"
        "allow_anonymous true\n"
        "persistence false\n"
        "log_dest stdout\n",
        encoding="utf-8",
    )
    (ha_dir / "configuration.yaml").write_text(
        "homeassistant:\n"
        "  name: HA Recording Control Synthetic Lab\n"
        "  latitude: 0\n"
        "  longitude: 0\n"
        "  elevation: 0\n"
        "  unit_system: metric\n"
        "  time_zone: UTC\n"
        "logger:\n"
        "  default: warning\n",
        encoding="utf-8",
    )
    (frigate_dir / "config.yml").write_text(
        "mqtt:\n"
        "  enabled: true\n"
        "  host: mosquitto\n"
        "  port: 1883\n"
        "  topic_prefix: ha_recording_control_ci\n"
        f"  client_id: ha-recording-control-ci-{run_id}\n"
        "detectors:\n"
        "  cpu:\n"
        "    type: cpu\n"
        "    num_threads: 1\n"
        "cameras: {}\n"
        "record:\n"
        "  enabled: false\n"
        "version: 0.18-0\n",
        encoding="utf-8",
    )
    helper = ROOT / "tools" / "integrated_lab" / "ha_mqtt_smoke.py"
    version_helper = ROOT / "tools" / "integrated_lab" / "frigate_version.py"
    entity_helper = ROOT / "tools" / "integrated_lab" / "synthetic_entity.py"
    if not helper.is_file() or not version_helper.is_file() or not entity_helper.is_file():
        raise RuntimeError("Home Assistant lab helper is missing")
    (control_dir / "ha_mqtt_smoke.py").write_bytes(helper.read_bytes())
    (control_dir / "frigate_version.py").write_bytes(version_helper.read_bytes())
    (control_dir / "synthetic_entity.py").write_bytes(entity_helper.read_bytes())
    return broker_dir, ha_dir, frigate_dir


def validate_runner_temp_root(root: Path, root_base: Path) -> Path:
    if root.is_symlink():
        raise RuntimeError("refusing to clean a symlink as the temporary lab root")
    if not root.exists():
        raise RuntimeError("refusing to clean a missing temporary lab root")
    resolved_base = root_base.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    if (
        resolved_root.parent != resolved_base
        or not resolved_root.name.startswith("ha-rc-integrated-lab-")
        or not resolved_root.is_dir()
    ):
        raise RuntimeError("refusing to clean a path outside this run's designated temporary lab root")
    return resolved_root


def remove_runner_temp_root(root: Path, root_base: Path) -> None:
    """Reclaim container-created files only inside this run's checked temp directory."""
    resolved_root = validate_runner_temp_root(root, root_base)
    if sys.platform != "linux" or not hasattr(os, "getuid") or not shutil.which("sudo"):
        raise RuntimeError("cannot safely reclaim container-owned temporary files on this runner")

    owner = f"{os.getuid()}:{os.getgid()}"
    for command in (
        ["sudo", "chown", "--recursive", "--no-dereference", "--preserve-root", "--", owner, str(resolved_root)],
        ["sudo", "chmod", "--recursive", "u+rwX", "--", str(resolved_root)],
    ):
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise RuntimeError("could not reclaim permissions in the checked runner temporary directory")
    shutil.rmtree(resolved_root)


def inspect_container(name: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "inspect", name],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not inspect lab container {name}: {(result.stdout or '')[-500:]}")
    inspected = json.loads(result.stdout or "")
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise RuntimeError(f"unexpected inspect result for lab container {name}")
    return inspected[0]


def inspect_optional(kind: str, name: str) -> dict[str, Any] | None:
    if kind not in {"container", "network"}:
        raise ValueError("unsupported lab resource kind")
    result = subprocess.run(
        ["docker", "inspect", name] if kind == "container" else ["docker", "network", "inspect", name],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    output = result.stdout or ""
    if result.returncode != 0:
        missing_container = kind == "container" and (
            "No such container" in output or "No such object" in output
        )
        missing_network = kind == "network" and (
            "No such network" in output or "No such object" in output or f"network {name} not found" in output
        )
        if missing_container or missing_network:
            return None
        raise RuntimeError(f"could not inspect lab {kind} {name}: {output[-500:]}")
    inspected = json.loads(output)
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise RuntimeError(f"unexpected inspect result for lab {kind} {name}")
    return inspected[0]


def require_running(name: str) -> None:
    state = inspect_container(name)["State"]
    if not state.get("Running"):
        logs = docker("logs", "--tail", "80", name, capture=True, check=False)
        raise RuntimeError(f"container {name} exited unexpectedly; logs:\n{logs[-5000:]}")


def verify_container_isolated(name: str, network: str) -> None:
    inspected = inspect_container(name)
    bindings = inspected.get("HostConfig", {}).get("PortBindings")
    if bindings:
        raise RuntimeError(f"container {name} unexpectedly has host port bindings")
    attached = set((inspected.get("NetworkSettings", {}).get("Networks") or {}).keys())
    if attached != {network}:
        raise RuntimeError(f"container {name} is attached to an unexpected network: {sorted(attached)}")


def wait_phase(path: Path, expected: str, timeout: int, description: str) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                time.sleep(0.5)
                continue
            if data.get("phase") == "failed":
                diagnostic = data.get("diagnostic")
                detail = f"; diagnostic={diagnostic}" if isinstance(diagnostic, str) and diagnostic else ""
                raise RuntimeError(
                    f"Home Assistant lab helper failed at {data.get('step', 'unknown')}: "
                    f"{data.get('error_type', 'unknown error')}{detail}"
                )
            if data.get("phase") == expected:
                return data
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {description}")


def wait_frigate_api(name: str, timeout: int = 240) -> dict[str, Any]:
    probe = (
        "import json,urllib.request; "
        "r=urllib.request.urlopen('http://127.0.0.1:5000/api/version',timeout=4); "
        "print(json.dumps(r.read().decode('utf-8').strip()))"
    )
    deadline = time.monotonic() + timeout
    last = "not yet available"
    while time.monotonic() < deadline:
        require_running(name)
        result = docker_result("exec", name, "python3", "-c", probe, capture=True)
        if result.returncode == 0:
            try:
                version = parse_frigate_version(json.loads(result.stdout))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise RuntimeError("Frigate API returned an unexpected version response") from error
            return {"version": version}
        last = (result.stdout or "")[-500:]
        time.sleep(3)
    raise RuntimeError(f"Frigate /api/version did not become ready: {last}")


def publish_synthetic_mqtt_from_frigate(name: str, run_id: str) -> None:
    script = r'''import json, sys
import paho.mqtt.client as mqtt
run_id = sys.argv[1]
slug = "ha_rc_ci_" + run_id
state_topic = "ha_recording_control_ci/synthetic/" + run_id + "/state"
config_topic = "homeassistant/sensor/" + slug + "/config"
payload = {
    "name": "CI Frigate MQTT Probe " + run_id,
    "unique_id": slug,
    "state_topic": state_topic,
    "device": {"identifiers": [slug], "name": "Synthetic Frigate CI publisher"}
}
client = mqtt.Client(client_id="ha-rc-ci-pub-" + run_id, protocol=mqtt.MQTTv311)
client.connect("mosquitto", 1883, 20)
client.loop_start()
try:
    for topic, body in ((config_topic, json.dumps(payload)), (state_topic, "synthetic_event_received")):
        result = client.publish(topic, body, qos=1, retain=True)
        result.wait_for_publish(timeout=12)
        if not result.is_published():
            raise RuntimeError("MQTT QoS 1 publication did not complete")
finally:
    client.disconnect()
    client.loop_stop()
print("synthetic_config_and_state_published_from_frigate_container")
'''
    result = subprocess.run(
        ["docker", "exec", "-i", name, "python3", "-", run_id],
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        encoding="utf-8",
    )
    if result.returncode != 0 or "synthetic_config_and_state_published_from_frigate_container" not in result.stdout:
        raise RuntimeError(f"synthetic MQTT publish from Frigate container failed: {(result.stdout or '')[-2500:]}")
    print("Frigate-container synthetic MQTT discovery and state published with QoS 1.")


def probe_ha_auth_from_frigate(name: str) -> None:
    script = """import urllib.error, urllib.request
try:
    urllib.request.urlopen('http://homeassistant:8123/api/', timeout=6)
    raise SystemExit('unauthenticated HA API unexpectedly succeeded')
except urllib.error.HTTPError as error:
    print(error.code)
    raise SystemExit(0 if error.code in (401, 403) else 1)
"""
    result = docker_result("exec", name, "python3", "-c", script, capture=True)
    if result.returncode != 0 or result.stdout.strip() not in {"401", "403"}:
        raise RuntimeError(f"Frigate-to-HA API reachability/auth probe failed: {(result.stdout or '')[-1000:]}")
    print(f"Frigate container reached the Home Assistant API; unauthenticated request correctly received HTTP {result.stdout.strip()}.")


def stop_and_remove(names: list[str], network: str, *, require_clean: bool) -> list[str]:
    errors: list[str] = []
    for name in names:
        inspected = inspect_optional("container", name)
        if inspected is None:
            continue
        state = inspected["State"]
        if state.get("Running"):
            stop_started = time.monotonic()
            result = docker_result("stop", "--time", "30", name, capture=True)
            stop_elapsed = time.monotonic() - stop_started
            if result.returncode != 0:
                errors.append(f"could not stop {name} cleanly")
            if stop_elapsed >= 29:
                errors.append(f"container {name} approached the forced-stop timeout")
        inspected = inspect_optional("container", name)
        if inspected is not None:
            status = inspected["State"]
            if status.get("Running") or status.get("Status") != "exited" or status.get("OOMKilled"):
                errors.append(f"container {name} did not reach exited state")
            docker("rm", "--force", name, check=False)
        if inspect_optional("container", name) is not None:
            errors.append(f"container {name} remained after cleanup")
    docker("network", "rm", network, check=False)
    if inspect_optional("network", network) is not None:
        errors.append("temporary internal network remained after cleanup")
    if errors and require_clean:
        raise RuntimeError("clean shutdown/cleanup failed: " + "; ".join(errors))
    return errors


def run_lab() -> None:
    validate_lock()
    verify_images()
    run_id = re.sub(r"[^a-z0-9]", "", os.environ.get("GITHUB_RUN_ID", "local").lower())[-20:]
    attempt = re.sub(r"[^a-z0-9]", "", os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
    run_id = (run_id or "local") + "a" + (attempt or "1")
    network = "ha-rc-lab-net-" + run_id
    broker = "ha-rc-lab-mqtt-" + run_id
    ha = "ha-rc-lab-core-" + run_id
    frigate = "ha-rc-lab-frigate-" + run_id
    names = [frigate, ha, broker]
    root_base = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
    root = Path(tempfile.mkdtemp(prefix="ha-rc-integrated-lab-", dir=root_base)).resolve()
    if root.parent != root_base or not root.name.startswith("ha-rc-integrated-lab-"):
        raise RuntimeError("temporary lab root escaped the runner's designated temp directory")
    broker_dir, ha_dir, frigate_dir = write_lab_configs(root, run_id)
    control_dir = root / "control"
    success = False
    try:
        docker("network", "create", "--driver", "bridge", "--internal", network)
        network_info = json.loads(docker("network", "inspect", network, capture=True))[0]
        if network_info.get("Internal") is not True or network_info.get("Driver") != "bridge":
            raise RuntimeError("Docker did not establish the required internal bridge network")

        docker(
            "run", "--detach", "--name", broker, "--network", network,
            "--network-alias", "mosquitto",
            "--mount", f"type=bind,source={broker_dir / 'mosquitto.conf'},target=/mosquitto/config/mosquitto.conf,readonly",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            os.environ["MOSQUITTO_IMAGE"], "mosquitto", "-c", "/mosquitto/config/mosquitto.conf",
        )
        require_running(broker)
        verify_container_isolated(broker, network)
        docker("exec", broker, "mosquitto_pub", "-h", "127.0.0.1", "-t", "ha_rc_ci/health", "-q", "1", "-m", "ready")
        print("Mosquitto started on the isolated internal network and accepted a synthetic QoS 1 probe.")

        docker(
            "run", "--detach", "--name", ha, "--network", network,
            "--network-alias", "homeassistant", "--shm-size=256m",
            "--memory=3g", "--cpus=1.5",
            "--mount", f"type=bind,source={ha_dir},target=/config",
            "--mount", f"type=bind,source={control_dir},target=/lab-control",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
            os.environ["HA_IMAGE"],
        )
        require_running(ha)
        verify_container_isolated(ha, network)
        docker("exec", "--detach", ha, "python3", "/lab-control/ha_mqtt_smoke.py", run_id)
        status_path = control_dir / "ha-result.json"
        wait_phase(status_path, "mqtt_ready", 300, "the Home Assistant MQTT config flow")
        print("Home Assistant Core completed disposable onboarding and loaded its real MQTT integration against the lab broker.")

        log_path = "/tmp/ha-recording-control-frigate-availability"
        docker(
            "exec", "-d", broker, "sh", "-c",
            f"rm -f {log_path}; mosquitto_sub -h 127.0.0.1 -t ha_recording_control_ci/available -C 1 -W 100 > {log_path} 2>&1",
        )
        time.sleep(1)
        docker(
            "run", "--detach", "--name", frigate, "--network", network,
            "--network-alias", "frigate", "--shm-size=256m",
            "--memory=4g", "--cpus=2.5",
            "--mount", f"type=bind,source={frigate_dir},target=/config",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
            "--tmpfs", "/media/frigate:rw,noexec,nosuid,size=512m",
            os.environ["FRIGATE_IMAGE"],
        )
        require_running(frigate)
        verify_container_isolated(frigate, network)
        network_containers = json.loads(docker("network", "inspect", network, capture=True))[0].get("Containers") or {}
        if {entry.get("Name") for entry in network_containers.values()} != {broker, ha, frigate}:
            raise RuntimeError("isolated network contains an unexpected or missing service container")
        frigate_version = wait_frigate_api(frigate)
        probe_ha_auth_from_frigate(frigate)
        print(f"Frigate API responded over the private network; version={frigate_version.get('version')}.")

        availability_deadline = time.monotonic() + 110
        while time.monotonic() < availability_deadline:
            require_running(frigate)
            result = docker_result("exec", broker, "sh", "-c", f"test -s {log_path}", capture=True)
            if result.returncode == 0:
                availability = docker("exec", broker, "cat", log_path, capture=True)
                if availability.strip() != "online":
                    raise RuntimeError(f"Frigate MQTT availability payload unexpected: {availability!r}")
                print("Observed Frigate's actual MQTT availability message from the running service.")
                break
            time.sleep(1)
        else:
            logs = docker("exec", broker, "cat", log_path, capture=True, check=False)
            raise RuntimeError(f"did not observe Frigate availability on the broker: {logs[-500:]}")

        publish_synthetic_mqtt_from_frigate(frigate, run_id)
        final = wait_phase(status_path, "passed", 100, "the synthetic MQTT discovery/state event in Home Assistant")
        if final.get("frigate_api_version") != frigate_version.get("version"):
            raise RuntimeError("Home Assistant-side Frigate API observation disagreed with the running service")
        print("Home Assistant's MQTT integration created the synthetic entity and observed its expected state.")

        stop_log = "/tmp/ha-recording-control-frigate-stop"
        docker(
            "exec", "-d", broker, "sh", "-c",
            f"rm -f {stop_log}; mosquitto_sub -d -v -h 127.0.0.1 -t ha_recording_control_ci/available -C 2 -W 20 > {stop_log} 2>&1",
        )
        subscription_deadline = time.monotonic() + 10
        while time.monotonic() < subscription_deadline:
            subscription = docker(
                "exec", broker, "sh", "-c",
                f"grep -F 'ha_recording_control_ci/available' {stop_log}",
                capture=True, check=False,
            )
            if subscription:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("shutdown observer did not subscribe to Frigate availability")
        stop_started = time.monotonic()
        docker("stop", "--time", "30", frigate)
        stop_elapsed = time.monotonic() - stop_started
        frigate_state = inspect_container(frigate)["State"]
        if frigate_state.get("Running") or frigate_state.get("Status") != "exited" or frigate_state.get("OOMKilled") or stop_elapsed >= 29:
            raise RuntimeError("Frigate did not stop gracefully")
        stop_deadline = time.monotonic() + 25
        stop_observation = ""
        while time.monotonic() < stop_deadline:
            stop_observation = docker("exec", broker, "cat", stop_log, capture=True, check=False)
            if any(
                line.endswith("ha_recording_control_ci/available stopped")
                for line in stop_observation.splitlines()
            ):
                break
            time.sleep(0.5)
        if not any(
            line.endswith("ha_recording_control_ci/available stopped")
            for line in stop_observation.splitlines()
        ):
            raise RuntimeError(f"Frigate clean-stop availability was not observed: {stop_observation[-1000:]!r}")
        print("Frigate stopped cleanly and published its documented stopped availability to the broker.")
        success = True
    except Exception:
        for name in names:
            logs = docker("logs", "--tail", "80", name, capture=True, check=False)
            if logs:
                print(f"Recent logs from {name} before cleanup:\n{logs[-5000:]}", file=sys.stderr)
        raise
    finally:
        cleanup_failure: Exception | None = None
        try:
            cleanup_errors = stop_and_remove(names, network, require_clean=success)
            if cleanup_errors:
                print("Cleanup warnings: " + "; ".join(cleanup_errors), file=sys.stderr)
        except Exception as error:
            cleanup_failure = error
        try:
            remove_runner_temp_root(root, root_base)
        except Exception as error:
            cleanup_failure = cleanup_failure or error
        if cleanup_failure is not None:
            if success:
                raise cleanup_failure
            print(f"Temporary lab cleanup warning: {type(cleanup_failure).__name__}: {cleanup_failure}", file=sys.stderr)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(
                "\n### Integrated synthetic service lab: PASS\n\n"
                "- All three images were pulled by reviewed linux/amd64 SHA-256 digest.\n"
                "- Core 2026.10.2 configured its real MQTT integration to the isolated Mosquitto broker.\n"
                "- Frigate API responded; HA reached the Frigate API; unauthenticated Frigate-to-HA API access was rejected.\n"
                "- Frigate's own availability message reached Mosquitto; a separate synthetic MQTT discovery/state message published from the Frigate container became a HA entity.\n"
                "- Frigate emitted stopped on graceful shutdown; all containers and the internal network were removed.\n"
                "- No cameras, household settings, external service endpoints, or physical devices were used. This is not Supervisor installation, production privacy, or recovery validation.\n"
            )
    print("INTEGRATED_SYNTHETIC_SERVICE_LAB=PASS; Supervisor/production validation remains pending.")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"validate-lock", "verify-images", "run"}:
        print("usage: integrated_service_lab.py {validate-lock|verify-images|run}", file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == "validate-lock":
            validate_lock()
        elif sys.argv[1] == "verify-images":
            verify_images()
        else:
            run_lab()
    except Exception as err:  # noqa: BLE001 - CI must fail closed and keep error concise.
        print(f"INTEGRATED_SYNTHETIC_SERVICE_LAB=FAIL: {type(err).__name__}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
