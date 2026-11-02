import base64
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from registry import minisign
from tests.fixtures import KEYS

MESSAGE = b"hikari-plugins test message\n"
MINISIGN = os.environ.get("MINISIGN") or shutil.which("minisign")


def legacy_signature(message: bytes, secret: minisign.SecretKey, comment: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(secret.seed)
    sig = priv.sign(message)
    glob = priv.sign(sig + comment.encode())
    return (f"untrusted comment: x\n{base64.b64encode(b'Ed' + secret.key_id + sig).decode()}\n"
            f"trusted comment: {comment}\n{base64.b64encode(glob).decode()}\n")


def encrypted_key_file(secret: minisign.SecretKey, password: str, ops=1 << 15, mem=1 << 20) -> str:
    sk = secret.seed + secret.public.key
    keynum = secret.key_id + sk + hashlib.blake2b(b"Ed" + secret.key_id + sk, digest_size=32).digest()
    salt = bytes(range(32))
    n_log2, r, p = minisign._scrypt_params(ops, mem)
    stream = hashlib.scrypt(password.encode(), salt=salt, n=1 << n_log2, r=r, p=p, dklen=104)
    raw = (b"Ed" + b"Sc" + b"B2" + salt + ops.to_bytes(8, "little") + mem.to_bytes(8, "little")
           + bytes(a ^ b for a, b in zip(keynum, stream)))
    return f"untrusted comment: x\n{base64.b64encode(raw).decode()}\n"


class MinisignTests(unittest.TestCase):
    alice = KEYS["alice"]

    def test_round_trip(self):
        sig = minisign.sign(MESSAGE, self.alice, "file:test")
        v = minisign.verify(MESSAGE, sig, [self.alice.public])
        self.assertEqual(v.trusted_comment, "file:test")
        self.assertTrue(v.prehashed)

    def test_accepts_legacy_signatures_like_hikari_minisign(self):
        v = minisign.verify(MESSAGE, legacy_signature(MESSAGE, self.alice, "old"), [self.alice.public])
        self.assertFalse(v.prehashed)

    def test_refusals(self):
        sig = minisign.sign(MESSAGE, self.alice, "file:test")
        with self.assertRaisesRegex(minisign.MinisignError, "untrusted"):
            minisign.verify(MESSAGE, sig, [KEYS["bob"].public])
        with self.assertRaisesRegex(minisign.MinisignError, "bad signature"):
            minisign.verify(MESSAGE + b"x", sig, [self.alice.public])
        lines = sig.splitlines()
        lines[2] = "trusted comment: file:other"
        with self.assertRaisesRegex(minisign.MinisignError, "trusted comment"):
            minisign.verify(MESSAGE, "\n".join(lines) + "\n", [self.alice.public])
        with self.assertRaisesRegex(minisign.MinisignError, "malformed"):
            minisign.verify(MESSAGE, "nonsense", [self.alice.public])

    def test_a_key_that_claims_another_keys_id_is_refused(self):
        bob_as_alice = minisign.SecretKey(self.alice.key_id, KEYS["bob"].seed)
        with self.assertRaisesRegex(minisign.MinisignError, "bad signature"):
            minisign.verify(MESSAGE, minisign.sign(MESSAGE, bob_as_alice, "x"), [self.alice.public])

    def test_key_files(self):
        self.assertEqual(minisign.parse_public_key(self.alice.public.file_text()), self.alice.public)
        self.assertEqual(minisign.parse_secret_key(minisign.secret_key_file(self.alice)), self.alice)
        encrypted = encrypted_key_file(self.alice, "hunter2")
        self.assertEqual(minisign.parse_secret_key(encrypted, "hunter2"), self.alice)
        with self.assertRaisesRegex(minisign.MinisignError, "wrong password"):
            minisign.parse_secret_key(encrypted, "wrong")
        with self.assertRaisesRegex(minisign.MinisignError, "needs its password"):
            minisign.parse_secret_key(encrypted)


@unittest.skipUnless(MINISIGN, "minisign is not installed")
class AgainstMinisignTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = Path(self.dir.name)
        subprocess.run([MINISIGN, "-G", "-W", "-p", self.d / "k.pub", "-s", self.d / "k.key"], check=True,
                       capture_output=True)
        (self.d / "msg").write_bytes(MESSAGE)

    def tearDown(self):
        self.dir.cleanup()

    def test_both_ways(self):
        pub = minisign.parse_public_key((self.d / "k.pub").read_text())
        secret = minisign.parse_secret_key((self.d / "k.key").read_text())
        self.assertEqual(secret.public, pub)
        subprocess.run([MINISIGN, "-S", "-s", self.d / "k.key", "-m", self.d / "msg", "-t", "theirs"], check=True,
                       capture_output=True)
        self.assertEqual(minisign.verify(MESSAGE, (self.d / "msg.minisig").read_text(), [pub]).trusted_comment,
                         "theirs")
        (self.d / "ours.minisig").write_text(minisign.sign(MESSAGE, secret, "ours"))
        subprocess.run([MINISIGN, "-V", "-m", self.d / "msg", "-x", self.d / "ours.minisig", "-p", self.d / "k.pub"],
                       check=True, capture_output=True)

    @unittest.skipUnless(os.environ.get("MINISIGN_SCRYPT_TEST"), "needs 1 GiB for scrypt; CI only")
    def test_reads_a_password_protected_key(self):
        subprocess.run([MINISIGN, "-G", "-f", "-p", self.d / "e.pub", "-s", self.d / "e.key"], check=True,
                       input=b"pw\npw\n", capture_output=True)
        secret = minisign.parse_secret_key((self.d / "e.key").read_text(), "pw")
        self.assertEqual(secret.public, minisign.parse_public_key((self.d / "e.pub").read_text()))


if __name__ == "__main__":
    unittest.main()
