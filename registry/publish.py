"""Finds new releases, checks them, and publishes the ones that pass."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from . import minisign, package, permissions, semver
from .listings import Config, shared_repos

MAX_CHANGELOG = 8 * 1024


@dataclass
class Asset:
    name: str
    size: int
    fetch: callable


@dataclass
class Release:
    tag: str
    url: str
    published: str
    notes: str
    assets: dict[str, Asset]
    draft: bool = False
    prerelease: bool = False


class Source(Protocol):
    def releases(self, repo: str) -> list[Release]: ...

    def digest(self, url: str, limit: int) -> tuple[int, str]:
        """Size and SHA-256 of what `url` serves, reading no more than `limit` + 1 bytes."""


class Reviewer(Protocol):
    def request_approval(self, pid: str, version: str, record: dict) -> None: ...
    def report_refusal(self, listing: dict, version: str, reasons: list[str]) -> None: ...


class Refused(Exception):
    pass


@dataclass
class Report:
    published: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"Published {x}" for x in self.published] + [f"Held for review {x}" for x in self.held]
        return "\n".join(lines + [f"Refused {x}" for x in self.refused]) or "Nothing new."


def state_path(root: Path, pid: str) -> Path:
    return root / "published" / f"{pid}.json"


def load_state(root: Path, pid: str) -> dict:
    path = state_path(root, pid)
    return json.loads(path.read_text()) if path.exists() else {"id": pid, "versions": [], "refused": []}


def save_state(root: Path, state: dict) -> None:
    path = state_path(root, state["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def approval(root: Path, pid: str, version: str) -> dict | None:
    path = root / "approvals" / pid / f"{version}.json"
    return json.loads(path.read_text()) if path.exists() else None


Packages = dict[str | None, tuple[Asset, Asset]]
_FOR_PLATFORM = re.compile(rf"(.+)-({package.PLATFORM.pattern})")


def _candidate(listing: dict, release: Release, shared: bool) -> tuple[str, Packages] | None:
    """The version and its signed packages: one under None, or one per platform."""
    prefix, suffix = f"{listing['id']}-", ".hikariplugin"
    found: dict[str, Packages] = {}
    for name, asset in release.assets.items():
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        version, platform = name[len(prefix):-len(suffix)], None
        if m := _FOR_PLATFORM.fullmatch(version):
            version, platform = m.group(1), m.group(2)
        sig = release.assets.get(name + ".minisig")
        if shared and release.tag != f"{listing['id']}-v{version}":
            continue
        if semver.PATTERN.match(version) and sig:
            found.setdefault(version, {})[platform] = asset, sig
    return next(iter(found.items()), None)


def package_name(pid: str, version: str, platform: str | None) -> str:
    return f"{pid}-{version}-{platform}.hikariplugin" if platform else f"{pid}-{version}.hikariplugin"


def stored_package(pid: str, version: str, platform: str | None) -> str:
    return f"p/{pid}/{version}-{platform}.hikariplugin" if platform else f"p/{pid}/{version}.hikariplugin"


def release_digest(shas: dict[str | None, str]) -> str:
    """The SHA-256 an approval names: the package's own, or one over every platform's."""
    if list(shas) == [None]:
        return shas[None]
    return hashlib.sha256("".join(f"{p} {shas[p]}\n" for p in sorted(shas, key=lambda p: p or "")).encode()).hexdigest()


def check_release(listing: dict, version: str, data: bytes, signature: str, previous: dict | None,
                  config: Config, platform: str | None = None) -> package.Package:
    """Every automatic check on one package. Raises Refused with the reasons."""
    key = minisign.parse_public_key(listing["publicKey"])
    label = f"{platform}: " if platform else ""
    try:
        minisign.verify(data, signature, [key])
    except minisign.MinisignError as e:
        raise Refused(f"{label}signature: {e}") from e
    try:
        pkg = package.open_package(data, config.newest_api, platform)
    except package.PackageError as e:
        raise Refused(f"{label}{e}") from e
    m = pkg.manifest
    reasons = []
    if m["id"] != listing["id"]:
        reasons.append(f"the manifest id {m['id']} is not the listing's {listing['id']}")
    if m["id"].split(".")[0] != listing["author"]:
        reasons.append(f"the id is outside the author's handle {listing['author']}")
    if m["version"] != version:
        reasons.append(f"the file name says {version}; the manifest says {m['version']}")
    if m.get("publicKey") not in (None, listing["publicKey"]):
        reasons.append("the manifest's publicKey is not the listing's key")
    if previous and not semver.newer(m["version"], previous["version"]):
        reasons.append(f"{m['version']} is not newer than {previous['version']}")
    for needed in ("summary", "license"):
        if needed not in m:
            reasons.append(f"the marketplace needs “{needed}” in the manifest")
    if pkg.readme is None:
        reasons.append("the marketplace needs a README.md")
    if reasons:
        raise Refused("; ".join(reasons))
    return pkg


def check_packages(listing: dict, version: str, packages: dict[str | None, tuple[bytes, str]], previous: dict | None,
                   config: Config) -> package.Package:
    """check_release on each package. Packages per platform must carry the same manifest, byte for byte."""
    if None in packages and len(packages) > 1:
        raise Refused("the release has one package for every platform and packages per platform; keep one kind")
    checked = {p: check_release(listing, version, data, sig, previous, config, p)
               for p, (data, sig) in sorted(packages.items(), key=lambda kv: kv[0] or "")}
    first = next(iter(checked.values()))
    for platform, pkg in checked.items():
        if pkg.files[package.MANIFEST] != first.files[package.MANIFEST]:
            raise Refused(f"{platform}: the manifest is not the same as {next(iter(checked))}'s")
    return first


def check_downloads(manifest: dict, source: Source) -> None:
    """Fetches every program the manifest downloads and checks its size and SHA-256."""
    for program in manifest.get("contributes", {}).get("programs", []):
        for platform, run in sorted(program["platforms"].items()):
            if not (download := run.get("download")):
                continue
            at = f"program {program['id']} on {platform}: {download['url']}"
            try:
                size, sha = source.digest(download["url"], download["size"])
            except Exception as e:
                raise Refused(f"{at} could not be fetched ({e})") from e
            if size != download["size"] or sha != download["sha256"]:
                raise Refused(f"{at} is not the declared file ({size} bytes, SHA-256 {sha})")


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[1]


def _media_entry(pid: str, version: str, pkg: package.Package) -> dict:
    m, base = pkg.manifest, f"p/{pid}/{version}"
    media = {"readme": f"{base}/README.md"}
    if icon := m.get("icon"):
        media["icon"] = f"{base}/icon.{_ext(icon)}"
    shots = [{"path": f"{base}/screenshot-{n}.{_ext(s['path'])}", "caption": s["caption"], "width": w, "height": h}
             for n, (s, (w, h)) in enumerate(zip(m.get("screenshots", []), pkg.screenshot_sizes), 1)]
    if shots:
        media["screenshots"] = shots
    return media


def upload_files(pid: str, entry: dict, packages: dict[str | None, bytes], pkg: package.Package, uploader) -> None:
    """The packages and their media, at the keys the published entry names. Packages go last-but-index."""
    m = pkg.manifest
    if "icon" in entry:
        uploader.put(entry["icon"], pkg.files[m["icon"]], immutable=True)
    for shot, source in zip(entry.get("screenshots", []), m.get("screenshots", [])):
        uploader.put(shot["path"], pkg.files[source["path"]], immutable=True)
    uploader.put(entry["readme"], pkg.files[package.README], immutable=True)
    for platform, data in sorted(packages.items(), key=lambda kv: kv[0] or ""):
        uploader.put(stored_package(pid, entry["version"], platform), data, immutable=True)


def _where(packages: dict[str | None, bytes]) -> dict:
    """sha256 and size for one package, or packages with them per platform."""
    if list(packages) == [None]:
        return {"sha256": hashlib.sha256(packages[None]).hexdigest(), "size": len(packages[None])}
    return {"packages": {p: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for p, d in sorted(packages.items())}}


def _publish(listing, version, release, packages, pkg, state, uploader, now) -> None:
    pid, m = listing["id"], pkg.manifest
    entry = {
        "version": version,
        **_where(packages),
        "api": m["api"],
        "tier": m.get("tier", "restricted"),
        "name": m["name"],
        "summary": m["summary"],
        "description": m.get("description", ""),
        "license": m["license"],
        "homepage": m.get("homepage"),
        "authors": m["authors"],
        "permissions": m.get("permissions", {}),
        "programs": permissions.programs(m) or None,
        "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release": release.url,
        "changelog": release.notes.strip()[:MAX_CHANGELOG],
        **_media_entry(pid, version, pkg),
    }
    entry = {k: v for k, v in entry.items() if v is not None}
    upload_files(pid, entry, packages, pkg, uploader)
    state["versions"].insert(0, entry)
    state["versions"].sort(key=lambda v: semver.key(v["version"]), reverse=True)


def _published_packages(pid: str, v: dict, fetch) -> dict[str | None, bytes]:
    """A published version's packages, fetched from its release and checked against the published SHA-256s."""
    shas = {p: x["sha256"] for p, x in v["packages"].items()} if "packages" in v else {None: v["sha256"]}
    base = v["release"].replace("/releases/tag/", "/releases/download/")
    out = {}
    for platform, sha in shas.items():
        data = fetch(f"{base}/{package_name(pid, v['version'], platform)}")
        if hashlib.sha256(data).hexdigest() != sha:
            raise Refused(f"{pid} {v['version']}: the release asset no longer matches what was published")
        out[platform] = data
    return out


def _open_published(packages: dict[str | None, bytes], config: Config) -> package.Package:
    opened = [package.open_package(data, config.newest_api, p) for p, data in sorted(packages.items(), key=lambda kv: kv[0] or "")]
    return opened[0]


def recheck(listings: dict[str, dict], states: dict[str, dict], config: Config, fetch) -> None:
    """Opens every published package again with today's checks."""
    for pid in sorted(listings):
        for v in states[pid]["versions"]:
            try:
                _open_published(_published_packages(pid, v, fetch), config)
            except package.PackageError as e:
                raise package.PackageError(f"{pid} {v['version']}: {e}") from e


def backfill(root: Path, listings: dict[str, dict], config: Config, uploader, fetch) -> int:
    """Puts every published version missing from `uploader` back, fetched from its release and checked."""
    count = 0
    for pid in sorted(listings):
        for v in load_state(root, pid)["versions"]:
            platforms = v["packages"] if "packages" in v else [None]
            if all(uploader.get(stored_package(pid, v["version"], p)) is not None for p in platforms):
                continue
            packages = _published_packages(pid, v, fetch)
            upload_files(pid, v, packages, _open_published(packages, config), uploader)
            count += 1
    return count


def run(root: Path, listings: dict[str, dict], config: Config, source: Source, uploader, reviewer: Reviewer,
        now: datetime) -> Report:
    report = Report()
    shared = shared_repos(listings)
    by_repo: dict[str, list[Release]] = {}
    for pid, listing in sorted(listings.items()):
        if "revoked" in listing:
            continue
        state = load_state(root, pid)
        known = {v["version"] for v in state["versions"]} | {r["version"] for r in state["refused"]}
        repo = listing["repo"].removeprefix("github.com/")
        if repo not in by_repo:
            by_repo[repo] = [r for r in source.releases(repo) if not r.draft and not r.prerelease]
        for release in sorted(by_repo[repo], key=lambda r: r.published):
            found = _candidate(listing, release, listing["repo"] in shared)
            if not found or found[0] in known:
                continue
            version, assets = found
            label = f"{pid} {version}"
            if any(asset.size > (package.MAX_PLATFORM_PACKAGE_BYTES if p else package.MAX_PACKAGE_BYTES)
                   or sig.size > minisign.MAX_SIGNATURE_BYTES for p, (asset, sig) in assets.items()):
                reasons = ["the package or its signature is too large"]
                pkg = packages = None
            else:
                fetched = {p: (asset.fetch(), sig.fetch().decode("utf-8", "replace")) for p, (asset, sig) in assets.items()}
                packages = {p: data for p, (data, _) in fetched.items()}
                sha = release_digest({p: hashlib.sha256(d).hexdigest() for p, d in packages.items()})
                previous = state["versions"][0] if state["versions"] else None
                try:
                    pkg = check_packages(listing, version, fetched, previous, config)
                    check_downloads(pkg.manifest, source)
                    reasons = []
                except Refused as e:
                    pkg, reasons = None, [str(e)]
            if reasons:
                state["refused"].append({"version": version, "release": release.url, "reasons": reasons})
                reviewer.report_refusal(listing, version, reasons)
                report.refused.append(f"{label}: {'; '.join(reasons)}")
                save_state(root, state)
                known.add(version)
                continue
            asks = permissions.increase(_last_good_manifest(state), pkg.manifest)
            if asks:
                ok = approval(root, pid, version)
                if not ok or ok.get("sha256") != sha:
                    reviewer.request_approval(pid, version, {"sha256": sha, "release": release.url, "asks": asks})
                    report.held.append(f"{label}: asks for {', '.join(asks)}")
                    continue
            _publish(listing, version, release, packages, pkg, state, uploader, now)
            save_state(root, state)
            known.add(version)
            report.published.append(label)
    return report


def _last_good_manifest(state: dict) -> dict | None:
    v = state["versions"][0] if state["versions"] else None
    return {"tier": v["tier"], "permissions": v["permissions"], "programs": v.get("programs", [])} if v else None
