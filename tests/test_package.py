import json
import unittest

from registry import package
from tests.fixtures import SVG, files_for, jpeg, manifest, png, zip_bytes


def opened(files, **kw):
    return package.open_package(zip_bytes(files, **kw), api_version=1)


class PackageTests(unittest.TestCase):
    def refused(self, files, pattern, **kw):
        with self.assertRaisesRegex(package.PackageError, pattern):
            opened(files, **kw)

    def test_a_good_package(self):
        pkg = opened(files_for(manifest()))
        self.assertEqual(pkg.manifest["id"], "alice.karaoke-flow")
        self.assertEqual(pkg.readme, "# Karaoke Flow\n")
        self.assertEqual(pkg.screenshot_sizes, [(1280, 720)])

    def test_media_are_optional_to_pl1(self):
        opened(files_for(manifest(icon=None, screenshots=None), **{"icon.png": None, "shots/one.png": None}))

    def test_paths_follow_pl1(self):
        for bad in ("../evil.lua", "/abs.lua", "con.lua", "a/lpt1.txt", "trailing.", "ü.lua", "a//b.lua"):
            with self.subTest(bad):
                self.refused(files_for(manifest(), **{bad: b"x"}), "bad path")
        self.refused(files_for(manifest(), **{"MAIN.lua": b"x"}), "bad path")
        self.refused(files_for(manifest(), **{"shots": b"x"}), "share the name")
        self.refused(files_for(manifest(), **{"link.lua": b"/etc/passwd"}), "symbolic link", symlink="link.lua")

    def test_limits(self):
        many = {f"f{i}.txt": b"" for i in range(package.MAX_PACKAGE_FILES)}
        self.refused({**files_for(manifest()), **many}, "more than 4096 files")

    def test_manifest(self):
        self.refused(files_for(manifest(), **{"hikari-plugin.json": None}), "no hikari-plugin.json")
        self.refused(files_for(manifest(), **{"hikari-plugin.json": b"{"}), "not JSON")
        self.refused(files_for(manifest(id="Not An Id")), "manifest: id")
        self.refused(files_for(manifest(summary="x" * 101)), "manifest: summary")
        self.refused(files_for(manifest(summary="two\nlines")), "manifest: summary")
        self.refused(files_for(manifest(api=2)), "needs API 2")
        self.refused(files_for(manifest(main="other.lua")), "main file missing")
        self.refused(files_for(manifest(icon="icon.gif")), "manifest: icon")
        self.refused(files_for(manifest(screenshots=[{"path": "a.png"}])), "manifest: screenshots")

    def test_icon(self):
        self.refused(files_for(manifest(), **{"icon.png": png(128, 128)}), "256 × 256")
        self.refused(files_for(manifest(), **{"icon.png": jpeg(256, 256)}), "not a PNG")
        self.refused(files_for(manifest(icon="missing.png")), "icon missing")
        opened(files_for(manifest(icon="icon.svg"), **{"icon.svg": SVG}))
        for svg, why in [
            (SVG.replace(b"0 0 48 48", b"0 0 48 40"), "square viewBox"),
            (SVG.replace(b"<rect", b"<script>alert(1)</script><rect"), "<script>"),
            (SVG.replace(b"<rect", b'<rect onclick="x()"'), "onclick"),
            (SVG.replace(b"<rect", b'<image href="https://evil.example/x.png"/><rect'), "links outside"),
            (SVG.replace(b'fill="#145c4c"', b'fill="url(https://evil.example/p)"'), "outside url"),
            (SVG.replace(b"<rect", b"<style>@import 'https://evil.example/a.css';</style><rect"), "imports"),
            (b'<!DOCTYPE svg [<!ENTITY x "y">]>' + SVG, "DOCTYPE"),
        ]:
            with self.subTest(why):
                self.refused(files_for(manifest(icon="icon.svg"), **{"icon.svg": svg}), why)

    def test_screenshots(self):
        opened(files_for(manifest(screenshots=[{"path": "s.jpg", "caption": "c"}]), **{"s.jpg": jpeg(1920, 1080)}))
        opened(files_for(manifest(), **{"shots/one.png": png(1366, 768)}))
        for shot, why in [(png(1280, 960), "16:9"), (png(1024, 576), "16:9"), (png(2560, 1440), "16:9"),
                          (jpeg(1280, 720), "not a PNG")]:
            with self.subTest(why):
                self.refused(files_for(manifest(), **{"shots/one.png": shot}), why)
        twice = [{"path": "shots/one.png", "caption": "a"}, {"path": "shots/one.png", "caption": "b"}]
        self.refused(files_for(manifest(screenshots=twice)), "listed twice")
        self.refused(files_for(manifest(), **{"shots/one.png": png(1280, 720) + bytes(2 * 1024 * 1024)}), "2 MiB")

    def test_readme(self):
        self.refused(files_for(manifest(), **{"README.md": b"\xff\xfe"}), "not UTF-8")
        self.refused(files_for(manifest(), **{"README.md": b"a" * (128 * 1024 + 1)}), "128 KiB")

    def test_schema_matches_pl1_plus_media(self):
        schema = json.loads(package.SCHEMA.read_text())
        self.assertFalse(schema["additionalProperties"])
        for field in ("summary", "icon", "screenshots"):
            self.assertIn(field, schema["properties"])


if __name__ == "__main__":
    unittest.main()
