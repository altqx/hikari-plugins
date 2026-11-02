import json
import unittest

from registry import listings, permissions, semver
from tests.fixtures import TempRoot, listing, pub


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.root = TempRoot()

    def tearDown(self):
        self.root.cleanup()

    def problems(self, pattern):
        with self.assertRaisesRegex(listings.ListingError, pattern):
            listings.load_listings(self.root.path)

    def test_a_good_listing(self):
        self.root.add_listing(listing())
        self.assertIn("alice.karaoke-flow", listings.load_listings(self.root.path))

    def test_an_id_outside_the_authors_handle_is_refused(self):
        self.root.add_listing(listing(pid="bob.karaoke-flow"))
        self.problems("must start with the author's handle")

    def test_file_name_must_match(self):
        (self.root.path / "plugins" / "other.json").write_text(json.dumps(listing()))
        self.problems("must be named alice.karaoke-flow.json")

    def test_a_handle_has_one_key(self):
        self.root.add_listing(listing())
        self.root.add_listing(listing(pid="alice.romaji-split", key="bob"))
        self.problems("already uses another key")

    def test_schema(self):
        self.root.add_listing(listing(categories=["Karaoke", "Timing", "Utilities"]))
        self.problems("categories")
        self.root.add_listing(listing(categories=["Games"]))
        self.problems("categories")

    def test_revocations(self):
        self.root.add_listing(listing(revokedVersions=[{"version": "1.0", "reason": "x"}]))
        self.problems("not a version")

    def test_featured(self):
        self.root.add_listing(listing())
        current = listings.load_listings(self.root.path)
        (self.root.path / "featured.json").write_text('{"featured": ["alice.nothing"]}')
        with self.assertRaisesRegex(listings.ListingError, "not a listed plugin"):
            listings.load_featured(self.root.path, current)

    def test_changes_for_reviewers(self):
        old = {"alice.karaoke-flow": listing()}
        new = {"alice.karaoke-flow": listing(key="bob", revokedVersions=[{"version": "1.0.0", "reason": "Crashes"}]),
               "carol.tools": listing(pid="carol.tools", author="carol", key="bob")}
        lines = listings.changes(old, new)
        self.assertIn("Key change for alice.karaoke-flow: needs the owner's check", lines)
        self.assertIn("Revokes alice.karaoke-flow 1.0.0: Crashes", lines)
        self.assertIn("New listing carol.tools, claiming the handle carol", lines)
        self.assertTrue(listings.changes(old, {})[0].startswith("Removes"))


class PermissionTests(unittest.TestCase):
    def test_increase(self):
        none = {"permissions": {}}
        net = {"permissions": {"network": {"hosts": ["a.example"], "reason": "r"}}}
        self.assertEqual(permissions.increase(None, none), [])
        self.assertEqual(permissions.increase(None, net), ["network host a.example"])
        self.assertEqual(permissions.increase(net, net), [])
        more = {"permissions": {"network": {"hosts": ["a.example", "b.example"], "reason": "changed"}}}
        self.assertEqual(permissions.increase(net, more), ["network host b.example"])
        self.assertEqual(permissions.increase(more, net), [])
        ro = {"permissions": {"files": [{"id": "fonts", "title": "Fonts", "reason": "r"}]}}
        rw = {"permissions": {"files": [{"id": "fonts", "title": "Fonts", "reason": "r", "writable": True}]}}
        self.assertEqual(permissions.increase(none, ro), ["folder “Fonts”"])
        self.assertEqual(permissions.increase(ro, rw), ["writing to folder “Fonts”"])
        self.assertEqual(permissions.increase(none, {"permissions": {"documents.all": "r"}}),
                         ["every open document (documents.all)"])
        self.assertEqual(permissions.increase(none, {"tier": "unrestricted"}), ["the unrestricted tier"])


class SemverTests(unittest.TestCase):
    def test_precedence(self):
        order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                 "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0"]
        self.assertEqual(sorted(order, key=semver.key), order)
        self.assertFalse(semver.newer("1.0.0+build", "1.0.0"))


if __name__ == "__main__":
    unittest.main()
