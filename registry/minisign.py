"""Minisign keys and signatures, matching hikari_minisign (ED and legacy Ed)."""

import base64
import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

MAX_SIGNATURE_BYTES = 64 * 1024


class MinisignError(Exception):
    pass


@dataclass(frozen=True)
class PublicKey:
    key_id: bytes
    key: bytes

    @property
    def text(self) -> str:
        return base64.b64encode(b"Ed" + self.key_id + self.key).decode()

    @property
    def id_text(self) -> str:
        return self.key_id[::-1].hex().upper()

    def file_text(self) -> str:
        return f"untrusted comment: minisign public key {self.id_text}\n{self.text}\n"


@dataclass(frozen=True)
class SecretKey:
    key_id: bytes
    seed: bytes

    @property
    def public(self) -> PublicKey:
        pk = Ed25519PrivateKey.from_private_bytes(self.seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return PublicKey(self.key_id, pk)


def _b64(text: str) -> bytes:
    try:
        return base64.b64decode(text.strip(), validate=True)
    except ValueError as e:
        raise MinisignError("not base64") from e


def parse_public_key(text: str) -> PublicKey:
    """Takes a whole .pub file or just its base64 line."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise MinisignError("empty public key")
    raw = _b64(lines[-1])
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise MinisignError("not a minisign public key")
    return PublicKey(raw[2:10], raw[10:])


def _scrypt_params(opslimit: int, memlimit: int) -> tuple[int, int, int]:
    # libsodium's pickparams for crypto_pwhash_scryptsalsa208sha256.
    opslimit = max(opslimit, 32768)
    r = 8
    if opslimit < memlimit // 32:
        p = 1
        max_n = opslimit // (r * 4)
        n_log2 = next(i for i in range(1, 63) if (1 << i) > max_n // 2)
    else:
        max_n = memlimit // (r * 128)
        n_log2 = next(i for i in range(1, 63) if (1 << i) > max_n // 2)
        p = min((opslimit // 4) // (1 << n_log2), 0x3FFFFFFF) // r
    return n_log2, r, p


def parse_secret_key(text: str, password: str | None = None) -> SecretKey:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    raw = _b64(lines[-1])
    if len(raw) != 158 or raw[:2] != b"Ed" or raw[4:6] != b"B2":
        raise MinisignError("not a minisign secret key")
    kdf, salt = raw[2:4], raw[6:38]
    ops, mem = int.from_bytes(raw[38:46], "little"), int.from_bytes(raw[46:54], "little")
    keynum = raw[54:]
    if kdf == b"Sc":
        if password is None:
            raise MinisignError("the secret key needs its password")
        n_log2, r, p = _scrypt_params(ops, mem)
        stream = hashlib.scrypt(password.encode(), salt=salt, n=1 << n_log2, r=r, p=p,
                                maxmem=(128 * r * (1 << n_log2)) + (256 << 20), dklen=104)
        keynum = bytes(a ^ b for a, b in zip(keynum, stream))
    elif kdf != b"\0\0":
        raise MinisignError("unknown key derivation")
    key_id, sk, checksum = keynum[:8], keynum[8:72], keynum[72:]
    # `minisign -W` leaves the checksum zero.
    expected = hashlib.blake2b(b"Ed" + key_id + sk, digest_size=32).digest()
    if checksum != expected and not (kdf == b"\0\0" and checksum == bytes(32)):
        raise MinisignError("wrong password or damaged secret key")
    secret = SecretKey(key_id, sk[:32])
    if secret.public.key != sk[32:]:
        raise MinisignError("damaged secret key")
    return secret


def secret_key_file(secret: SecretKey) -> str:
    """An unencrypted .key file, as `minisign -G -W` writes."""
    sk = secret.seed + secret.public.key
    keynum = secret.key_id + sk + bytes(32)
    raw = b"Ed" + b"\0\0" + b"B2" + bytes(32) + bytes(16) + keynum
    return f"untrusted comment: minisign encrypted secret key\n{base64.b64encode(raw).decode()}\n"


def sign(message: bytes, secret: SecretKey, trusted_comment: str) -> str:
    if "\n" in trusted_comment or "\r" in trusted_comment:
        raise MinisignError("trusted comment must be one line")
    priv = Ed25519PrivateKey.from_private_bytes(secret.seed)
    signature = priv.sign(hashlib.blake2b(message, digest_size=64).digest())
    global_signature = priv.sign(signature + trusted_comment.encode())
    return (
        "untrusted comment: signature from minisign secret key\n"
        f"{base64.b64encode(b'ED' + secret.key_id + signature).decode()}\n"
        f"trusted comment: {trusted_comment}\n"
        f"{base64.b64encode(global_signature).decode()}\n"
    )


@dataclass(frozen=True)
class Verified:
    key: PublicKey
    trusted_comment: str
    prehashed: bool


def verify(message: bytes, signature: str, trusted: list[PublicKey]) -> Verified:
    if len(signature.encode()) > MAX_SIGNATURE_BYTES:
        raise MinisignError("signature too large")
    lines = signature.splitlines()
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
        raise MinisignError("malformed signature")
    raw = _b64(lines[1])
    if len(raw) != 74:
        raise MinisignError("malformed signature")
    algorithm, key_id, sig = raw[:2], raw[2:10], raw[10:]
    if algorithm not in (b"ED", b"Ed"):
        raise MinisignError("unknown algorithm")
    key = next((k for k in trusted if k.key_id == key_id), None)
    if key is None:
        raise MinisignError("signed by an untrusted key")
    comment = lines[2][len("trusted comment: "):]
    global_sig = _b64(lines[3])
    pub = Ed25519PublicKey.from_public_bytes(key.key)
    payload = hashlib.blake2b(message, digest_size=64).digest() if algorithm == b"ED" else message
    try:
        pub.verify(sig, payload)
    except InvalidSignature as e:
        raise MinisignError("bad signature") from e
    try:
        pub.verify(global_sig, sig + comment.encode())
    except InvalidSignature as e:
        raise MinisignError("the trusted comment is not signed") from e
    return Verified(key, comment, algorithm == b"ED")
