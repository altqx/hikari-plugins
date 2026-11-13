"""End to end: listings and releases in, packages and a signed index.json out."""

import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from registry import index, listings, minisign, publish, upload
from registry.testkey import TEST_KEY
from registry.publish import Asset, Release
from tests.fixtures import (FakeReviewer, FakeSource, TempRoot, listing, package, release, signature,
                            throwaway_key)

NOW = datetime(2027, 1, 19, 10, 0, tzinfo=UTC)
REPO = "alice/karaoke-flow"
NET = {"network": {"hosts": ["libretranslate.com"], "reason": "Sends the selected lines to translate them."}}


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.root = TempRoot()
        self.out = tempfile.TemporaryDirectory()
        self.site = Path(self.out.name)
        self.uploader = upload.LocalUploader(self.site)
        self.source, self.reviewer = FakeSource(), FakeReviewer()
        self.root.add_listing(listing())

    def tearDown(self):
        self.root.cleanup()
        self.out.cleanup()

    def add(self, version, data=None, signer="alice", **flags):
        data = data or package(version)
        self.source.add(REPO, release(version, data, signature(data, signer), **flags))
        return data

    def run_once(self):
        current = listings.load_listings(self.root.path)
        config = listings.load_config(self.root.path)
        report = publish.run(self.root.path, current, config, self.source, self.uploader, self.reviewer, NOW)
        states = {pid: publish.load_state(self.root.path, pid) for pid in current}
        full, changed = index.finish(index.build(current, states, [], config), self.root.path, NOW,
                                     TEST_KEY.public.id_text)
        data = index.encode(full)
        if changed:
            self.uploader.put("index.json", data, immutable=False)
            self.uploader.put("index.json.minisig", index.sign(data, full, TEST_KEY).encode(), immutable=False)
        return report

    def index(self) -> dict:
        data = (self.site / "index.json").read_bytes()
        minisign.verify(data, (self.site / "index.json.minisig").read_text(), [TEST_KEY.public])
        return json.loads(data)

    def test_a_good_release_is_published_and_signed(self):
        data = self.add("1.0.0")
        report = self.run_once()
        self.assertEqual(report.published, ["alice.karaoke-flow 1.0.0"])
        self.assertEqual((self.site / "p/alice.karaoke-flow/1.0.0.hikariplugin").read_bytes(), data)
        for media in ("icon.png", "screenshot-1.png", "README.md"):
            self.assertTrue((self.site / "p/alice.karaoke-flow/1.0.0" / media).exists(), media)
        idx = self.index()
        self.assertEqual(idx["serial"], 1)
        plugin = idx["plugins"][0]
        self.assertEqual(plugin["publicKey"], listing()["publicKey"])
        v = plugin["versions"][0]
        self.assertEqual(v["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(v["url"], "https://marketplace.hikarisub.org/p/alice.karaoke-flow/1.0.0.hikariplugin")
        self.assertEqual(v["screenshots"][0]["caption"], "The preset panel.")
        self.assertEqual(v["changelog"], "Notes for 1.0.0.")
        self.assertEqual(idx["apiVersions"], {"1": "0.2.0"})

    def test_a_bad_signature_is_refused(self):
        data = package("1.0.0")
        self.source.add(REPO, release("1.0.0", data, signature(package("1.0.0", summary="Changed after signing."))))
        report = self.run_once()
        self.assertIn("signature: bad signature", report.refused[0])
        self.assertEqual(self.reviewer.refusals[0][:2], ("alice.karaoke-flow", "1.0.0"))
        self.assertFalse((self.site / "p").exists())

    def test_the_wrong_key_is_refused(self):
        self.add("1.0.0", signer="bob")
        self.assertIn("untrusted key", self.run_once().refused[0])

    def test_a_schema_error_is_refused(self):
        self.add("1.0.0", data=package("1.0.0", api="one"))
        self.assertIn("manifest: api", self.run_once().refused[0])

    def test_an_id_outside_the_authors_handle_is_refused(self):
        self.root.add_listing(listing(pid="alice.tools", repo="github.com/alice/tools"))
        data = package("1.0.0", pid="bob.tools")
        self.source.add("alice/tools", release("1.0.0", data, signature(data), pid="alice.tools"))
        refused = " ".join(self.run_once().refused)
        self.assertIn("is not the listing's alice.tools", refused)
        self.assertIn("outside the author's handle", refused)

    def test_a_permission_increase_opens_a_review_instead(self):
        self.add("1.0.0")
        self.run_once()
        data = self.add("1.1.0", data=package("1.1.0", permissions=NET))
        report = self.run_once()
        self.assertEqual(report.held, ["alice.karaoke-flow 1.1.0: asks for network host libretranslate.com"])
        pid, version, record = self.reviewer.approvals[0]
        self.assertEqual((pid, version), ("alice.karaoke-flow", "1.1.0"))
        self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual([v["version"] for v in self.index()["plugins"][0]["versions"]], ["1.0.0"])

        self.root.approve(pid, version, record)
        self.assertEqual(self.run_once().published, ["alice.karaoke-flow 1.1.0"])
        self.assertEqual(self.index()["plugins"][0]["versions"][0]["permissions"], NET)

    def test_an_approval_covers_only_the_bytes_reviewed(self):
        self.add("1.0.0", data=package("1.0.0", permissions=NET))
        self.run_once()
        self.root.approve("alice.karaoke-flow", "1.0.0", {"sha256": "0" * 64})
        self.assertEqual(self.run_once().held, ["alice.karaoke-flow 1.0.0: asks for network host libretranslate.com"])

    def test_the_first_version_with_permissions_is_reviewed(self):
        self.add("1.0.0", data=package("1.0.0", tier="unrestricted"))
        self.assertEqual(self.run_once().held, ["alice.karaoke-flow 1.0.0: asks for the unrestricted tier"])

    def test_a_downgrade_is_refused(self):
        self.add("2.0.0")
        self.run_once()
        self.add("1.5.0", when="2027-9")
        self.assertIn("1.5.0 is not newer than 2.0.0", self.run_once().refused[0])

    def test_marketplace_requirements(self):
        self.add("1.0.0", data=package("1.0.0", summary=None, license=None))
        refused = self.run_once().refused[0]
        self.assertIn("“summary”", refused)
        self.assertIn("“license”", refused)

    def test_drafts_and_prereleases_wait(self):
        self.add("1.0.0", draft=True)
        self.add("1.1.0", prerelease=True)
        report = self.run_once()
        self.assertEqual((report.published, report.refused), ([], []))

    def test_runs_are_idempotent_and_the_serial_only_grows(self):
        self.add("1.0.0")
        self.run_once()
        self.assertEqual(self.run_once().summary(), "Nothing new.")
        self.assertEqual(self.index()["serial"], 1)
        self.assertEqual(len(self.reviewer.refusals), 0)
        self.add("1.1.0")
        self.run_once()
        self.assertEqual(self.index()["serial"], 2)

    def test_a_refused_version_is_not_retried(self):
        self.add("1.0.0", signer="bob")
        self.run_once()
        self.run_once()
        self.assertEqual(len(self.reviewer.refusals), 1)

    def test_revocation(self):
        self.add("1.0.0")
        self.add("1.1.0")
        self.run_once()
        self.root.add_listing(listing(revokedVersions=[{"version": "1.1.0", "reason": "Crashes on VFR video.",
                                                         "date": "2027-01-19"}]))
        self.run_once()
        versions = self.index()["plugins"][0]["versions"]
        self.assertEqual(versions[0]["revoked"], {"reason": "Crashes on VFR video.", "date": "2027-01-19"})
        self.assertNotIn("revoked", versions[1])
        self.assertEqual(self.index()["serial"], 2)

        self.root.add_listing(listing(revoked={"reason": "Malware."}))
        self.add("1.2.0")
        report = self.run_once()
        self.assertEqual(report.published, [])
        plugin = self.index()["plugins"][0]
        self.assertEqual(plugin["revoked"], {"reason": "Malware."})
        self.assertEqual(len(plugin["versions"]), 2)

    def test_published_files_never_change(self):
        self.uploader.put("p/x/1.0.0.hikariplugin", b"one", immutable=True)
        self.uploader.put("p/x/1.0.0.hikariplugin", b"one", immutable=True)
        with self.assertRaisesRegex(upload.UploadError, "never change"):
            self.uploader.put("p/x/1.0.0.hikariplugin", b"two", immutable=True)

    def test_a_new_signing_key_bumps_the_serial(self):
        current = listings.load_listings(self.root.path)
        body = index.build(current, {}, [], listings.load_config(self.root.path))
        self.assertEqual(index.finish(body, self.root.path, NOW, "AAAA")[0]["serial"], 1)
        self.assertEqual(index.finish(body, self.root.path, NOW, "AAAA")[1], False)
        full, changed = index.finish(body, self.root.path, NOW, "BBBB")
        self.assertEqual((full["serial"], changed), (2, True))

    def test_backfill_puts_back_only_what_is_missing_and_checks_it(self):
        data = self.add("1.0.0")
        self.add("1.1.0")
        self.run_once()
        (self.site / "p/alice.karaoke-flow/1.0.0.hikariplugin").unlink()
        fresh = upload.LocalUploader(self.site / "fresh")
        fetched = []

        def fetch(url):
            fetched.append(url)
            return data if url.endswith("1.0.0.hikariplugin") else package("1.1.0")

        current = listings.load_listings(self.root.path)
        config = listings.load_config(self.root.path)
        self.assertEqual(publish.backfill(self.root.path, current, config, fresh, fetch), 2)
        self.assertIn("https://github.com/alice/karaoke-flow/releases/download/v1.0.0/"
                      "alice.karaoke-flow-1.0.0.hikariplugin", fetched)
        self.assertTrue((self.site / "fresh/p/alice.karaoke-flow/1.0.0/screenshot-1.png").exists())
        self.assertEqual(publish.backfill(self.root.path, current, config, fresh, fetch), 0)
        with self.assertRaisesRegex(publish.Refused, "no longer matches"):
            publish.backfill(self.root.path, current, config, upload.LocalUploader(self.site / "other"),
                             lambda url: b"changed")

    def test_the_index_matches_its_schema(self):
        import jsonschema
        self.add("1.0.0")
        self.run_once()
        schema = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "index.schema.json").read_text())
        jsonschema.validate(self.index(), schema)


OFFICIAL_REPO = "altqx/hikari-official-plugins"


class OfficialRepoTests(unittest.TestCase):
    """One repository, many plugins: releases are tagged <id>-v<version>."""

    def setUp(self):
        self.root = TempRoot()
        self.out = tempfile.TemporaryDirectory()
        self.uploader = upload.LocalUploader(Path(self.out.name))
        self.source, self.reviewer = FakeSource(), FakeReviewer()
        self.key = throwaway_key()
        self.root.reserve("hikarisub", self.key)
        for pid in ("hikarisub.macro-chains", "hikarisub.keyframes"):
            self.root.add_listing(listing(pid=pid, author="hikarisub", publicKey=self.key.public.text,
                                          repo=f"github.com/{OFFICIAL_REPO}"))

    def tearDown(self):
        self.root.cleanup()
        self.out.cleanup()

    def add(self, tag, pid, version, when):
        data = package(version, pid=pid)
        sig = minisign.sign(data, self.key, "timestamp:0").encode()
        name = f"{pid}-{version}.hikariplugin"
        assets = {name: Asset(name, len(data), lambda: data), name + ".minisig": Asset(name, len(sig), lambda: sig)}
        self.source.add(OFFICIAL_REPO, Release(tag, f"https://github.com/{OFFICIAL_REPO}/releases/tag/{tag}", when,
                                               "", assets))

    def run_once(self):
        current = listings.load_listings(self.root.path)
        return publish.run(self.root.path, current, listings.load_config(self.root.path), self.source,
                           self.uploader, self.reviewer, NOW)

    def test_each_plugin_takes_only_its_own_tags(self):
        self.add("hikarisub.macro-chains-v1.0.0", "hikarisub.macro-chains", "1.0.0", "2027-01-01")
        self.add("hikarisub.keyframes-v0.1.0", "hikarisub.keyframes", "0.1.0", "2027-01-02")
        self.add("hikarisub.macro-chains-v1.1.0", "hikarisub.macro-chains", "1.1.0", "2027-01-03")
        report = self.run_once()
        self.assertEqual(sorted(report.published), ["hikarisub.keyframes 0.1.0", "hikarisub.macro-chains 1.0.0",
                                                    "hikarisub.macro-chains 1.1.0"])
        self.assertEqual(report.refused, [])
        self.assertEqual(self.source.asked, [OFFICIAL_REPO])
        state = publish.load_state(self.root.path, "hikarisub.macro-chains")
        self.assertEqual(state["versions"][0]["release"],
                         f"https://github.com/{OFFICIAL_REPO}/releases/tag/hikarisub.macro-chains-v1.1.0")

    def test_a_release_under_another_tag_is_left_alone(self):
        self.add("v1.0.0", "hikarisub.macro-chains", "1.0.0", "2027-01-01")
        self.add("hikarisub.keyframes-v1.0.0", "hikarisub.macro-chains", "1.0.0", "2027-01-02")
        self.add("hikarisub.macro-chains-v1.0.1", "hikarisub.macro-chains", "1.0.0", "2027-01-03")
        report = self.run_once()
        self.assertEqual((report.published, report.refused, self.reviewer.refusals), ([], [], []))


class PublishIndexTests(unittest.TestCase):
    def setUp(self):
        self.root = TempRoot()
        self.out = tempfile.TemporaryDirectory()
        self.uploader = upload.LocalUploader(Path(self.out.name))

    def tearDown(self):
        self.root.cleanup()
        self.out.cleanup()

    def publish_index(self):
        import registry.__main__ as cli
        from unittest import mock
        config = listings.load_config(self.root.path)
        with mock.patch.object(cli, "ROOT", self.root.path):
            return cli.publish_index({}, [], config, self.uploader, TEST_KEY, NOW)

    def test_the_serial_is_returned_only_when_a_new_index_was_uploaded(self):
        self.assertEqual(self.publish_index(), 1)
        self.assertIsNone(self.publish_index())
        (Path(self.out.name) / "index.json").write_bytes(b"stale")
        self.assertEqual(self.publish_index(), 1)


if __name__ == "__main__":
    unittest.main()
