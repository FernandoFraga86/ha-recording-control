from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from build_official_base_image import (
    _canonical_config,
    docker_import_changes,
    pinned_patch_labels,
    pinned_upstream_commit,
    verify_oci_index,
    verify_oci_manifest,
    verify_python_tree_matches_commit,
)


class OfficialImageBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.child_digest = "sha256:" + "a" * 64
        self.config_digest = "sha256:" + "b" * 64

    def test_official_index_must_bind_linux_amd64_to_pinned_manifest(self) -> None:
        document = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "digest": self.child_digest,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": "sha256:" + "c" * 64,
                    "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"},
                },
            ],
        }
        raw = json.dumps(document, separators=(",", ":")).encode()
        index_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        verified = verify_oci_index(raw, index_digest, self.child_digest)
        self.assertEqual(verified["schemaVersion"], 2)

        with self.assertRaises(SystemExit):
            verify_oci_index(raw, index_digest, "sha256:" + "d" * 64)

    def test_platform_manifest_must_bind_the_locked_config(self) -> None:
        document = {
            "schemaVersion": 2,
            "config": {"digest": self.config_digest},
            "layers": [],
        }
        raw = json.dumps(document, separators=(",", ":")).encode()
        manifest_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.assertEqual(
            verify_oci_manifest(raw, manifest_digest, self.config_digest)["config"]["digest"],
            self.config_digest,
        )
        with self.assertRaises(SystemExit):
            verify_oci_manifest(raw, manifest_digest, "sha256:" + "d" * 64)

    def test_candidate_upstream_commit_is_read_from_verification_lock(self) -> None:
        commit = "cfa08c56b23d23280c3d0a218e5a43182c97445b"
        self.assertEqual(pinned_upstream_commit({"upstream": {"commit": commit}}), commit)
        with self.assertRaises(SystemExit):
            pinned_upstream_commit({"source": {"upstream_base_commit": commit}})

    def test_candidate_patch_labels_match_actual_verification_manifest(self) -> None:
        manifest_path = Path(__file__).resolve().parents[1] / "candidate-verification.json"
        verification = json.loads(manifest_path.read_text(encoding="utf-8"))
        labels = pinned_patch_labels(verification)
        self.assertEqual(
            labels,
            {
                "io.ha-recording-control.source-overlay-patch-sha256": "512f404a274aeb11a3e26246cddae48a62539cc01ccb10d6a297c5ea42221b65",
                "io.ha-recording-control.branding-patch-sha256": "35dddbc08947abb8db1b97e7faeb4855d323f8f0fa722db9756afcf20a2e0a6a",
                "io.ha-recording-control.distribution-build-source-patch-sha256": "2f093d99c1f77d9869eeb1a70d54b8c66e0112fbc1c511aec4dceb58e4e28d1c",
            },
        )

    def test_candidate_patch_labels_reject_missing_or_malformed_hashes(self) -> None:
        with self.assertRaises(SystemExit):
            pinned_patch_labels({"patches": {"source_overlay": {"sha256": "bad"}}})

    def test_docker_import_reconstructs_official_runtime_settings(self) -> None:
        config = {
            "Env": ["PATH=/usr/local/bin:/usr/bin", "NVIDIA_VISIBLE_DEVICES=all"],
            "Entrypoint": ["/init"],
            "Cmd": [],
            "WorkingDir": "/opt/frigate/",
            "User": "",
            "ExposedPorts": {"5000/tcp": {}, "8555/udp": {}},
            "Volumes": None,
            "Healthcheck": {
                "Test": ["CMD-SHELL", "curl --fail http://127.0.0.1:5000/api/version || exit 1"],
                "Interval": 15_000_000_000,
                "Timeout": 5_000_000_000,
                "StartPeriod": 300_000_000_000,
                "StartInterval": 5_000_000_000,
                "Retries": 3,
            },
            "Labels": {"org.example.base": "upstream"},
            "AttachStdin": False,
            "AttachStdout": False,
            "AttachStderr": False,
            "Tty": False,
            "OpenStdin": False,
            "StdinOnce": False,
            "Hostname": "",
            "Domainname": "",
            "MacAddress": "",
            "NetworkDisabled": False,
            "OnBuild": None,
            "Shell": None,
            "ArgsEscaped": False,
        }
        instructions = docker_import_changes(
            config,
            {"org.opencontainers.image.title": "HA Recording Control"},
            image_os="linux",
        )
        self.assertIn('ENTRYPOINT ["/init"]', instructions)
        self.assertIn('WORKDIR "/opt/frigate/"', instructions)
        self.assertIn("EXPOSE 5000/tcp", instructions)
        self.assertIn("EXPOSE 8555/udp", instructions)
        self.assertTrue(any(item.startswith("HEALTHCHECK --interval=15s") for item in instructions))
        self.assertIn('LABEL org.example.base="upstream"', instructions)
        self.assertIn('LABEL org.opencontainers.image.title="HA Recording Control"', instructions)

    def test_runtime_config_comparison_normalizes_only_workdir_trailing_slash(self) -> None:
        base = _canonical_config({"WorkingDir": "/opt/frigate/"})
        imported = _canonical_config({"WorkingDir": "/opt/frigate"})
        self.assertEqual(base, imported)
        self.assertNotEqual(
            _canonical_config({"WorkingDir": "/opt/frigate//"}),
            imported,
        )
        self.assertNotEqual(
            _canonical_config({"WorkingDir": "/opt/other/"}),
            imported,
        )

    def test_linux_import_omits_windows_only_args_escaped_but_preserves_commands(self) -> None:
        config = {"ArgsEscaped": True, "Entrypoint": ["/init"], "Cmd": ["python", "-m", "frigate"]}
        instructions = docker_import_changes(config, {}, image_os="linux")
        self.assertIn('ENTRYPOINT ["/init"]', instructions)
        self.assertIn('CMD ["python", "-m", "frigate"]', instructions)

    def test_rootfs_import_rejects_non_linux_platform(self) -> None:
        with self.assertRaises(SystemExit):
            docker_import_changes({"ArgsEscaped": True}, {}, image_os="windows")

    def test_export_with_declared_volume_fails_closed(self) -> None:
        config = {"Volumes": {"/media/frigate": {}}}
        with self.assertRaises(SystemExit):
            docker_import_changes(config, {}, image_os="linux")

    def _create_upstream_fixture(self, root: Path) -> tuple[Path, str]:
        upstream = root / "upstream"
        upstream.mkdir()
        (upstream / "frigate").mkdir()
        (upstream / "frigate" / "__init__.py").write_bytes(b"# pinned source\n")
        (upstream / "Makefile").write_text(
            'VERSION = 0.18.2\n'
            'COMMIT_HASH := $(shell git log -1 --pretty=format:"%h"|tail -1)\n'
            'version:\n'
            '\techo \'VERSION = "$(VERSION)-$(COMMIT_HASH)"\' > frigate/version.py\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "init", str(upstream)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(upstream), "config", "core.autocrlf", "false"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(upstream), "add", "."], check=True, capture_output=True)
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "Offline Test")
        env.setdefault("GIT_AUTHOR_EMAIL", "offline-test@example.invalid")
        env.setdefault("GIT_COMMITTER_NAME", "Offline Test")
        env.setdefault("GIT_COMMITTER_EMAIL", "offline-test@example.invalid")
        subprocess.run(
            ["git", "-C", str(upstream), "commit", "-m", "fixture"],
            check=True,
            capture_output=True,
            env=env,
        )
        pinned_commit = subprocess.run(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return upstream, pinned_commit

    def test_generated_upstream_version_file_is_verified_against_pinned_recipe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ha-version-source-") as temp_dir:
            root = Path(temp_dir)
            upstream, pinned_commit = self._create_upstream_fixture(root)
            copied = root / "copied"
            copied.mkdir()
            (copied / "__init__.py").write_bytes(b"# pinned source\n")
            short_hash = subprocess.run(
                ["git", "-C", str(upstream), "log", "-1", "--pretty=format:%h"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (copied / "version.py").write_bytes(
                f'VERSION = "0.18.2-{short_hash}"\n'.encode("utf-8")
            )

            self.assertEqual(
                verify_python_tree_matches_commit(upstream, copied, pinned_commit),
                (1, True),
            )

    def test_generated_upstream_version_file_rejects_mismatched_version_or_commit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ha-version-mismatch-") as temp_dir:
            root = Path(temp_dir)
            upstream, pinned_commit = self._create_upstream_fixture(root)
            copied = root / "copied"
            copied.mkdir()
            (copied / "__init__.py").write_bytes(b"# pinned source\n")
            (copied / "version.py").write_text('VERSION = "0.18.2-wrong"\n', encoding="utf-8")

            with self.assertRaises(SystemExit):
                verify_python_tree_matches_commit(upstream, copied, pinned_commit)
            with self.assertRaises(SystemExit):
                verify_python_tree_matches_commit(upstream, copied, "0" * 40)


if __name__ == "__main__":
    unittest.main()
