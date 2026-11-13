# hikari-plugins

The registry behind the [HikariSub plugin marketplace](https://marketplace.hikarisub.org). It holds the plugin listings, checks every release, and builds the signed `index.json` that HikariSub reads. The rules are in [ADR 0028](https://github.com/altqx/hikari/blob/main/docs/adr/0028-the-plugin-marketplace.md); the website's source is [altqx/hikari-marketplace](https://github.com/altqx/hikari-marketplace).

## For authors

1. Write the plugin, with an id that starts with your handle: `alice.karaoke-tools`. Besides PL1's manifest, the marketplace needs a `summary`, an SPDX `license` and a `README.md` at the package root. An `icon` and `screenshots` are optional.
2. Make a minisign key: `minisign -G -p alice.pub -s ~/.minisign/alice.key`.
3. Open a pull request adding `plugins/<id>.json`. We review the first listing by hand.

   ```json
   {
     "id": "alice.karaoke-tools",
     "author": "alice",
     "authorName": "Alice",
     "repo": "github.com/alice/karaoke-tools",
     "publicKey": "RW…",
     "categories": ["Karaoke"]
   }
   ```

4. Publish a GitHub release with `<id>-<version>.hikariplugin` and `<id>-<version>.hikariplugin.minisig`. Within half an hour it's checked and published, and its release notes become its changelog. Drafts and pre-releases are skipped. A repository can hold several of your plugins: give each its own listing, and tag each release `<id>-v<version>` (`alice.karaoke-tools-v1.2.0`), so CI knows which plugin it belongs to.

Check a package before releasing it:

```sh
pip install -r requirements.txt
python -m registry check-package alice.karaoke-tools-1.0.0.hikariplugin --listing plugins/alice.karaoke-tools.json
```

A release that fails is recorded and reported in an issue here; publish a new version once it's fixed. Version numbers are never reused.

## What CI checks

- **Listings** ([schema](schemas/listing.schema.json)): the file is named after the id; the id's first segment is the author's handle; one handle has one key; a repository belongs to one handle; one or two categories. The `hikarisub` handle is reserved for HikariSub's own plugins: a listing under it must use the key in [`keys/hikarisub.pub`](keys/hikarisub.pub) (C399EBCEA48BCB30). [`config/registry.json`](config/registry.json) names the reserved handles and their keys. On a pull request, CI lists what it changes for the reviewers: new handles, key changes and revocations. Removing a listing fails: versions are never deleted, only revoked.
- **Releases:** the author's signature with the listing's key; the package rules of PL1 (paths, sizes, the manifest [schema](schemas/hikari-plugin.schema.json), the API number, the main file); the media rules (icon, screenshots, README), plus one of the registry's own: an SVG icon may not have a `<style>` element or a `style` attribute, only presentation attributes such as `fill`; the id against the listing and the handle; the version against the file name and the last published version; the marketplace's required fields.
- **Permissions:** a version that asks for more than the last published one (a network host, a folder or writing to it, `documents.all`, the unrestricted tier) is held. CI opens a pull request adding `approvals/<id>/<version>.json` with the package's SHA-256; merging it publishes exactly those bytes. A plugin's first version is compared with nothing, so a first version with any permission is reviewed too.

## Revoking

A pull request to the listing:

```json
"revokedVersions": [{"version": "0.9.2", "reason": "Crashes the plugin helper on variable-frame-rate video.", "date": "2027-01-08"}],
"revoked": {"reason": "Uploads subtitle files to an undeclared host.", "date": "2027-01-20"}
```

`revokedVersions` marks versions, `revoked` the whole plugin. The index rebuilds when it merges, and HikariSub turns the plugin off at its next check.

## The index

[`schemas/index.schema.json`](schemas/index.schema.json) describes `index.json`. It is signed into `index.json.minisig` with the marketplace key, and its `serial` grows with every change, so HikariSub can refuse an older index. Packages and media live under `/p/<id>/<version>…` next to it. `published/` is the registry's own record of what it has published; only CI writes it.

## Keys and storage

- **Signing:** CI signs `index.json` with the marketplace key, kept as the `MARKETPLACE_MINISIGN_KEY` and `MARKETPLACE_MINISIGN_PASSWORD` secrets. It refuses any key but [`keys/marketplace.pub`](keys/marketplace.pub), the key HikariSub pins. The owner holds the offline copy; no key lives in a Worker. Tests use a throwaway key ([`registry/testkey.py`](registry/testkey.py)) that nothing trusts.
- **The site:** when Publish uploads a new `index.json`, it sends an `index-published` dispatch to [altqx/hikari-marketplace](https://github.com/altqx/hikari-marketplace), whose Deploy production rebuilds from the signed index. The dispatch uses the `MARKETPLACE_DISPATCH_TOKEN` secret, a token that can dispatch to that repository only. Without it, Publish says so, and the site is deployed by hand.
- **Storage:** packages, media and the index go to the private Cloudflare R2 bucket `hikari-marketplace` through the `CLOUDFLARE_API_TOKEN` secret. The Worker that serves them at marketplace.hikarisub.org comes with the go-live. Running Publish by hand with “backfill” uploads any published version R2 lacks, fetched again from its release and checked against its SHA-256.

## Development

```sh
pip install -r requirements.txt
python -m unittest
python -m registry check-listings
```

`MINISIGN=/path/to/minisign` adds the tests against the real minisign; `MINISIGN_SCRYPT_TEST=1` also reads a password-protected key (1 GiB of memory).

Malware and security problems: see [SECURITY.md](SECURITY.md).
