"""What a version asks for beyond the one before it."""


def increase(old: dict | None, new: dict) -> list[str]:
    """Plain-language list of what `new` asks for that `old` didn't. `old` is None for a first version."""
    before = (old or {}).get("permissions", {})
    after = new.get("permissions", {})
    out = []
    if new.get("tier") == "unrestricted" and (old or {}).get("tier") != "unrestricted":
        out.append("the unrestricted tier")
    if "documents.all" in after and "documents.all" not in before:
        out.append("every open document (documents.all)")
    if "vcs.read" in after and "vcs.read" not in before:
        out.append("the git repository's branches, tags and history (vcs.read)")
    old_hosts = set(before.get("network", {}).get("hosts", []))
    for host in after.get("network", {}).get("hosts", []):
        if host not in old_hosts:
            out.append(f"network host {host}")
    old_files = {f["id"]: f for f in before.get("files", [])}
    for f in after.get("files", []):
        was = old_files.get(f["id"])
        if was is None:
            out.append(f"folder “{f['title']}”" + (" (writable)" if f.get("writable") else ""))
        elif f.get("writable") and not was.get("writable"):
            out.append(f"writing to folder “{f['title']}”")
    return out
