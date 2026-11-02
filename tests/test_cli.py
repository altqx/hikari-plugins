import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import registry.__main__ as cli
from registry import minisign, upload
from tests.fixtures import KEYS


class SigningKeyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "keys").mkdir()
        (self.root / "keys" / "marketplace.pub").write_text(KEYS["alice"].public.file_text())

    def tearDown(self):
        self.dir.cleanup()

    def secret(self, name, command="publish"):
        env = {"MARKETPLACE_MINISIGN_KEY": minisign.secret_key_file(KEYS[name])}
        with mock.patch.object(cli, "ROOT", self.root), mock.patch.dict(os.environ, env):
            return cli._secret(argparse.Namespace(test_key=False, command=command))

    def test_signs_only_with_the_pinned_key(self):
        self.assertEqual(self.secret("alice"), KEYS["alice"])
        with self.assertRaisesRegex(SystemExit, "does not match keys/marketplace.pub"):
            self.secret("bob")

    def test_key_info_works_before_the_key_is_pinned(self):
        self.assertEqual(self.secret("bob", command="key-info"), KEYS["bob"])


class R2Tests(unittest.TestCase):
    def test_put_keeps_published_files_immutable(self):
        store = {}

        def call(method, key, data=None):
            if method == "PUT":
                store[key] = data
                return b""
            if key not in store:
                raise upload.urllib.error.HTTPError(key, 404, "missing", {}, None)
            return store[key]

        r2 = upload.R2Uploader("bucket", "account", "token")
        r2._call = call
        self.assertIsNone(r2.get("index.json"))
        r2.put("p/x/1.0.0.hikariplugin", b"one", immutable=True)
        r2.put("p/x/1.0.0.hikariplugin", b"one", immutable=True)
        with self.assertRaisesRegex(upload.UploadError, "never change"):
            r2.put("p/x/1.0.0.hikariplugin", b"two", immutable=True)
        r2.put("index.json", b"1", immutable=False)
        r2.put("index.json", b"2", immutable=False)
        self.assertEqual(store["index.json"], b"2")

    def test_r2_needs_its_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(upload.UploadError, "needs"):
            upload.make("r2:hikari-marketplace")


if __name__ == "__main__":
    unittest.main()
