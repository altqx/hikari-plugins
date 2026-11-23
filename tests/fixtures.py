import hashlib
import io
import json
import os
import shutil
import struct
import tempfile
import zipfile
import zlib
from pathlib import Path

from registry import minisign
from registry.publish import Asset, Release

REPO = Path(__file__).resolve().parent.parent

KEYS = {
    "alice": minisign.SecretKey(b"ALICEKEY", bytes(range(32))),
    "bob": minisign.SecretKey(b"BOB__KEY", bytes(range(32, 64))),
}


def throwaway_key() -> minisign.SecretKey:
    return minisign.SecretKey(os.urandom(8), os.urandom(32))


def pub(name: str) -> str:
    return KEYS[name].public.text


def png(w: int, h: int) -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = zlib.compress((b"\0" + bytes(w * 3)) * h, 9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(
        b"IDAT", raw) + chunk(b"IEND", b"")


def jpeg(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\x01\x01\0\0\x01\0\x01\0\0"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" fill="#145c4c"/></svg>'


def manifest(version="1.0.0", pid="alice.karaoke-flow", **extra) -> dict:
    m = {"id": pid, "name": "Karaoke Flow", "version": version, "api": 1, "main": "main.lua",
         "authors": ["Alice"], "license": "MIT", "summary": "Syllable effects from presets.",
         "icon": "icon.png", "screenshots": [{"path": "shots/one.png", "caption": "The preset panel."}]}
    m.update(extra)
    return {k: v for k, v in m.items() if v is not None}


def files_for(m: dict, **overrides) -> dict[str, bytes]:
    files = {"hikari-plugin.json": json.dumps(m).encode(), "main.lua": b"return {}\n",
             "README.md": b"# Karaoke Flow\n", "icon.png": png(256, 256), "shots/one.png": png(1280, 720)}
    files.update(overrides)
    return {k: v for k, v in files.items() if v is not None}


def zip_bytes(files: dict[str, bytes], symlink: str | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 2, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o120777 if name == symlink else 0o100644) << 16
            z.writestr(info, data)
    return out.getvalue()


def package(version="1.0.0", **extra) -> bytes:
    return zip_bytes(files_for(manifest(version, **extra)))


PLATFORMS = ("linux-x64", "windows-x64")


def tool(platform: str) -> str:
    return f"bin/{platform}/tool"


def with_tools(version="1.0.0", **extra) -> dict:
    programs = [{"id": "tool", "title": "Tool", "platforms": {p: {"path": tool(p)} for p in PLATFORMS}}]
    return manifest(version, permissions={"programs": "Runs the bundled tool."}, contributes={"programs": programs},
                    **extra)


def platform_package(m: dict, platform: str, **overrides) -> bytes:
    """A package built for `platform` alone: the shared manifest and only that platform's tool."""
    return zip_bytes(files_for(m, **{tool(platform): b"\x7fELF " + platform.encode(), **overrides}))


def signature(data: bytes, signer="alice") -> bytes:
    return minisign.sign(data, KEYS[signer], "timestamp:0 file:x.hikariplugin").encode()


def release(version: str, data: bytes, sig: bytes, pid="alice.karaoke-flow", when=None, **flags) -> Release:
    name = f"{pid}-{version}.hikariplugin"
    assets = {name: Asset(name, len(data), lambda: data), name + ".minisig": Asset(name, len(sig), lambda: sig)}
    return Release(f"v{version}", f"https://github.com/alice/karaoke-flow/releases/tag/v{version}",
                   when or f"2027-{version}", f"Notes for {version}.", assets, **flags)


def platform_release(version: str, packages: dict[str, bytes], pid="alice.karaoke-flow", signer="alice") -> Release:
    assets = {}
    for platform, data in packages.items():
        name = f"{pid}-{version}-{platform}.hikariplugin"
        sig = signature(data, signer)
        assets[name] = Asset(name, len(data), lambda data=data: data)
        assets[name + ".minisig"] = Asset(name, len(sig), lambda sig=sig: sig)
    return Release(f"v{version}", f"https://github.com/alice/karaoke-flow/releases/tag/v{version}", f"2027-{version}",
                   f"Notes for {version}.", assets)


def listing(pid="alice.karaoke-flow", author="alice", key="alice", **extra) -> dict:
    data = {"id": pid, "author": author, "authorName": "Alice", "repo": "github.com/alice/karaoke-flow",
            "publicKey": pub(key), "categories": ["Karaoke"]}
    data.update(extra)
    return data


class FakeSource:
    def __init__(self):
        self.by_repo: dict[str, list[Release]] = {}
        self.asked: list[str] = []
        self.served: dict[str, bytes] = {}

    def add(self, repo: str, rel: Release) -> None:
        self.by_repo.setdefault(repo, []).append(rel)

    def releases(self, repo: str) -> list[Release]:
        self.asked.append(repo)
        return list(self.by_repo.get(repo, []))

    def digest(self, url: str, limit: int) -> tuple[int, str]:
        if url not in self.served:
            raise OSError("404")
        data = self.served[url][:limit + 1]
        return len(data), hashlib.sha256(data).hexdigest()


class FakeReviewer:
    def __init__(self):
        self.approvals: list[tuple[str, str, dict]] = []
        self.refusals: list[tuple[str, str, list[str]]] = []

    def request_approval(self, pid, version, record):
        self.approvals.append((pid, version, record))

    def report_refusal(self, listing, version, reasons):
        self.refusals.append((listing["id"], version, reasons))


class TempRoot:
    """A registry checkout in a temporary folder: config, featured.json and plugins/."""

    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        shutil.copytree(REPO / "config", self.path / "config")
        (self.path / "plugins").mkdir()
        (self.path / "featured.json").write_text('{"featured": []}\n')

    def add_listing(self, data: dict) -> None:
        (self.path / "plugins" / f"{data['id']}.json").write_text(json.dumps(data))

    def reserve(self, handle: str, key: minisign.SecretKey) -> None:
        config = json.loads((self.path / "config" / "registry.json").read_text())
        config["reservedHandles"] = {handle: f"keys/{handle}.pub"}
        (self.path / "config" / "registry.json").write_text(json.dumps(config))
        (self.path / "keys").mkdir(exist_ok=True)
        (self.path / "keys" / f"{handle}.pub").write_text(key.public.file_text())

    def approve(self, pid: str, version: str, record: dict) -> None:
        path = self.path / "approvals" / pid / f"{version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record))

    def cleanup(self) -> None:
        self.dir.cleanup()
