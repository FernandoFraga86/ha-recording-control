"""Focused offline contract checks for the temporary HAOS/Supervisor lab harness."""

from __future__ import annotations

import importlib.util
import base64
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("supervisor_app_lab", ROOT / "tools" / "supervisor_app_lab.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class SupervisorAppLabTests(unittest.TestCase):
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

    def test_workflow_is_branch_scoped_and_has_capacity_gate_before_apt(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "supervisor-app-lab.yml").read_text(encoding="utf-8")
        self.assertIn("codex/supervisor-app-lab-2026-10-5", workflow)
        self.assertLess(workflow.index("Preflight remote runner"), workflow.index("apt-get update"))
        self.assertIn("/dev/kvm", workflow)
        self.assertIn("40 * 1024 * 1024 * 1024", workflow)

    def test_production_template_stays_manual_and_false_gates_are_not_changed(self) -> None:
        template = module.TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertIn("boot: manual", template)
        self.assertNotIn("boot: auto", template)
        lock = module.load_lock()
        self.assertIs(lock["production_ready"], False)
        self.assertIs(lock["installable"], False)
        self.assertIs(lock["native_recovery_enabled"], False)


if __name__ == "__main__":
    unittest.main()
