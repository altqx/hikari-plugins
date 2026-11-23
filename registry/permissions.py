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
    if "programs" in after and "programs" not in before:
        out.append("running its programs")
    old_programs = {p["id"]: p for p in programs(old or {})}
    for p in programs(new):
        was = old_programs.get(p["id"])
        if was is None:
            out.append(f"program {p['id']}" + (" with the internet" if p.get("network") else ""))
        elif p.get("network") and not was.get("network"):
            out.append(f"the internet for program {p['id']}")
        if p.get("confinement") == "lowIntegrity" and (was or {}).get("confinement") != "lowIntegrity":
            out.append(f"program {p['id']} at low integrity, which can read your files")
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


def programs(manifest: dict) -> list[dict]:
    """What review compares of each program: its id, network and confinement."""
    found = manifest.get("programs") or manifest.get("contributes", {}).get("programs", [])
    return [{k: p[k] for k in ("id", "network", "confinement") if k in p} for p in found]
