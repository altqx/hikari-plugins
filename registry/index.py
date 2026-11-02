"""Builds and signs index.json, the one file HikariSub trusts."""

import hashlib
import json
from datetime import datetime
from pathlib import Path

from . import minisign, semver
from .listings import Config

FORMAT = 1


def build(listings: dict[str, dict], states: dict[str, dict], featured: list[str], config: Config) -> dict:
    plugins = []
    for pid, listing in sorted(listings.items()):
        state = states.get(pid)
        if not state or not state["versions"]:
            continue
        revoked = {r["version"]: r for r in listing.get("revokedVersions", [])}
        versions = []
        for v in sorted(state["versions"], key=lambda v: semver.key(v["version"]), reverse=True):
            out = {k: v[k] for k in ("version", "api", "tier", "name", "summary", "description", "license", "authors",
                                     "permissions", "sha256", "size", "published", "changelog") if k in v}
            out["url"] = f"{config.base_url}/p/{pid}/{v['version']}.hikariplugin"
            if "homepage" in v:
                out["homepage"] = v["homepage"]
            if "icon" in v:
                out["icon"] = f"{config.base_url}/{v['icon']}"
            if "screenshots" in v:
                out["screenshots"] = [{**s, "url": f"{config.base_url}/{s['path']}"} for s in v["screenshots"]]
                for s in out["screenshots"]:
                    del s["path"]
            out["readme"] = f"{config.base_url}/{v['readme']}"
            if v["version"] in revoked:
                out["revoked"] = {k: revoked[v["version"]][k] for k in ("reason", "date") if k in revoked[v["version"]]}
            versions.append(out)
        entry = {
            "id": pid,
            "author": listing["author"],
            "authorName": listing.get("authorName", listing["author"]),
            "repo": f"https://{listing['repo']}",
            "publicKey": listing["publicKey"],
            "categories": listing["categories"],
            "versions": versions,
        }
        if "revoked" in listing:
            entry["revoked"] = listing["revoked"]
        plugins.append(entry)
    return {"format": FORMAT, "apiVersions": config.api_versions, "featured": featured, "plugins": plugins}


def content_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def finish(body: dict, root: Path, now: datetime, key_id: str) -> tuple[dict, bool]:
    """Adds serial and generated. The serial only grows: with the content, or with a new signing key."""
    path = root / "published" / "_index.json"
    last = json.loads(path.read_text()) if path.exists() else {"serial": 0, "sha256": ""}
    digest = content_hash(body)
    changed = digest != last["sha256"] or key_id != last.get("key")
    serial = last["serial"] + 1 if changed else last["serial"]
    generated = now.strftime("%Y-%m-%dT%H:%M:%SZ") if changed else last.get("generated")
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"serial": serial, "sha256": digest, "generated": generated, "key": key_id}, indent=2) + "\n")
    return {"format": FORMAT, "serial": serial, "generated": generated, **{k: v for k, v in body.items() if k != "format"}}, changed


def encode(index: dict) -> bytes:
    return (json.dumps(index, indent=1, ensure_ascii=False) + "\n").encode()


def sign(data: bytes, index: dict, secret: minisign.SecretKey) -> str:
    return minisign.sign(data, secret, f"file:index.json serial:{index['serial']} generated:{index['generated']}")
