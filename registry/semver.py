"""SemVer 2.0.0 precedence."""

import re

PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


def key(version: str):
    m = PATTERN.match(version)
    if not m:
        raise ValueError(f"not a semantic version: {version}")
    core = tuple(int(x) for x in m.groups()[:3])
    if m.group(4) is None:
        return core, (1,)
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in m.group(4).split("."))
    return core, (0, parts)


def newer(a: str, b: str) -> bool:
    return key(a) > key(b)
