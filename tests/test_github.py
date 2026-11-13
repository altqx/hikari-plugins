import base64
import io
import json
import unittest
import urllib.error
from unittest import mock

from registry import github

RECORD = {"release": "https://github.com/alice/karaoke-flow/releases/tag/v1.1.0", "sha256": "ab" * 32,
          "asks": ["asks for network host libretranslate.com"]}
LISTING = {"id": "alice.karaoke-flow", "repo": "github.com/alice/karaoke-flow"}


class FakeGitHub:
    def __init__(self, branches=(), issues=()):
        self.branches = set(branches)
        self.issues = list(issues)
        self.calls = []
        self.tokens = []

    def __call__(self, method, path, body=None, token=None, **_):
        self.calls.append((method, path, body))
        if token:
            self.tokens.append(token)
        if method == "GET" and "/branches/" in path:
            if path.rsplit("/branches/", 1)[1] in self.branches:
                return {}
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO())
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "c0ffee"}}
        if method == "GET" and path.startswith("/search/issues"):
            return {"total_count": sum(title in path for title in self.issues)}
        return {}

    def writes(self):
        return [(method, path) for method, path, _ in self.calls if method != "GET"]

    def body(self, method, path):
        return next(body for m, p, body in self.calls if (m, p) == (method, path))


class ReviewerTests(unittest.TestCase):
    def run_with(self, fake, call, *args):
        with mock.patch.object(github, "_request", fake), mock.patch.object(github, "REGISTRY", "altqx/hikari-plugins"):
            call(github.GitHubReviewer(), *args)

    def test_a_held_version_opens_an_approval_pr(self):
        fake = FakeGitHub()
        self.run_with(fake, github.GitHubReviewer.request_approval, "alice.karaoke-flow", "1.1.0", RECORD)
        branch = "approve/alice.karaoke-flow-1.1.0"
        file = "/repos/altqx/hikari-plugins/contents/approvals/alice.karaoke-flow/1.1.0.json"
        self.assertEqual(fake.writes(), [
            ("POST", "/repos/altqx/hikari-plugins/git/refs"),
            ("PUT", file),
            ("POST", "/repos/altqx/hikari-plugins/pulls"),
        ])
        self.assertEqual(fake.body("POST", "/repos/altqx/hikari-plugins/git/refs"),
                         {"ref": f"refs/heads/{branch}", "sha": "c0ffee"})
        put = fake.body("PUT", file)
        self.assertEqual(put["branch"], branch)
        self.assertEqual(json.loads(base64.b64decode(put["content"])), RECORD)
        pr = fake.body("POST", "/repos/altqx/hikari-plugins/pulls")
        self.assertEqual((pr["head"], pr["base"]), (branch, "main"))
        self.assertEqual(pr["title"], "Approve alice.karaoke-flow 1.1.0: asks for more")
        self.assertIn("- asks for network host libretranslate.com", pr["body"])

    def test_an_approval_pr_is_opened_once(self):
        fake = FakeGitHub(branches={"approve/alice.karaoke-flow-1.1.0"})
        self.run_with(fake, github.GitHubReviewer.request_approval, "alice.karaoke-flow", "1.1.0", RECORD)
        self.assertEqual(fake.writes(), [])

    def test_a_refused_release_opens_an_issue(self):
        fake = FakeGitHub()
        self.run_with(fake, github.GitHubReviewer.report_refusal, LISTING, "1.2.0", ["the signature doesn't verify"])
        self.assertEqual(fake.writes(), [("POST", "/repos/altqx/hikari-plugins/issues")])
        issue = fake.body("POST", "/repos/altqx/hikari-plugins/issues")
        self.assertEqual(issue["title"], "alice.karaoke-flow 1.2.0 was refused")
        self.assertIn("- the signature doesn't verify", issue["body"])

    def test_a_refusal_is_reported_once(self):
        fake = FakeGitHub(issues=["alice.karaoke-flow%201.2.0%20was%20refused"])
        self.run_with(fake, github.GitHubReviewer.report_refusal, LISTING, "1.2.0", ["the signature doesn't verify"])
        self.assertEqual(fake.writes(), [])


class DispatchTests(unittest.TestCase):
    def test_a_new_index_asks_the_site_to_rebuild(self):
        fake = FakeGitHub()
        with mock.patch.object(github, "_request", fake):
            github.dispatch_index_published(7, "placeholder-token")
        self.assertEqual(fake.writes(), [("POST", "/repos/altqx/hikari-marketplace/dispatches")])
        self.assertEqual(fake.body("POST", "/repos/altqx/hikari-marketplace/dispatches"),
                         {"event_type": "index-published", "client_payload": {"serial": 7}})
        self.assertEqual(fake.tokens, ["placeholder-token"])

    def test_the_dispatch_carries_its_own_token(self):
        sent = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def urlopen(req, timeout):
            sent.append(req)
            return Response(b"")

        with mock.patch.object(github.urllib.request, "urlopen", urlopen), \
                mock.patch.dict(github.os.environ, {"GITHUB_TOKEN": "workflow-token"}):
            github.dispatch_index_published(7, "dispatch-token")
        self.assertEqual(sent[0].get_header("Authorization"), "Bearer dispatch-token")
        self.assertEqual(sent[0].full_url, "https://api.github.com/repos/altqx/hikari-marketplace/dispatches")

    def test_without_a_token_nothing_is_sent(self):
        import argparse

        import registry.__main__ as cli
        fake = FakeGitHub()
        with mock.patch.object(github, "_request", fake), mock.patch.dict(github.os.environ, {}, clear=True):
            self.assertEqual(cli.notify_site(argparse.Namespace(serial=7)), 0)
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
