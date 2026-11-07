import gzip
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parent
REPOSITORY = TOOLS.parent
SPEC = importlib.util.spec_from_file_location(
    "prepare_and_verify", TOOLS / "prepare_and_verify.py"
)
assert SPEC and SPEC.loader
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def tar_bytes(files: dict[str, bytes]) -> bytes:
    layer_stream = io.BytesIO()
    with tarfile.open(fileobj=layer_stream, mode="w") as layer:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            layer.addfile(info, io.BytesIO(content))

    layer_bytes = layer_stream.getvalue()
    layer_name = "sha256-test/layer.tar"
    config_name = "sha256-test/config.json"
    docker_manifest = json.dumps(
        [{"Config": config_name, "RepoTags": ["synthetic:test"], "Layers": [layer_name]}]
    ).encode("utf-8")
    image_stream = io.BytesIO()
    with tarfile.open(fileobj=image_stream, mode="w") as image:
        for name, content in {
            "manifest.json": docker_manifest,
            config_name: b"{}",
            layer_name: layer_bytes,
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            image.addfile(info, io.BytesIO(content))
    return image_stream.getvalue()


def oci_tar_bytes(
    files: dict[str, bytes],
    *,
    layer_descriptor_digest: str | None = None,
    compress_layer: bool = False,
) -> bytes:
    layer_stream = io.BytesIO()
    with tarfile.open(fileobj=layer_stream, mode="w") as layer:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            layer.addfile(info, io.BytesIO(content))

    layer_bytes = layer_stream.getvalue()
    if compress_layer:
        layer_bytes = gzip.compress(layer_bytes, mtime=0)
    config_bytes = b'{"architecture":"amd64","os":"linux"}'
    layer_digest = hashlib.sha256(layer_bytes).hexdigest()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": f"sha256:{config_digest}",
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": (
                    "application/vnd.oci.image.layer.v1.tar+gzip"
                    if compress_layer
                    else "application/vnd.oci.image.layer.v1.tar"
                ),
                "digest": f"sha256:{layer_descriptor_digest or layer_digest}",
                "size": len(layer_bytes),
            }
        ],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{manifest_digest}",
                "size": len(manifest_bytes),
                "platform": {"architecture": "amd64", "os": "linux"},
            }
        ],
    }
    outer_members = {
        "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
        "index.json": json.dumps(index, separators=(",", ":")).encode("utf-8"),
        f"blobs/sha256/{manifest_digest}": manifest_bytes,
        f"blobs/sha256/{config_digest}": config_bytes,
        f"blobs/sha256/{layer_digest}": layer_bytes,
    }
    image_stream = io.BytesIO()
    with tarfile.open(fileobj=image_stream, mode="w") as image:
        for name, content in outer_members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            image.addfile(info, io.BytesIO(content))
    return image_stream.getvalue()


def docker_blob_tar_bytes(
    files: dict[str, bytes], *, layer_reference_digest: str | None = None
) -> bytes:
    layer_stream = io.BytesIO()
    with tarfile.open(fileobj=layer_stream, mode="w") as layer:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            layer.addfile(info, io.BytesIO(content))

    layer_bytes = layer_stream.getvalue()
    config_bytes = b'{"architecture":"amd64","os":"linux"}'
    layer_digest = hashlib.sha256(layer_bytes).hexdigest()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    manifest = [
        {
            "Config": f"blobs/sha256/{config_digest}",
            "RepoTags": ["synthetic:blob-layout"],
            "Layers": [
                f"blobs/sha256/{layer_reference_digest or layer_digest}"
            ],
        }
    ]
    outer_members = {
        "manifest.json": json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
        f"blobs/sha256/{config_digest}": config_bytes,
        f"blobs/sha256/{layer_digest}": layer_bytes,
    }
    image_stream = io.BytesIO()
    with tarfile.open(fileobj=image_stream, mode="w") as image:
        for name, content in outer_members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            image.addfile(info, io.BytesIO(content))
    return image_stream.getvalue()


class DistributionVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=TOOLS)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_layer_check(
        self, archive: bytes, distribution: Path = REPOSITORY
    ) -> None:
        stdin = io.TextIOWrapper(io.BytesIO(archive), encoding="utf-8")
        with patch.object(sys, "stdin", stdin), redirect_stdout(io.StringIO()):
            VERIFY.verify_image_layers(type("Args", (), {"distribution": distribution})())

    def test_built_ui_with_neutral_brand_passes(self) -> None:
        web = self.root / "web"
        (web / "images").mkdir(parents=True)
        (web / "index.html").write_text(
            '<title>HA Recording Control</title><link href="/BASE_PATH/images/ha-recording-control.svg">',
            encoding="utf-8",
        )
        (web / "login.html").write_text(
            '<title>HA Recording Control</title><link href="/BASE_PATH/images/ha-recording-control.svg">',
            encoding="utf-8",
        )
        (web / "site.webmanifest").write_text(
            json.dumps(
                {
                    "name": "HA Recording Control",
                    "short_name": "HA Recording Control",
                    "icons": [
                        {
                            "src": "/BASE_PATH/images/ha-recording-control.svg",
                            "sizes": "any",
                            "type": "image/svg+xml",
                            "purpose": "any",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (web / "notifications-worker.js").write_text(
            'icon: "/images/ha-recording-control.svg", badge: "/images/ha-recording-control.svg"',
            encoding="utf-8",
        )
        (web / "images/ha-recording-control.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8"
        )
        with redirect_stdout(io.StringIO()):
            VERIFY.verify_built_web(
                type(
                    "Args",
                    (),
                    {"built_web": web, "distribution": REPOSITORY},
                )()
            )

    def test_built_ui_with_removed_brand_asset_fails(self) -> None:
        web = self.root / "web"
        (web / "images").mkdir(parents=True)
        (web / "index.html").write_text(
            '<title>HA Recording Control</title><link href="/BASE_PATH/images/ha-recording-control.svg">',
            encoding="utf-8",
        )
        (web / "login.html").write_text(
            '<title>HA Recording Control</title><link href="/BASE_PATH/images/ha-recording-control.svg">',
            encoding="utf-8",
        )
        (web / "site.webmanifest").write_text(
            json.dumps(
                {
                    "name": "HA Recording Control",
                    "short_name": "HA Recording Control",
                    "icons": [
                        {
                            "src": "/BASE_PATH/images/ha-recording-control.svg",
                            "sizes": "any",
                            "type": "image/svg+xml",
                            "purpose": "any",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (web / "notifications-worker.js").write_text(
            'icon: "/images/ha-recording-control.svg", badge: "/images/ha-recording-control.svg"',
            encoding="utf-8",
        )
        (web / "images/ha-recording-control.svg").write_text(
            "<svg></svg>", encoding="utf-8"
        )
        (web / "images/favicon.svg").write_text("<svg></svg>", encoding="utf-8")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            VERIFY.verify_built_web(
                type(
                    "Args",
                    (),
                    {"built_web": web, "distribution": REPOSITORY},
                )()
            )

    def test_image_layers_without_brand_assets_pass(self) -> None:
        archive = tar_bytes(
            {
                "opt/frigate/web/images/ha-recording-control.svg": b"<svg/>",
                "opt/frigate/web/images/marker.png": b"generic marker",
            }
        )
        self.run_layer_check(archive)

    def test_image_layer_brand_path_fails(self) -> None:
        archive = tar_bytes(
            {"opt/frigate/web/images/branding/favicon.svg": b"<svg/>"}
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_layer_check(archive)

    def test_image_layer_brand_bytes_fails_even_when_renamed(self) -> None:
        protected = b"known upstream protected artwork"
        lock = json.loads(
            (REPOSITORY / "candidate-verification.json").read_text(encoding="utf-8")
        )
        lock["upstream_brand_asset_sha256"] = {
            "synthetic-fixture": hashlib.sha256(protected).hexdigest()
        }
        distribution = self.root / "distribution"
        distribution.mkdir()
        (distribution / "candidate-verification.json").write_text(
            json.dumps(lock), encoding="utf-8"
        )
        archive = tar_bytes(
            {"opt/frigate/web/assets/rebranded-file.png": protected}
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_layer_check(archive, distribution)

    def test_oci_image_layout_layers_are_scanned_and_referenced_by_digest(self) -> None:
        self.run_layer_check(
            oci_tar_bytes({"opt/frigate/web/images/ha-recording-control.svg": b"<svg/>"})
        )

    def test_oci_gzip_compressed_layers_are_scanned(self) -> None:
        self.run_layer_check(
            oci_tar_bytes(
                {"opt/frigate/web/images/ha-recording-control.svg": b"<svg/>"},
                compress_layer=True,
            )
        )

    def test_oci_image_layout_rejects_a_missing_referenced_layer(self) -> None:
        archive = oci_tar_bytes(
            {"opt/frigate/web/images/ha-recording-control.svg": b"<svg/>"},
            layer_descriptor_digest="0" * 64,
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_layer_check(archive)

    def test_docker_manifest_with_digest_named_blob_layers_passes(self) -> None:
        self.run_layer_check(
            docker_blob_tar_bytes(
                {"opt/frigate/web/images/ha-recording-control.svg": b"<svg/>"}
            )
        )

    def test_docker_manifest_rejects_missing_digest_named_layer(self) -> None:
        archive = docker_blob_tar_bytes(
            {"opt/frigate/web/images/ha-recording-control.svg": b"<svg/>"},
            layer_reference_digest="0" * 64,
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_layer_check(archive)

    def test_oci_image_layout_rejects_brand_asset_in_a_layer(self) -> None:
        archive = oci_tar_bytes(
            {"opt/frigate/web/images/branding/favicon.svg": b"<svg/>"}
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.run_layer_check(archive)

    def test_distribution_build_source_patch_is_locked_and_applies_narrowly(self) -> None:
        lock = json.loads(
            (REPOSITORY / "candidate-verification.json").read_text(encoding="utf-8")
        )
        patch_entry = lock["patches"]["distribution_build_source"]
        patch_path = REPOSITORY / patch_entry["path"]
        self.assertEqual(
            hashlib.sha256(patch_path.read_bytes()).hexdigest(), patch_entry["sha256"]
        )
        patch_text = patch_path.read_text(encoding="utf-8")
        self.assertIn("http://distcache.FreeBSD.org/ports-distfiles/", patch_text)
        self.assertIn(
            'VOD_MODULE_ARCHIVE="kaltura-nginx-vod-module-${VOD_MODULE_VERSION}_GH0.tar.gz"',
            patch_text,
        )
        self.assertIn(
            "ace04201cf2d2b1a3e5e732a22b92225b8ce61a494df9cc7f79d97efface8952",
            patch_text,
        )
        self.assertIn('VOD_MODULE_ARCHIVE_SIZE="470904"', patch_text)

        with tempfile.TemporaryDirectory(dir=TOOLS) as temporary:
            root = Path(temporary)
            script = root / "docker/main/build_nginx.sh"
            script.parent.mkdir(parents=True)
            original_block = [
                "wget -nv https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz",
                "tar -zxf nginx-${NGINX_VERSION}.tar.gz -C /tmp/nginx --strip-components=1",
                "rm nginx-${NGINX_VERSION}.tar.gz",
                "mkdir /tmp/nginx-vod-module",
                "wget -nv https://github.com/kaltura/nginx-vod-module/archive/refs/tags/${VOD_MODULE_VERSION}.tar.gz",
                "tar -zxf ${VOD_MODULE_VERSION}.tar.gz -C /tmp/nginx-vod-module --strip-components=1",
                "rm ${VOD_MODULE_VERSION}.tar.gz",
                "    # Patch MAX_CLIPS to allow more clips to be added than the default 128",
                "sed -i 's/MAX_CLIPS (128)/MAX_CLIPS (1080)/g' /tmp/nginx-vod-module/vod/media_set.h",
                "patch -d /tmp/nginx-vod-module/ -p1 << 'EOF'",
            ]
            script.write_text(
                "".join(f"# context {index}\n" for index in range(29))
                + "\n".join(original_block)
                + "\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "apply", "--check", str(patch_path)], cwd=root, check=True
            )
            subprocess.run(["git", "apply", str(patch_path)], cwd=root, check=True)
            patched_script = script.read_text(encoding="utf-8")
            self.assertIn('sha256sum --check', patched_script)
            self.assertNotIn(
                "https://github.com/kaltura/nginx-vod-module/archive/refs/tags/",
                patched_script,
            )


if __name__ == "__main__":
    unittest.main()
