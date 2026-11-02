"""Loads plugins/*.json, featured.json and config/registry.json, and checks them together."""

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import jsonschema

from . import minisign, semver

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


class ListingError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    base_url: str
    api_versions: dict[str, str]

    @property
    def newest_api(self) -> int:
        return max(int(k) for k in self.api_versions)


@cache
def _validator():
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / "listing.schema.json").read_text()))


def load_config(root: Path) -> Config:
    data = json.loads((root / "config" / "registry.json").read_text())
    return Config(data["baseUrl"].rstrip("/"), data["apiVersions"])


def load_listings(root: Path) -> dict[str, dict]:
    listings, problems = {}, []
    for path in sorted((root / "plugins").glob("*.json")):
        try:
            listing = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            problems.append(f"{path.name}: not JSON ({e.msg})")
            continue
        errors = sorted(_validator().iter_errors(listing), key=lambda e: list(e.path))
        if errors:
            where = "/".join(str(p) for p in errors[0].path) or "(root)"
            problems.append(f"{path.name}: {where}: {errors[0].message}")
            continue
        problems += [f"{path.name}: {p}" for p in _own_problems(path, listing)]
        listings[listing["id"]] = listing
    keys: dict[str, tuple[str, str]] = {}
    for listing in listings.values():
        handle, key = listing["author"], listing["publicKey"]
        if handle in keys and keys[handle][1] != key:
            problems.append(f"{listing['id']}.json: {handle} already uses another key in {keys[handle][0]}.json")
        keys.setdefault(handle, (listing["id"], key))
    if problems:
        raise ListingError("\n".join(problems))
    return listings


def _own_problems(path: Path, listing: dict) -> list[str]:
    out = []
    if path.stem != listing["id"]:
        out.append(f"the file must be named {listing['id']}.json")
    if listing["id"].split(".")[0] != listing["author"]:
        out.append(f"the id must start with the author's handle: {listing['author']}.…")
    try:
        minisign.parse_public_key(listing["publicKey"])
    except minisign.MinisignError as e:
        out.append(f"publicKey: {e}")
    seen = set()
    for r in listing.get("revokedVersions", []):
        if not semver.PATTERN.match(r["version"]):
            out.append(f"revokedVersions: {r['version']} is not a version")
        if r["version"] in seen:
            out.append(f"revokedVersions: {r['version']} is listed twice")
        seen.add(r["version"])
    return out


def load_featured(root: Path, listings: dict[str, dict]) -> list[str]:
    path = root / "featured.json"
    featured = json.loads(path.read_text())["featured"] if path.exists() else []
    if len(featured) > 3:
        raise ListingError("featured.json: three plugins at most")
    for pid in featured:
        if pid not in listings or "revoked" in listings[pid]:
            raise ListingError(f"featured.json: {pid} is not a listed plugin")
    return featured


def changes(old: dict[str, dict], new: dict[str, dict]) -> list[str]:
    """What a listing PR changes, for its reviewers."""
    out = []
    for pid, listing in new.items():
        was = old.get(pid)
        if was is None:
            others = [x for x in old.values() if x["author"] == listing["author"]]
            out.append(f"New listing {pid}" + ("" if others else f", claiming the handle {listing['author']}"))
            continue
        if was["publicKey"] != listing["publicKey"]:
            out.append(f"Key change for {pid}: needs the owner's check")
        if was["repo"] != listing["repo"]:
            out.append(f"Repository change for {pid}: {was['repo']} → {listing['repo']}")
        if "revoked" in listing and "revoked" not in was:
            out.append(f"Revokes {pid}: {listing['revoked']['reason']}")
        old_rv = {r["version"] for r in was.get("revokedVersions", [])}
        for r in listing.get("revokedVersions", []):
            if r["version"] not in old_rv:
                out.append(f"Revokes {pid} {r['version']}: {r['reason']}")
        for gone in old_rv - {r["version"] for r in listing.get("revokedVersions", [])}:
            out.append(f"Un-revokes {pid} {gone}")
    for pid in old.keys() - new.keys():
        out.append(f"Removes the listing {pid}: versions are never deleted; revoke instead")
    return out
