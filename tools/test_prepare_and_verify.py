import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
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
    image_stream = io.BytesIO()
    with tarfile.open(fileobj=image_stream, mode="w") as image:
        info = tarfile.TarInfo("sha256-test/layer.tar")
        info.size = len(layer_bytes)
        image.addfile(info, io.BytesIO(layer_bytes))
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


if __name__ == "__main__":
    unittest.main()
