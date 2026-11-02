"""Finds new releases, checks them, and publishes the ones that pass."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from . import minisign, package, permissions, semver
from .listings import Config

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


def _candidate(listing: dict, release: Release) -> tuple[str, Asset, Asset] | None:
    prefix, suffix = f"{listing['id']}-", ".hikariplugin"
    for name, asset in release.assets.items():
        if name.startswith(prefix) and name.endswith(suffix):
            version = name[len(prefix):-len(suffix)]
            sig = release.assets.get(name + ".minisig")
            if semver.PATTERN.match(version) and sig:
                return version, asset, sig
    return None


def check_release(listing: dict, version: str, data: bytes, signature: str, previous: dict | None,
                  config: Config) -> package.Package:
    """Every automatic check. Raises Refused with the reasons."""
    key = minisign.parse_public_key(listing["publicKey"])
    try:
        minisign.verify(data, signature, [key])
    except minisign.MinisignError as e:
        raise Refused(f"signature: {e}") from e
    try:
        pkg = package.open_package(data, config.newest_api)
    except package.PackageError as e:
        raise Refused(str(e)) from e
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


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[1]


def _publish(listing, version, release, data, pkg, sha, state, uploader, now) -> None:
    pid, m = listing["id"], pkg.manifest
    base = f"p/{pid}/{version}"
    uploader.put(f"{base}.hikariplugin", data, immutable=True)
    media = {}
    if icon := m.get("icon"):
        media["icon"] = f"{base}/icon.{_ext(icon)}"
        uploader.put(media["icon"], pkg.files[icon], immutable=True)
    shots = []
    for n, s in enumerate(m.get("screenshots", []), 1):
        key = f"{base}/screenshot-{n}.{_ext(s['path'])}"
        uploader.put(key, pkg.files[s["path"]], immutable=True)
        w, h = pkg.screenshot_sizes[n - 1]
        shots.append({"path": key, "caption": s["caption"], "width": w, "height": h})
    if shots:
        media["screenshots"] = shots
    media["readme"] = f"{base}/README.md"
    uploader.put(media["readme"], pkg.files[package.README], immutable=True)
    entry = {
        "version": version,
        "sha256": sha,
        "size": len(data),
        "api": m["api"],
        "tier": m.get("tier", "restricted"),
        "name": m["name"],
        "summary": m["summary"],
        "description": m.get("description", ""),
        "license": m["license"],
        "homepage": m.get("homepage"),
        "authors": m["authors"],
        "permissions": m.get("permissions", {}),
        "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release": release.url,
        "changelog": release.notes.strip()[:MAX_CHANGELOG],
        **media,
    }
    state["versions"].insert(0, {k: v for k, v in entry.items() if v is not None})
    state["versions"].sort(key=lambda v: semver.key(v["version"]), reverse=True)


def run(root: Path, listings: dict[str, dict], config: Config, source: Source, uploader, reviewer: Reviewer,
        now: datetime) -> Report:
    report = Report()
    for pid, listing in sorted(listings.items()):
        if "revoked" in listing:
            continue
        state = load_state(root, pid)
        known = {v["version"] for v in state["versions"]} | {r["version"] for r in state["refused"]}
        repo = listing["repo"].removeprefix("github.com/")
        releases = [r for r in source.releases(repo) if not r.draft and not r.prerelease]
        for release in sorted(releases, key=lambda r: r.published):
            found = _candidate(listing, release)
            if not found or found[0] in known:
                continue
            version, asset, sig_asset = found
            label = f"{pid} {version}"
            if asset.size > package.MAX_PACKAGE_BYTES or sig_asset.size > minisign.MAX_SIGNATURE_BYTES:
                reasons = ["the package or its signature is too large"]
                pkg = data = None
            else:
                data = asset.fetch()
                sha = hashlib.sha256(data).hexdigest()
                previous = state["versions"][0] if state["versions"] else None
                try:
                    pkg = check_release(listing, version, data, sig_asset.fetch().decode("utf-8", "replace"),
                                        previous, config)
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
            _publish(listing, version, release, data, pkg, sha, state, uploader, now)
            save_state(root, state)
            known.add(version)
            report.published.append(label)
    return report


def _last_good_manifest(state: dict) -> dict | None:
    v = state["versions"][0] if state["versions"] else None
    return {"tier": v["tier"], "permissions": v["permissions"]} if v else None
