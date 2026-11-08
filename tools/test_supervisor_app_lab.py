"""Focused offline contract checks for the temporary HAOS/Supervisor lab harness."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("supervisor_app_lab", ROOT / "tools" / "supervisor_app_lab.py")
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class SupervisorAppLabTests(unittest.TestCase):
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
