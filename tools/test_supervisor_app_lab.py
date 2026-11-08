"""Focused offline contract checks for the temporary HAOS/Supervisor lab harness."""

from __future__ import annotations

import importlib.util
import base64
import inspect
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("supervisor_app_lab", ROOT / "tools" / "supervisor_app_lab.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class SupervisorAppLabTests(unittest.TestCase):
    def test_manual_stop_quiescence_requires_successful_job_and_no_live_container(self) -> None:
        self.assertTrue(module.stop_quiescence_confirmed(
            stop_job_succeeded=True,
            supervisor_state="stopped",
            container_state=None,
            active_frigate_writers=False,
        ))
        self.assertTrue(module.stop_quiescence_confirmed(
            stop_job_succeeded=True,
            supervisor_state="error",
            container_state=None,
            active_frigate_writers=False,
        ))
        self.assertTrue(module.stop_quiescence_confirmed(
            stop_job_succeeded=True,
            supervisor_state="error",
            container_state={"Status": "exited", "ExitCode": 143, "OOMKilled": False, "Error": ""},
            active_frigate_writers=False,
        ))
        self.assertFalse(module.stop_quiescence_confirmed(
            stop_job_succeeded=False,
            supervisor_state="error",
            container_state=None,
            active_frigate_writers=False,
        ))
        self.assertFalse(module.stop_quiescence_confirmed(
            stop_job_succeeded=True,
            supervisor_state="error",
            container_state={"Status": "exited", "ExitCode": 1, "OOMKilled": False, "Error": ""},
            active_frigate_writers=False,
        ))
        self.assertFalse(module.stop_quiescence_confirmed(
            stop_job_succeeded=True,
            supervisor_state="error",
            container_state={"Status": "running", "ExitCode": 0, "OOMKilled": False, "Error": ""},
            active_frigate_writers=False,
        ))
        self.assertFalse(module.stop_quiescence_confirmed(
            stop_job_succeeded=True,
            supervisor_state="error",
            container_state={"Status": "exited", "ExitCode": 143, "OOMKilled": True, "Error": ""},
            active_frigate_writers=False,
        ))
        self.assertFalse(module.stop_quiescence_confirmed(
            stop_job_succeeded=True,
            supervisor_state="error",
            container_state=None,
            active_frigate_writers=True,
        ))

    def test_guest_write_splits_base64_into_short_independently_decodable_commands(self) -> None:
        class RecordingConsole:
            def __init__(self) -> None:
                self.commands: list[str] = []

            def run(self, command: str, timeout: int = 180) -> str:
                self.commands.append(command)
                return ""

        console = RecordingConsole()
        payload = "manifest: " + "x" * 4200 + "\n"
        module.guest_write(console, "/mnt/data/supervisor/apps/local/lab/config.yaml", payload)

        self.assertGreater(len(console.commands), 1)
        self.assertTrue(all(len(command) < 1024 for command in console.commands))
        self.assertIn(" > /mnt/data/supervisor/apps/local/lab/config.yaml", console.commands[0])
        self.assertTrue(all(" >> /mnt/data/supervisor/apps/local/lab/config.yaml" in command for command in console.commands[1:]))

        encoded_chunks = []
        for command in console.commands:
            match = re.search(r"printf '%s' '([A-Za-z0-9+/=]*)'", command)
            self.assertIsNotNone(match)
            encoded_chunks.append(match.group(1))
        self.assertEqual(base64.b64decode("".join(encoded_chunks)).decode("utf-8"), payload)

    def test_guest_write_refuses_paths_outside_disposable_supervisor_data(self) -> None:
        class RecordingConsole:
            def run(self, command: str, timeout: int = 180) -> str:
                raise AssertionError("must validate path before sending guest commands")

        with self.assertRaises(module.LabFailure):
            module.guest_write(RecordingConsole(), "/config/configuration.yaml", "not allowed")

    def test_candidate_manifest_comes_from_template_and_only_overrides_lab_hardware_and_boot(self) -> None:
        original = module.TEMPLATE_PATH.read_text(encoding="utf-8")
        candidate = module.candidate_lab_manifest(original)
        self.assertIn("boot: auto", candidate)
        self.assertIn("devices: []", candidate)
        self.assertIn("usb: false", candidate)
        self.assertIn("video: false", candidate)
        self.assertIn(f'image: "{module.GHCR_IMAGE}"', candidate)
        self.assertIn(f'version: "{module.GHCR_TAG}"', candidate)
        self.assertIn("homeassistant_api: true", candidate)
        self.assertIn("hassio_api: true", candidate)
        self.assertIn("full_access: false", candidate)
        self.assertIn('"homeassistant_config:rw"', candidate)
        self.assertIn('"media:rw"', candidate)
        self.assertNotIn("boot: manual", candidate)

    def test_permission_lab_changes_only_the_core_api_proxy_grant(self) -> None:
        original = module.TEMPLATE_PATH.read_text(encoding="utf-8")
        ordinary = module.candidate_lab_manifest(original)
        reduced = module.candidate_lab_manifest(original, homeassistant_api=False)
        self.assertEqual(
            reduced,
            ordinary.replace("homeassistant_api: true", "homeassistant_api: false", 1),
        )
        self.assertEqual(reduced.count("homeassistant_api: false"), 1)
        self.assertIn("hassio_api: true", reduced)
        self.assertIn("ingress: true", reduced)
        self.assertIn("full_access: false", reduced)
        self.assertIn(f'image: "{module.GHCR_IMAGE}"', reduced)
        self.assertIn(f'version: "{module.GHCR_TAG}"', reduced)

    def test_permission_manifest_reload_uses_effective_store_readback(self) -> None:
        source = inspect.getsource(module.store_permission_lab_manifest)
        self.assertIn('console.run("ha store reload --no-progress"', source)
        self.assertIn('info.get("available") is not True', source)
        self.assertIn('info.get("homeassistant_api") is not False', source)
        self.assertNotIn('"Command completed successfully."', source)

    def test_permission_lab_app_info_parser_handles_terminal_noise_and_ansi(self) -> None:
        output = (
            "ha > \x1b[32m{\"result\":\"ok\",\"data\":"
            "{\"slug\":\"local_recording_control\",\"available\":true,"
            "\"homeassistant_api\":false}}\x1b[0m\r\n"
            "prompt> "
        )
        self.assertEqual(
            module.parse_permission_lab_app_info(output),
            {
                "slug": "local_recording_control",
                "available": True,
                "homeassistant_api": False,
            },
        )

    def test_permission_lab_app_info_parser_fails_closed_on_non_ok_or_missing_json(self) -> None:
        with self.assertRaises(module.LabFailure):
            module.parse_permission_lab_app_info('{"result":"error","data":{}}')
        with self.assertRaises(module.LabFailure):
            module.parse_permission_lab_app_info("Supervisor returned no structured app info")

    def test_permission_lab_cli_diagnostic_records_shape_not_response_values(self) -> None:
        output = (
            '{"result":"ok","data":{"slug":"local_recording_control",'
            '"options":{"password":"never-log-this"}}}\n__HA_RC_COMMAND_RC__0\n'
        )
        diagnostic = module.permission_lab_cli_shape(output)
        self.assertIn("exit_code=0", diagnostic)
        self.assertIn("root_keys=['data', 'result']", diagnostic)
        self.assertIn("data_keys=['options', 'slug']", diagnostic)
        self.assertNotIn("never-log-this", diagnostic)

    def test_official_supervisor_api_probe_is_read_only_and_returns_allowlisted_fields(self) -> None:
        source = module.supervisor_app_info_probe_source("local_recording_control")
        compile(source, "supervisor-app-info-probe", "exec")
        self.assertIn("http://supervisor/addons/", source)
        self.assertIn('os.environ.get("SUPERVISOR_TOKEN")', source)
        self.assertIn('"ingress_entry"', source)
        self.assertNotIn('print(token)', source)
        self.assertNotIn("options", source)
        self.assertEqual(
            module.parse_supervisor_app_info_probe(
                'root@haos: docker exec ... "print(\\"LAB_SUPERVISOR_APP_INFO=\\")"\n'
                'LAB_SUPERVISOR_APP_INFO={"slug":"local_recording_control",'
                '"available":true,"homeassistant_api":false}\n'
            ),
            {
                "slug": "local_recording_control",
                "available": True,
                "homeassistant_api": False,
            },
        )
        with self.assertRaises(module.LabFailure):
            module.parse_supervisor_app_info_probe("LAB_SUPERVISOR_APP_INFO_ERROR status=404")

    def test_permission_lab_has_positive_core_and_ingress_probes_and_no_reboot(self) -> None:
        source = inspect.getsource(module.run_permission_lab)
        self.assertIn("homeassistant_api=False", source)
        self.assertIn("app_api_from_core", source)
        self.assertIn("app_core_api_denied", source)
        self.assertIn("ingress_to_app_api", source)
        self.assertIn("verify_pulled_image_digest", source)
        self.assertNotIn("console.reboot()", source)
        self.assertNotIn("LOCAL_PREVIOUS_SLUG", source)

    def test_permission_checks_do_not_log_core_token_or_ingress_token(self) -> None:
        source = (
            inspect.getsource(module.core_api_permission_probe_source)
            + inspect.getsource(module.app_core_api_denied)
            + inspect.getsource(module.ingress_to_app_api)
            + inspect.getsource(module.ingress_probe_source)
        )
        self.assertIn("SUPERVISOR_TOKEN", source)
        self.assertNotIn("log_event(root, token", source)
        self.assertNotIn("print(access", source)
        self.assertIn("HTTP 401", inspect.getsource(module.app_core_api_denied))

    def test_permission_probe_sources_are_valid_python_and_fit_guest_shell(self) -> None:
        core_probe = module.core_api_permission_probe_source()
        ingress_probe = module.ingress_probe_source("/api/hassio_ingress/lab_token/api/version")
        compile(core_probe, "core-api-permission-probe", "exec")
        compile(ingress_probe, "ingress-permission-probe", "exec")
        self.assertLess(len("docker exec candidate python3 -c " + __import__("shlex").quote(core_probe)), 1024)
        self.assertIn("guest_write(console, guest_script, python)", inspect.getsource(module.ingress_to_app_api))
        self.assertIn("docker cp", inspect.getsource(module.ingress_to_app_api))

    def test_bad_or_drifted_template_fails_closed(self) -> None:
        with self.assertRaises(module.LabFailure):
            module.candidate_lab_manifest("name: incomplete\n")

    def test_rollback_peer_uses_the_locked_official_image_and_manual_boot(self) -> None:
        manifest = module.previous_lab_manifest()
        self.assertIn(f'image: "{module.OFFICIAL_IMAGE}"', manifest)
        self.assertIn(f'version: "{module.OFFICIAL_TAG}"', manifest)
        self.assertIn("boot: manual", manifest)
        self.assertIn("legacy: true", manifest)
        self.assertIn('"media:rw"', manifest)
        self.assertNotIn("homeassistant_api: true", manifest)

    def test_false_readiness_flags_and_digest_pins_are_locked(self) -> None:
        module.validate_distribution_lock()
        self.assertTrue(module.HAOS_ASSET_SHA256.startswith("756ec485"))
        self.assertTrue(module.GHCR_DIGEST.startswith("sha256:e024fe700120021c"))
        self.assertEqual(module.SUPERVISOR_VERSION, "2026.10.5")

    def test_recorded_supervisor_lab_pass_keeps_production_gates_closed(self) -> None:
        lock = module.load_lock()
        lab = lock["supervisor_lab"]
        self.assertEqual(lab["status"], "passed_disposable_haos_lab_only")
        self.assertEqual(lab["workflow_run_id"], "42435376634")
        self.assertEqual(lab["workflow_commit"], "05caf1ea52f95af17ee604c672ad85323a9b8c4d")
        self.assertEqual(lab["candidate_digest"], module.GHCR_DIGEST)
        self.assertTrue(lab["artifact_sha256"].startswith("sha256:"))
        self.assertTrue(lab["synthetic_marker_survived_reboot_and_rollback"])
        self.assertTrue(lab["single_writer_handoff_verified"])
        self.assertIs(lab["production_ready"], False)
        self.assertIs(lab["installable"], False)
        self.assertIs(lab["native_recovery_enabled"], False)
        self.assertIs(lock["production_ready"], False)
        self.assertIs(lock["installable"], False)
        self.assertIs(lock["native_recovery_enabled"], False)
        self.assertNotIn("target Supervisor load/install validation pending", lock["blocking_gates"])

    def test_workflow_is_branch_scoped_and_has_capacity_gate_before_apt(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "supervisor-app-lab.yml").read_text(encoding="utf-8")
        self.assertIn("codex/supervisor-app-lab-2026-10-5", workflow)
        self.assertIn(module.PERMISSION_LAB_BRANCH, workflow)
        self.assertLess(workflow.index("Preflight remote runner"), workflow.index("apt-get update"))
        self.assertIn("/dev/kvm", workflow)
        self.assertIn("40 * 1024 * 1024 * 1024", workflow)

    def test_qemu_guest_reboot_is_not_configured_to_exit_the_vm(self) -> None:
        source = (ROOT / "tools" / "supervisor_app_lab.py").read_text(encoding="utf-8")
        self.assertNotIn('"-no-reboot"', source)
        self.assertIn("console.reboot()", source)

    def test_production_template_stays_manual_and_false_gates_are_not_changed(self) -> None:
        template = module.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn("boot: manual", template)
        self.assertNotIn("boot: auto", template)
        lock = module.load_lock()
        self.assertIs(lock["production_ready"], False)
        self.assertIs(lock["installable"], False)
        self.assertIs(lock["native_recovery_enabled"], False)

    def test_store_reload_uses_success_text_then_verifies_store_entries(self) -> None:
        source = (ROOT / "tools" / "supervisor_app_lab.py").read_text(encoding="utf-8")
        self.assertIn('console.run("ha store reload --no-progress", timeout=180)', source)
        self.assertIn('"Command completed successfully."', source)
        self.assertIn('info.get("available") is not True', source)
        self.assertIn('info.get("version_latest") != version', source)
        self.assertNotIn('console.json_command("ha store reload --no-progress --raw-json"', source)

    def test_supervisor_ingress_metadata_uses_effective_token_route_without_logging_token(self) -> None:
        info = {
            "webui": "http://[HOST]:None/",
            "ingress_entry": "/api/hassio_ingress/lab-token-placeholder",
            "ingress_url": "/api/hassio_ingress/lab-token-placeholder//",
        }
        self.assertTrue(module.ingress_metadata_valid(info))
        info["ingress_url"] = "/api/hassio_ingress/different-token/"
        self.assertFalse(module.ingress_metadata_valid(info))
        info["ingress_entry"] = "/"
        self.assertFalse(module.ingress_metadata_valid(info))

    def test_core_to_app_probe_uses_pinned_frigate_api_version_path(self) -> None:
        source = (ROOT / "tools" / "supervisor_app_lab.py").read_text(encoding="utf-8")
        self.assertIn('"http://{address}:5000/api/version"', source)
        self.assertIn('base+"version"', source)
        self.assertIn('base+"config"', source)
        self.assertIn("Frigate /api/version response", source)
        self.assertNotIn('"http://{address}:5000/version"', source)


if __name__ == "__main__":
    unittest.main()
