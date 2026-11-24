import copy
import json
import unittest
from pathlib import Path

from registry import package, themes
from tests.fixtures import files_for, manifest, zip_bytes

VECTORS = json.loads((Path(__file__).resolve().parent / "vectors" / "contrast-vectors.json").read_text())


def theme(name: str) -> dict:
    return copy.deepcopy(next(v["theme"] for v in VECTORS["vectors"] if v["name"] == f"built-in {name} passes"))


def icon(body: str = '<path fill="none" stroke="currentColor" d="M2 2h12"/>', box: str = "0 0 16 16") -> bytes:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}">{body}</svg>'.encode()


def data_only(contributes: dict, **extra) -> dict:
    return manifest(main=None, contributes=contributes, **extra)


def opened(files):
    return package.open_package(zip_bytes(files), api_version=1)


class ThemeVectorTests(unittest.TestCase):
    """The vectors HikariSub's tests/themes reads too, so both loaders refuse the same themes the same way."""

    def test_each_vector(self):
        for vector in VECTORS["vectors"]:
            with self.subTest(vector["name"]):
                found, expected = themes.problem(vector["theme"]), vector["problem"]
                if expected is None:
                    self.assertIsNone(found)
                    continue
                self.assertIsNotNone(found)
                self.assertEqual(found.stage, expected["stage"])
                if expected["stage"] == "contrast":
                    self.assertEqual((found.path, found.message), (expected["path"], expected["message"]))


class LooksTests(unittest.TestCase):
    def refused(self, files, pattern):
        with self.assertRaisesRegex(package.PackageError, pattern):
            opened(files)

    def test_a_data_only_package_needs_no_main(self):
        dusk = {"themes": [{"id": "dusk", "title": "Dusk", "type": "dark", "path": "dusk.json"}]}
        pkg = opened(files_for(data_only(dusk), **{"main.lua": None, "dusk.json": json.dumps(theme("Dark")).encode()}))
        self.assertNotIn("main", pkg.manifest)

    def test_a_data_only_package_holds_only_data(self):
        for key, value in (("commands", [{"id": "a", "title": "A"}]), ("panels", [{"id": "a", "title": "A"}]),
                           ("statusItems", [{"id": "a", "title": "A", "text": "A"}])):
            with self.subTest(key):
                self.refused(files_for(data_only({key: value})), f"contributes/{key}: needs code, and a data-only package runs none")
        setting = [{"id": "s", "title": "S", "type": "boolean", "default": True}]
        self.refused(files_for(data_only({"settings": setting})), "a data-only package has no settings")
        network = {"network": {"hosts": ["example.com"], "reason": "Fetches"}}
        self.refused(files_for(data_only({}, permissions=network)), "a data-only package asks for no permissions")
        self.refused(files_for(data_only({}, tier="unrestricted")), "only a plugin with a Lua main can be unrestricted")
        opened(files_for(data_only({"commands": []}, permissions={})))

    def test_ids_repeat_nowhere(self):
        twice = [{"id": "a", "path": "a.svg"}, {"id": "a", "path": "b.svg"}]
        self.refused(files_for(manifest(contributes={"icons": twice})), "contributes/icons/1/id: repeats the id a")

    def test_an_own_icon_is_declared(self):
        command = [{"id": "glow", "title": "Glow", "icon": "own:spark"}]
        self.refused(files_for(manifest(contributes={"commands": command})),
                     "contributes/commands/0/icon: names no icon in contributes.icons")
        declared = {"commands": command, "icons": [{"id": "spark", "path": "spark.svg"}]}
        opened(files_for(manifest(contributes=declared), **{"spark.svg": icon('<g id="accent"><circle fill="currentColor" r="2"/></g>')}))

    def test_a_theme_loads_and_matches_its_type(self):
        dusk = {"themes": [{"id": "dusk", "title": "Dusk", "type": "dark", "path": "dusk.json"}]}
        self.refused(files_for(manifest(contributes=dusk)), "dusk.json names no file")
        self.refused(files_for(manifest(contributes=dusk), **{"dusk.json": json.dumps(theme("Light")).encode()}),
                     "dusk.json is a light theme, and hikari-plugin.json lists it as dark")
        dim = theme("Dark")
        dim["roles"]["muted"] = "#505050"
        self.refused(files_for(manifest(contributes=dusk), **{"dusk.json": json.dumps(dim).encode()}),
                     "dusk.json fails the theme check at /roles/muted: .* below 4.50:1")

    def test_an_icon_theme_covers_every_role(self):
        round_ = {"iconThemes": [{"id": "round", "title": "Round", "path": "round"}]}
        every = {f"round/{role}.svg": icon() for role in package.icon_roles()}
        opened(files_for(manifest(contributes=round_), **every))
        last = f"round/{package.icon_roles()[-1]}.svg"
        self.refused(files_for(manifest(contributes=round_), **{**every, last: None}), f"{last} names no file")
        self.refused(files_for(manifest(contributes=round_), **{**every, last: icon('<path fill="#FF0000"/>')}),
                     "draws fill in #FF0000; an interface icon draws in currentColor only")
        self.refused(files_for(manifest(contributes=round_), **{**every, last: icon(box="0 0 24 24")}),
                     "has no viewBox of 0 0 16 16")
        self.refused(files_for(manifest(contributes=round_), **{**every, last: icon('<path style="fill: red"/>')}),
                     "has a style attribute")


if __name__ == "__main__":
    unittest.main()
