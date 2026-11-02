"""Opens a .hikariplugin the way PL1's openPackage does, plus the media rules."""

import io
import json
import stat
import zipfile
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import jsonschema

from . import media

MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_FILES = 4096
MANIFEST = "hikari-plugin.json"
README = "README.md"
SCHEMA = Path(__file__).resolve().parent.parent / "schemas" / "hikari-plugin.schema.json"
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.-+")
_DEVICES = {"con", "prn", "aux", "nul"}


class PackageError(Exception):
    pass


@dataclass
class Package:
    manifest: dict
    files: dict[str, bytes]
    readme: str | None = None
    screenshot_sizes: list[tuple[int, int]] = field(default_factory=list)


def _good_part(part: str) -> bool:
    stem = part.split(".", 1)[0].lower()
    reserved = stem in _DEVICES or (len(stem) == 4 and stem[:3] in ("com", "lpt") and stem[3] in "123456789")
    return (part not in ("", ".", "..") and len(part) <= 255 and set(part) <= _ALLOWED
            and not part.endswith((".", " ")) and not reserved)


def good_path(path: str) -> bool:
    return 0 < len(path) <= 400 and all(_good_part(p) for p in path.split("/"))


@cache
def _validator():
    schema = json.loads(SCHEMA.read_text())
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def validate_manifest(manifest) -> None:
    errors = sorted(_validator().iter_errors(manifest), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "(root)"
        raise PackageError(f"manifest: {where}: {e.message}")


def read_zip(data: bytes) -> dict[str, bytes]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise PackageError("not a zip") from e
    entries = [i for i in z.infolist() if not i.is_dir()]
    if len(entries) > MAX_PACKAGE_FILES:
        raise PackageError(f"more than {MAX_PACKAGE_FILES} files")
    if sum(i.file_size for i in entries) > MAX_PACKAGE_BYTES:
        raise PackageError("over 64 MiB unpacked")
    files = {}
    for info in entries:
        if stat.S_ISLNK(info.external_attr >> 16):
            raise PackageError(f"{info.filename}: symbolic link")
        with z.open(info) as f:
            content = f.read(info.file_size + 1)
        if len(content) != info.file_size:
            raise PackageError(f"{info.filename}: size does not match the zip directory")
        files[info.filename] = content
    return files


def open_package(data: bytes, api_version: int) -> Package:
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageError("over 64 MiB")
    files = read_zip(data)
    seen = set()
    for path in files:
        if not good_path(path) or path.lower() in seen:
            raise PackageError(f"{path}: bad path")
        seen.add(path.lower())
    folders = {p[:i] for p in seen for i in range(len(p)) if p[i] == "/"}
    if clash := seen & folders:
        raise PackageError(f"{sorted(clash)[0]}: a file and a folder share the name")
    if MANIFEST not in files:
        raise PackageError(f"no {MANIFEST}")
    try:
        manifest = json.loads(files[MANIFEST].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PackageError(f"{MANIFEST} is not JSON") from e
    validate_manifest(manifest)
    if manifest["api"] > api_version:
        raise PackageError(f"needs API {manifest['api']}; the newest is {api_version}")
    if manifest["main"] not in files:
        raise PackageError(f"{manifest['main']}: main file missing")
    pkg = Package(manifest, files)
    _check_media(pkg)
    return pkg


def _check_media(pkg: Package) -> None:
    m, files = pkg.manifest, pkg.files
    total = 0
    try:
        if icon := m.get("icon"):
            if icon not in files:
                raise media.MediaError(f"{icon}: icon missing")
            media.check_icon(icon, files[icon])
            total += len(files[icon])
        paths = [s["path"] for s in m.get("screenshots", [])]
        if len(set(paths)) != len(paths):
            raise media.MediaError("a screenshot is listed twice")
        for path in paths:
            if path not in files:
                raise media.MediaError(f"{path}: screenshot missing")
            pkg.screenshot_sizes.append(media.check_screenshot(path, files[path]))
            total += len(files[path])
        if total > media.MAX_MEDIA_TOTAL:
            raise media.MediaError("icon and screenshots are over 8 MiB together")
        if README in files:
            pkg.readme = media.check_readme(files[README])
    except media.MediaError as e:
        raise PackageError(str(e)) from e
