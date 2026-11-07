from __future__ import annotations

import hashlib
import json
import unittest

from build_official_base_image import (
    docker_import_changes,
    pinned_upstream_commit,
    verify_oci_index,
    verify_oci_manifest,
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
        )
        self.assertIn('ENTRYPOINT ["/init"]', instructions)
        self.assertIn('WORKDIR "/opt/frigate/"', instructions)
        self.assertIn("EXPOSE 5000/tcp", instructions)
        self.assertIn("EXPOSE 8555/udp", instructions)
        self.assertTrue(any(item.startswith("HEALTHCHECK --interval=15s") for item in instructions))
        self.assertIn('LABEL org.example.base="upstream"', instructions)
        self.assertIn('LABEL org.opencontainers.image.title="HA Recording Control"', instructions)

    def test_export_with_declared_volume_fails_closed(self) -> None:
        config = {"Volumes": {"/media/frigate": {}}}
        with self.assertRaises(SystemExit):
            docker_import_changes(config, {})


if __name__ == "__main__":
    unittest.main()
