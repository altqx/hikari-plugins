"""Where published files go. R2 comes later; for now a local folder or a dry run."""

from pathlib import Path


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


def make(spec: str):
    if spec == "dry-run":
        return DryRunUploader()
    if spec.startswith("local:"):
        return LocalUploader(Path(spec[len("local:"):]))
    raise UploadError(f"unknown upload target {spec}; R2 is not wired yet")
