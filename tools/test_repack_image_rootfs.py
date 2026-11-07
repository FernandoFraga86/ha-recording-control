from __future__ import annotations

import hashlib
import io
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path

from repack_image_rootfs import repack_rootfs


def make_source_tar(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return output.getvalue()


class RootfsRepackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temporary.name)
        self.source = self.root / "upstream" / "frigate"
        self.web = self.root / "web"
        self.upstream = self.root / "upstream"
        (self.source / "api").mkdir(parents=True)
        (self.web / "images").mkdir(parents=True)
        (self.upstream / "web" / "images" / "branding").mkdir(parents=True)
        (self.source / "api" / "new.py").write_bytes(b"candidate = True\n")
        (self.web / "index.html").write_text("HA Recording Control", encoding="utf-8")
        (self.web / "site.webmanifest").write_text("{}", encoding="utf-8")
        (self.upstream / "LICENSE").write_text("MIT notice", encoding="utf-8")
        (self.upstream / "TRADEMARK.md").write_text("brand policy", encoding="utf-8")
        (self.upstream / "web" / "images" / "branding" / "LICENSE").write_text(
            "asset attribution", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_replaces_app_trees_preserves_runtime_metadata_and_removes_brand_bytes(self) -> None:
        protected = b"synthetic protected logo bytes"
        protected_hash = hashlib.sha256(protected).hexdigest()
        entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
        for name, body, mode in (
            ("etc/issue", b"Debian GNU/Linux\n", 0o644),
            ("opt/frigate/frigate/old.py", b"old implementation", 0o644),
            ("opt/frigate/web/index.html", b"old UI", 0o644),
            ("opt/frigate/licenses/OLD", b"old attribution", 0o644),
            ("opt/frigate/migrations/001.sql", b"CREATE TABLE x;", 0o640),
            ("opt/frigate/web/images/branding/favicon.svg", b"different protected path", 0o644),
            ("usr/share/icons/renamed-logo.svg", protected, 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.mode = mode
            info.uid = 123
            info.gid = 456
            info.size = len(body)
            entries.append((info, body))

        symlink = tarfile.TarInfo("usr/bin/python3")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "../lib/python3"
        entries.append((symlink, None))
        source_tar = make_source_tar(entries)
        output = self.root / "repacked.tar.gz"

        result = repack_rootfs(
            io.BytesIO(source_tar),
            output,
            frigate_source=self.source,
            built_web=self.web,
            attribution_root=self.upstream,
            removed_visual_assets=["web/images/branding/favicon.svg"],
            protected_asset_hashes=[protected_hash],
        )

        self.assertGreater(result["removed_members"], 0)
        with tarfile.open(output, "r:gz") as archive:
            members = {member.name: member for member in archive}
            self.assertIn("etc/issue", members)
            self.assertIn("opt/frigate/migrations/001.sql", members)
            self.assertEqual(members["opt/frigate/migrations/001.sql"].uid, 123)
            self.assertEqual(members["opt/frigate/migrations/001.sql"].gid, 456)
            self.assertEqual(stat.S_IMODE(members["opt/frigate/migrations/001.sql"].mode), 0o640)
            self.assertNotIn("opt/frigate/frigate/old.py", members)
            self.assertIn("opt/frigate/frigate/api/new.py", members)
            self.assertIn("opt/frigate/web/index.html", members)
            self.assertNotIn("opt/frigate/licenses/OLD", members)
            self.assertIn("opt/frigate/licenses/UPSTREAM-MIT-LICENSE", members)
            self.assertIn("opt/frigate/licenses/UPSTREAM-TRADEMARK-POLICY.md", members)
            self.assertIn("opt/frigate/licenses/UPSTREAM-WEB-BRAND-ASSETS-LICENSE", members)
            self.assertNotIn("opt/frigate/web/images/branding/favicon.svg", members)
            self.assertNotIn("usr/share/icons/renamed-logo.svg", members)
            self.assertIn("usr/bin/python3", members)
            self.assertEqual(members["usr/bin/python3"].linkname, "../lib/python3")

            added = members["opt/frigate/frigate/api/new.py"]
            self.assertEqual(added.uid, 0)
            self.assertEqual(added.gid, 0)
            self.assertEqual(archive.extractfile(added).read(), b"candidate = True\n")
            replaced_ui = archive.extractfile(members["opt/frigate/web/index.html"]).read()
            self.assertEqual(replaced_ui, b"HA Recording Control")

    def test_rejects_parent_traversal_before_writing_a_candidate(self) -> None:
        info = tarfile.TarInfo("opt/frigate/../../outside")
        info.size = 1
        archive = make_source_tar([(info, b"x")])
        with self.assertRaises(SystemExit):
            repack_rootfs(
                io.BytesIO(archive),
                self.root / "invalid.tar.gz",
                frigate_source=self.source,
                built_web=self.web,
                attribution_root=self.upstream,
                removed_visual_assets=[],
                protected_asset_hashes=[],
            )

    def test_rejects_link_from_retained_tree_into_replaced_web(self) -> None:
        link = tarfile.TarInfo("usr/share/frigate-web")
        link.type = tarfile.SYMTYPE
        link.linkname = "/opt/frigate/web/index.html"
        archive = make_source_tar([(link, None)])
        with self.assertRaises(SystemExit):
            repack_rootfs(
                io.BytesIO(archive),
                self.root / "invalid-link.tar.gz",
                frigate_source=self.source,
                built_web=self.web,
                attribution_root=self.upstream,
                removed_visual_assets=[],
                protected_asset_hashes=[],
            )


if __name__ == "__main__":
    unittest.main()
