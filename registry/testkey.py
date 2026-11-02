"""A throwaway signing key for CI and the preview. Its secret is public, so HikariSub never trusts it."""

import hashlib

from .minisign import SecretKey

TEST_KEY = SecretKey(key_id=b"TESTONLY", seed=hashlib.sha256(b"hikari-plugins throwaway test key").digest())
