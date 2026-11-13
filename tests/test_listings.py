import json
import unittest

from registry import listings, minisign, permissions, semver
from tests.fixtures import REPO, TempRoot, listing, pub, throwaway_key


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

    def official(self, key: str, pid="hikarisub.macro-chains") -> dict:
        return listing(pid=pid, author="hikarisub", authorName="HikariSub", key="alice", publicKey=key,
                       repo="github.com/altqx/hikari-official-plugins")

    def test_the_reserved_handle_takes_only_its_own_key(self):
        official = throwaway_key()
        self.root.reserve("hikarisub", official)
        self.root.add_listing(self.official(official.public.text))
        self.assertIn("hikarisub.macro-chains", listings.load_listings(self.root.path))
        self.root.add_listing(self.official(pub("alice")))
        self.problems(f"the handle hikarisub is reserved for key {official.public.id_text}")

    def test_the_reserved_handle_is_refused_while_its_key_is_missing(self):
        self.root.reserve("hikarisub", throwaway_key())
        (self.root.path / "keys" / "hikarisub.pub").unlink()
        self.root.add_listing(self.official(pub("alice")))
        self.problems("the handle hikarisub is reserved, and its key keys/hikarisub.pub can't be read")

    def test_hikarisub_is_reserved_for_the_official_key(self):
        config = listings.load_config(REPO)
        self.assertEqual(config.reserved_handles, {"hikarisub": "keys/hikarisub.pub"})
        key = minisign.parse_public_key((REPO / "keys" / "hikarisub.pub").read_text())
        self.assertEqual(key.id_text, "C399EBCEA48BCB30")
        self.root.add_listing(self.official(pub("alice")))
        self.problems("the handle hikarisub is reserved")

    def test_look_alikes_of_a_reserved_handle_are_refused(self):
        for handle in ("hikari-sub", "hikarisub-official", "myhikarisub", "hikarisubs"):
            with self.subTest(handle=handle):
                root = TempRoot()
                root.add_listing(listing(pid=f"{handle}.tools", author=handle))
                with self.assertRaisesRegex(listings.ListingError, f"the handle {handle} is too close to the reserved "
                                                                   "handle hikarisub"):
                    listings.load_listings(root.path)
                root.cleanup()
        self.root.add_listing(listing(pid="hikari-fan.tools", author="hikari-fan"))
        self.assertIn("hikari-fan.tools", listings.load_listings(self.root.path))

    def test_one_repo_holds_one_handles_plugins(self):
        self.root.add_listing(listing())
        self.root.add_listing(listing(pid="alice.romaji-split"))
        self.assertEqual(listings.shared_repos(listings.load_listings(self.root.path)),
                         {"github.com/alice/karaoke-flow"})
        self.root.add_listing(listing(pid="bob.tools", author="bob", key="bob"))
        self.problems("github.com/alice/karaoke-flow is listed by more than one handle: alice, bob")

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
        git = {"permissions": {"vcs.read": "r"}}
        self.assertEqual(permissions.increase(none, git), ["the git repository's branches, tags and history (vcs.read)"])
        self.assertEqual(permissions.increase(git, {"permissions": {"vcs.read": "changed"}}), [])
        self.assertEqual(permissions.increase(git, none), [])


class SemverTests(unittest.TestCase):
    def test_precedence(self):
        order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11",
                 "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0"]
        self.assertEqual(sorted(order, key=semver.key), order)
        self.assertFalse(semver.newer("1.0.0+build", "1.0.0"))


if __name__ == "__main__":
    unittest.main()
