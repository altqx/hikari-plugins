"""GitHub as the release source and the place reviews and refusals go."""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .publish import Asset, Release

API = "https://api.github.com"
REGISTRY = os.environ.get("GITHUB_REPOSITORY", "altqx/hikari-plugins")


def _request(method: str, path: str, body: dict | None = None, accept: str = "application/vnd.github+json",
             limit: int | None = None, auth: bool = True):
    url = path if path.startswith("https://") else f"{API}{path}"
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Accept", accept)
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if auth and (token := os.environ.get("GITHUB_TOKEN")):
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1) if limit else r.read()
    if limit and len(data) > limit:
        raise ValueError(f"{url} is larger than {limit} bytes")
    return data if accept == "application/octet-stream" else json.loads(data or b"null")


def download(url: str, limit: int = 64 * 1024 * 1024 + 1) -> bytes:
    return _request("GET", url, accept="application/octet-stream", limit=limit, auth=False)


class GitHubSource:
    def releases(self, repo: str) -> list[Release]:
        out = []
        for r in _request("GET", f"/repos/{repo}/releases?per_page=30"):
            assets = {a["name"]: Asset(a["name"], a["size"], self._fetcher(a)) for a in r["assets"]}
            out.append(Release(r["tag_name"], r["html_url"], r.get("published_at") or "", r.get("body") or "", assets,
                               r["draft"], r["prerelease"]))
        return out

    @staticmethod
    def _fetcher(asset: dict):
        # The public download URL; the token must not follow its redirect to storage.
        return lambda: _request("GET", asset["browser_download_url"], accept="application/octet-stream",
                                limit=asset["size"], auth=False)


class GitHubReviewer:
    """Opens an approval PR per held version and an issue per refused release, each once."""

    def request_approval(self, pid: str, version: str, record: dict) -> None:
        branch = f"approve/{pid}-{version}"
        try:
            _request("GET", f"/repos/{REGISTRY}/branches/{branch}")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
        main = _request("GET", f"/repos/{REGISTRY}/git/ref/heads/main")["object"]["sha"]
        _request("POST", f"/repos/{REGISTRY}/git/refs", {"ref": f"refs/heads/{branch}", "sha": main})
        content = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
        _request("PUT", f"/repos/{REGISTRY}/contents/approvals/{pid}/{version}.json",
                 {"message": f"Approve {pid} {version}", "branch": branch,
                  "content": base64.b64encode(content.encode()).decode()})
        asks = "\n".join(f"- {a}" for a in record["asks"])
        _request("POST", f"/repos/{REGISTRY}/pulls", {
            "title": f"Approve {pid} {version}: asks for more",
            "head": branch, "base": "main",
            "body": f"{record['release']} passed every automatic check but asks for more than the last published "
                    f"version:\n\n{asks}\n\nMerging publishes it, and HikariSub asks each user before updating. "
                    f"Close it to keep the version out.",
        })

    def report_refusal(self, listing: dict, version: str, reasons: list[str]) -> None:
        title = f"{listing['id']} {version} was refused"
        found = _request("GET", f"/search/issues?q={urllib.parse.quote(f'repo:{REGISTRY} in:title \"{title}\"')}")
        if found["total_count"]:
            return
        body = "\n".join(f"- {r}" for r in reasons)
        _request("POST", f"/repos/{REGISTRY}/issues", {
            "title": title,
            "body": f"@{listing['repo'].split('/')[1]}, the release didn't pass the automatic checks:\n\n{body}\n\n"
                    f"Publish a new version once it's fixed. A version number is never reused.",
        })

