"""Where published files go: Cloudflare R2, a local folder, or a dry run."""

import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TYPES = {"hikariplugin": "application/zip", "minisig": "text/plain; charset=utf-8", "json": "application/json",
         "png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml", "md": "text/markdown; charset=utf-8"}


class UploadError(Exception):
    pass


class LocalUploader:
    """Writes the site's /p/ and /index.json tree into a folder."""

    def __init__(self, root: Path):
        self.root = root

    def put(self, key: str, data: bytes, immutable: bool) -> None:
        path = self.root / key
        if immutable and path.exists():
            if path.read_bytes() != data:
                raise UploadError(f"{key} already holds other bytes; published files never change")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.exists() else None


class DryRunUploader:
    def __init__(self):
        self.puts: list[tuple[str, int]] = []

    def put(self, key: str, data: bytes, immutable: bool) -> None:
        self.puts.append((key, len(data)))
        print(f"dry run: would upload {key} ({len(data)} bytes)")

    def get(self, key: str) -> bytes | None:
        return None


class R2Uploader:
    """Cloudflare's R2 object API, with the CI's API token; no S3 keys needed."""

    def __init__(self, bucket: str, account: str, token: str):
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{account}/r2/buckets/{bucket}/objects/"
        self.token = token

    def _call(self, method: str, key: str, data: bytes | None = None):
        req = urllib.request.Request(self.base + urllib.parse.quote(key, safe="/"), data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", TYPES.get(key.rsplit(".", 1)[-1], "application/octet-stream"))
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()

    def get(self, key: str) -> bytes | None:
        try:
            return self._call("GET", key)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def put(self, key: str, data: bytes, immutable: bool) -> None:
        if immutable and (old := self.get(key)) is not None:
            if old != data:
                raise UploadError(f"{key} already holds other bytes; published files never change")
            return
        self._call("PUT", key, data)


def make(spec: str):
    if spec == "dry-run":
        return DryRunUploader()
    if spec.startswith("local:"):
        return LocalUploader(Path(spec[len("local:"):]))
    if spec.startswith("r2:"):
        token, account = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if not token or not account:
            raise UploadError("r2: needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID")
        return R2Uploader(spec[len("r2:"):], account, token)
    raise UploadError(f"unknown upload target {spec}")
