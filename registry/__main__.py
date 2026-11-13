"""python -m registry <command>; see README.md."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import index, listings, minisign, package, publish, upload
from .testkey import TEST_KEY

ROOT = Path(__file__).resolve().parent.parent


def _summary(text: str) -> None:
    print(text)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write(text + "\n")


def _listings_at(ref: str) -> dict[str, dict]:
    names = subprocess.run(["git", "ls-tree", "--name-only", ref, "plugins/"], cwd=ROOT, capture_output=True,
                           text=True, check=True).stdout.split()
    out = {}
    for name in names:
        if name.endswith(".json"):
            data = json.loads(subprocess.run(["git", "show", f"{ref}:{name}"], cwd=ROOT, capture_output=True,
                                             text=True, check=True).stdout)
            out[data["id"]] = data
    return out


def check_listings(args) -> int:
    try:
        current = listings.load_listings(ROOT)
        listings.load_featured(ROOT, current)
        listings.load_config(ROOT)
    except listings.ListingError as e:
        _summary(f"Listings refused:\n{e}")
        return 1
    if not args.base:
        print(f"{len(current)} listings are valid.")
        return 0
    lines = listings.changes(_listings_at(args.base), current)
    _summary("\n".join(f"- {line}" for line in lines) or "No listing changes.")
    return 1 if any(line.startswith("Removes") for line in lines) else 0


def check_package(args) -> int:
    config = listings.load_config(ROOT)
    data = Path(args.package).read_bytes()
    try:
        if args.listing:
            listing = json.loads(Path(args.listing).read_text())
            version = Path(args.package).name.removeprefix(listing["id"] + "-").removesuffix(".hikariplugin")
            publish.check_release(listing, version, data, Path(args.package + ".minisig").read_text(), None, config)
        else:
            package.open_package(data, config.newest_api)
    except (publish.Refused, package.PackageError) as e:
        print(f"Refused: {e}")
        return 1
    print("The package passes.")
    return 0


def _secret(args) -> minisign.SecretKey:
    if args.test_key:
        return TEST_KEY
    text = os.environ.get("MARKETPLACE_MINISIGN_KEY")
    if not text:
        raise SystemExit("No signing key: pass --test-key, or set MARKETPLACE_MINISIGN_KEY")
    secret = minisign.parse_secret_key(text, os.environ.get("MARKETPLACE_MINISIGN_PASSWORD"))
    pinned = ROOT / "keys" / "marketplace.pub"
    if args.command != "key-info" and (not pinned.exists() or minisign.parse_public_key(pinned.read_text()) != secret.public):
        raise SystemExit("The signing key does not match keys/marketplace.pub")
    return secret


def run_publish(args) -> int:
    from .github import GitHubReviewer, GitHubSource

    config = listings.load_config(ROOT)
    current = listings.load_listings(ROOT)
    featured = listings.load_featured(ROOT, current)
    secret = _secret(args)
    uploader = upload.make(args.upload)
    now = datetime.now(UTC)
    report = publish.run(ROOT, current, config, GitHubSource(), uploader, GitHubReviewer(), now)
    _summary(report.summary())
    serial = publish_index(current, featured, config, uploader, secret, now)
    if serial is not None and (path := os.environ.get("GITHUB_OUTPUT")):
        with open(path, "a") as f:
            f.write(f"index-serial={serial}\n")
    return 0


def publish_index(current, featured, config, uploader, secret, now) -> int | None:
    """Uploads the signed index when it differs from what the target holds. Returns the uploaded serial."""
    states = {pid: publish.load_state(ROOT, pid) for pid in current}
    body = index.build(current, states, featured, config)
    full, changed = index.finish(body, ROOT, now, secret.public.id_text)
    data = index.encode(full)
    if changed or uploader.get("index.json") != data:
        uploader.put("index.json", data, immutable=False)
        uploader.put("index.json.minisig", index.sign(data, full, secret).encode(), immutable=False)
        _summary(f"index.json serial {full['serial']}, signed by key {secret.public.id_text}.")
        return full["serial"]
    return None


def notify_site(args) -> int:
    from .github import SITE, dispatch_index_published

    token = os.environ.get("MARKETPLACE_DISPATCH_TOKEN")
    if not token:
        _summary(f"No MARKETPLACE_DISPATCH_TOKEN: run {SITE}'s Deploy production by hand.")
        return 0
    dispatch_index_published(args.serial, token)
    _summary(f"Asked {SITE} to rebuild from index serial {args.serial}.")
    return 0


def backfill(args) -> int:
    from .github import download

    config = listings.load_config(ROOT)
    count = publish.backfill(ROOT, listings.load_listings(ROOT), config, upload.make(args.upload), download)
    _summary(f"Backfilled {count} versions.")
    return 0


def key_info(args) -> int:
    sys.stdout.write(_secret(args).public.file_text())
    return 0


def test_key(args) -> int:
    sys.stdout.write(TEST_KEY.public.file_text())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="registry")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("check-listings", help="validate plugins/, featured.json and config/")
    p.add_argument("--base", help="git ref to compare with, to list what a PR changes")
    p.set_defaults(func=check_listings)
    p = sub.add_parser("check-package", help="run the automatic checks on a package before releasing it")
    p.add_argument("package")
    p.add_argument("--listing", help="your plugins/<id>.json; the .minisig must sit beside the package")
    p.set_defaults(func=check_package)
    p = sub.add_parser("publish", help="publish new releases and the signed index")
    p.add_argument("--upload", default="dry-run", help="dry-run, local:<folder> or r2:<bucket>")
    p.add_argument("--test-key", action="store_true", help="sign with the throwaway test key")
    p.set_defaults(func=run_publish)
    p = sub.add_parser("notify-site", help="ask the marketplace site to rebuild from a newly uploaded index")
    p.add_argument("--serial", type=int, required=True)
    p.set_defaults(func=notify_site)
    p = sub.add_parser("backfill", help="upload published versions missing from the target")
    p.add_argument("--upload", required=True)
    p.set_defaults(func=backfill)
    p = sub.add_parser("key-info", help="print the public key of MARKETPLACE_MINISIGN_KEY")
    p.add_argument("--test-key", action="store_true")
    p.set_defaults(func=key_info)
    p = sub.add_parser("test-key", help="print the throwaway test key's .pub")
    p.set_defaults(func=test_key)
    args = parser.parse_args()
    args.command = args.func.__name__.replace("_", "-")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
