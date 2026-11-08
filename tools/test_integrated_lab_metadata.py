"""Guard the published-artifact and integrated-lab metadata contract."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from tools.integrated_service_lab import (
    docker_result,
    inspect_optional,
    validate_runner_temp_root,
    wait_frigate_api,
)
from tools.integrated_lab.frigate_version import parse_frigate_version
from tools.integrated_lab.synthetic_entity import select_synthetic_sensor_state


ROOT = Path(__file__).resolve().parents[1]


class IntegratedLabMetadataTests(unittest.TestCase):
    def test_frigate_version_parser_accepts_the_official_plain_text_api_body(self) -> None:
        self.assertEqual(parse_frigate_version("0.18.2\n"), "0.18.2")

    def test_frigate_version_parser_rejects_json_and_unexpected_bodies(self) -> None:
        for body in ('{"version":"0.18.2"}', "0.19.0", "not ready"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_frigate_version(body)

    def test_synthetic_entity_match_accepts_ha_device_prefix_but_rejects_ambiguity(self) -> None:
        run_id = "42427283360a1"
        state = {
            "entity_id": "sensor.synthetic_frigate_ci_publisher_ci_frigate_mqtt_probe_" + run_id,
            "state": "synthetic_event_received",
            "attributes": {"friendly_name": "Synthetic Frigate CI Publisher CI Frigate MQTT Probe"},
        }
        self.assertEqual(select_synthetic_sensor_state([state], run_id), "synthetic_event_received")
        self.assertIsNone(select_synthetic_sensor_state([state, state.copy()], run_id))
        self.assertIsNone(select_synthetic_sensor_state([], run_id))

    @patch("tools.integrated_service_lab.require_running")
    @patch("tools.integrated_service_lab.docker_result")
    def test_runner_frigate_probe_accepts_plain_text_api_response(self, run, require_running) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = '"0.18.2"\n'
        self.assertEqual(wait_frigate_api("synthetic-frigate", timeout=1), {"version": "0.18.2"})
        require_running.assert_called_once_with("synthetic-frigate")

    def test_temp_cleanup_accepts_only_a_direct_child_with_the_run_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            valid = base / "ha-rc-integrated-lab-test"
            valid.mkdir()
            self.assertEqual(validate_runner_temp_root(valid, base), valid.resolve())

            with self.assertRaises(RuntimeError):
                validate_runner_temp_root(base, base)
            with self.assertRaises(RuntimeError):
                validate_runner_temp_root(base / "unrelated", base)

    @patch("tools.integrated_service_lab.subprocess.run")
    def test_cleanup_inspection_treats_a_missing_container_as_absent(self, run) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = "Error response from daemon: No such container: lab-only"
        self.assertIsNone(inspect_optional("container", "lab-only"))

    @patch("tools.integrated_service_lab.subprocess.run")
    def test_cleanup_inspection_treats_a_missing_network_as_absent(self, run) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = "Error response from daemon: network lab-only not found"
        self.assertIsNone(inspect_optional("network", "lab-only"))

    @patch("tools.integrated_service_lab.subprocess.run")
    def test_cleanup_inspection_fails_closed_on_unrecognized_inspect_error(self, run) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = "Docker daemon unavailable"
        with self.assertRaises(RuntimeError):
            inspect_optional("container", "lab-only")

    @patch("tools.integrated_service_lab.subprocess.run")
    def test_docker_result_preserves_exit_status_for_callers(self, run) -> None:
        run.return_value.returncode = 7
        run.return_value.stdout = "synthetic diagnostic"
        result = docker_result("version", capture=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "synthetic diagnostic")

    def test_published_artifact_metadata_is_synchronized_and_non_installable(self) -> None:
        lock = json.loads((ROOT / "distribution-lock.json").read_text(encoding="utf-8"))
        provenance = json.loads((ROOT / "SOURCE-PROVENANCE.json").read_text(encoding="utf-8"))
        image = lock["image"]
        source_image = provenance["image"]

        self.assertTrue(image["published"])
        self.assertTrue(source_image["published"])
        self.assertEqual(image["platform_digest_amd64"], source_image["oci_digest"])
        self.assertEqual(image["workflow_commit"], source_image["publication_workflow_commit"])
        self.assertEqual(image["publication_run_id"], source_image["publication_run_id"])
        self.assertEqual(
            image["platform_digest_amd64"],
            "sha256:e024fe700120021c710c08bd28d7e65e5f24442755a0381c028bdf3596ebf7c4",
        )
        self.assertIsNone(image["oci_index_digest"])
        self.assertFalse(lock["installable"])
        self.assertFalse(lock["production_ready"])
        self.assertFalse(lock["native_recovery_enabled"])
        self.assertFalse(source_image["installable"])
        self.assertFalse(source_image["production_ready"])
        self.assertFalse(source_image["native_recovery_enabled"])

    def test_integrated_lab_images_are_pinned_identically_in_lock_and_provenance(self) -> None:
        lock = json.loads((ROOT / "distribution-lock.json").read_text(encoding="utf-8"))
        provenance = json.loads((ROOT / "SOURCE-PROVENANCE.json").read_text(encoding="utf-8"))
        refs = {
            "HA_IMAGE": "ghcr.io/home-assistant/home-assistant@"
            + lock["integrated_lab"]["images"]["home_assistant_core"]["linux_amd64_digest"],
            "MOSQUITTO_IMAGE": "docker.io/library/eclipse-mosquitto@"
            + lock["integrated_lab"]["images"]["mosquitto"]["linux_amd64_digest"],
            "FRIGATE_IMAGE": lock["integrated_lab"]["images"]["frigate_experimental"]["tag"].split(":", 1)[0]
            + "@"
            + lock["integrated_lab"]["images"]["frigate_experimental"]["linux_amd64_digest"],
        }
        expected = {
            "HA_IMAGE": "ghcr.io/home-assistant/home-assistant@sha256:d30d46d24d55c43f1122ab04fdf04d2d31144815b983b07feb075f3a18814239",
            "MOSQUITTO_IMAGE": "docker.io/library/eclipse-mosquitto@sha256:22075095e8790eec5592ff3c213b1974cd51aa458275af2f8b0595af4c385df2",
            "FRIGATE_IMAGE": "ghcr.io/fernandofraga86/ha-recording-control@sha256:e024fe700120021c710c08bd28d7e65e5f24442755a0381c028bdf3596ebf7c4",
        }
        self.assertEqual(refs, expected)
        for ref in refs.values():
            self.assertRegex(ref, re.compile(r".+@sha256:[0-9a-f]{64}"))
        provenance_refs = provenance["integrated_lab"]["images"]
        self.assertEqual(provenance_refs["home_assistant_core_linux_amd64"], refs["HA_IMAGE"])
        self.assertEqual(provenance_refs["mosquitto_linux_amd64"], refs["MOSQUITTO_IMAGE"])
        self.assertEqual(provenance_refs["frigate_experimental_linux_amd64"], refs["FRIGATE_IMAGE"])
        self.assertEqual(lock["integrated_lab"]["status"], "passed_synthetic_integration_only")
        self.assertEqual(provenance["integrated_lab"]["status"], "passed_synthetic_integration_only")
        self.assertEqual(lock["integrated_lab"]["workflow_run_id"], "42427714912")
        self.assertEqual(provenance["integrated_lab"]["workflow_run_id"], "42427714912")

    def test_workflow_preflights_before_pulls_and_never_publishes_ports_or_images(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "integrated-service-lab.yml").read_text(
            encoding="utf-8"
        )
        preflight = workflow.index("Preflight runner disk, memory, and CPU before image pulls")
        first_pull = workflow.index('docker pull --platform linux/amd64 "$HA_IMAGE"')
        self.assertLess(preflight, first_pull)
        self.assertIn("20 * 1024 * 1024 * 1024", workflow)
        self.assertIn("8 * 1024 * 1024", workflow)
        self.assertIn("permissions: {}", workflow)
        self.assertIn("contents: read", workflow)
        self.assertNotIn("packages: write", workflow)
        self.assertNotIn("docker push", workflow)
        self.assertNotIn("docker run -p ", workflow)

        runner = (ROOT / "tools" / "integrated_service_lab.py").read_text(encoding="utf-8")
        self.assertIn('"--internal"', runner)
        self.assertIn("verify_container_isolated", runner)
        self.assertIn("ha_recording_control_ci", runner)
        self.assertIn("record:\\n", runner)
        self.assertIn("  enabled: false\\n", runner)
        self.assertNotIn('"ports",', runner)

        smoke = (ROOT / "tools" / "integrated_lab" / "ha_mqtt_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('BASE + "/api/"', smoke)
        self.assertIn("from frigate_version import parse_frigate_version", smoke)
        self.assertIn('session.get("http://frigate:5000/api/version")', smoke)


if __name__ == "__main__":
    unittest.main()
