"""HikariSub's theme loader (altqx/hikari src/themes/theme.cpp): the theme schema, then the contrast check."""

import json
import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import jsonschema

SCHEMA = Path(__file__).resolve().parent.parent / "schemas" / "hikari-theme.schema.json"
ROLES = ("background", "panel", "raised", "field", "text", "muted", "line", "control", "disabled",
         "accent", "onAccent", "select", "hover", "pressed", "focus", "danger", "warning", "success")
SURFACES = ("background", "panel", "raised", "field")
HOVER_ALPHA = {"light": 13, "dark": 15, "highContrastLight": 20, "highContrastDark": 31}
BLACK, WHITE = 0xFF000000, 0xFFFFFFFF


@dataclass
class Problem:
    path: str
    message: str
    stage: str


class ThemeError(Exception):
    def __init__(self, problem: Problem):
        super().__init__(f"{problem.path or '/'}: {problem.message}")
        self.problem = problem


def colour(text: str) -> int:
    value = int(text[1:], 16)
    return BLACK | value if len(text) == 7 else (value & 0xFF) << 24 | value >> 8


def with_alpha(c: int, alpha: int) -> int:
    return (c & 0xFFFFFF) | (alpha & 0xFF) << 24


def luminance(c: int) -> float:
    def channel(v: int) -> float:
        s = v / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(c >> 16 & 0xFF) + 0.7152 * channel(c >> 8 & 0xFF) + 0.0722 * channel(c & 0xFF)


def contrast_ratio(a: int, b: int) -> float:
    x, y = luminance(a), luminance(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def composite(over: int, under: int) -> int:
    a = (over >> 24) / 255

    def mix(shift: int) -> int:
        # std::lround: halves away from zero, unlike round()
        return math.floor((over >> shift & 0xFF) * a + (under >> shift & 0xFF) * (1 - a) + 0.5) << shift
    return BLACK | mix(16) | mix(8) | mix(0)


def readable_on(background: int) -> int:
    return BLACK if contrast_ratio(BLACK, background) >= contrast_ratio(WHITE, background) else WHITE


def ratio_text(ratio: float) -> str:
    return f"{math.floor(ratio * 100) / 100:.2f}"


def hex_text(c: int) -> str:
    return f"#{c & 0xFFFFFF:06X}"


@cache
def _validator():
    return jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))


def _schema_problem(theme) -> Problem | None:
    e = jsonschema.exceptions.best_match(_validator().iter_errors(theme))
    if e is None:
        return None
    message = e.message if len(e.message) <= 200 else f"fails the theme schema's {e.validator}"
    return Problem("".join(f"/{p}" for p in e.absolute_path), message, "schema")


def _optional(obj: dict, key: str) -> int | None:
    return colour(obj[key]) if key in obj else None


def _resolve(theme: dict, choice: dict | None) -> dict:
    given = {r: _optional(theme["roles"], r) for r in ROLES}
    if choice:
        given["accent"], given["select"] = colour(choice["accent"]), colour(choice["select"])
        given["onAccent"] = _optional(choice, "onAccent")
    roles = {r: given[r] or 0 for r in ROLES}
    text = roles["text"]
    if given["focus"] is None:
        roles["focus"] = text
    if given["control"] is None:
        roles["control"] = roles["line"]
    if given["onAccent"] is None:
        roles["onAccent"] = readable_on(roles["accent"])
    if given["hover"] is None:
        roles["hover"] = with_alpha(text, HOVER_ALPHA[theme["type"]])
    if given["pressed"] is None:
        roles["pressed"] = with_alpha(text, min(255, 2 * (roles["hover"] >> 24)))
    return roles


def _default_accent(theme: dict) -> dict | None:
    plain = _resolve(theme, None)
    for choice in theme.get("accents", []):
        on = _optional(choice, "onAccent")
        if (colour(choice["accent"]) == plain["accent"] and colour(choice["select"]) == plain["select"]
                and (on if on is not None else readable_on(colour(choice["accent"]))) == plain["onAccent"]):
            return choice
    return None


def _check_one(theme: dict, roles: dict, note: str) -> Problem | None:
    kind = theme["type"]
    hc = kind.startswith("highContrast")
    text_floor = 7 if hc else 4.5
    content = theme["content"]
    audio = content["audio"]
    checks = []

    def need(fore: int, fore_name: str, back: int, back_name: str, floor: float):
        checks.append((fore, fore_name, back, back_name, floor))

    def on_surfaces(role: str, floor: float):
        for surface in SURFACES:
            need(roles[role], f"/roles/{role}", roles[surface], surface, floor)

    for role in ("text", "muted", "danger", "warning", "accent"):
        on_surfaces(role, text_floor)
    if kind != "light":
        on_surfaces("success", text_floor)
    if hc:
        on_surfaces("line", 7)
    for role in ("focus", "disabled", "control"):
        on_surfaces(role, 3)
    need(roles["onAccent"], "/roles/onAccent", roles["accent"], "accent", text_floor)
    need(roles["text"], "/roles/text", roles["select"], "select", text_floor)
    need(roles["disabled"], "/roles/disabled", roles["text"], "text", 2)
    spellcheck = colour(content["spellcheck"])
    need(spellcheck, "/content/spellcheck", roles["field"], "field", 3)
    need(spellcheck, "/content/spellcheck", roles["panel"], "panel", 3)
    need(spellcheck, "/content/spellcheck", composite(colour(content["grid"]["alternate"]), roles["field"]),
         "grid alternate", 3)
    need(colour(content["grammar"]), "/content/grammar", roles["field"], "field", 3)
    if kind != "dark":
        background = colour(audio["background"])
        need(colour(audio["waveform"]), "/content/audio/waveform", background, "audio background", 4.5 if hc else 3)
        need(colour(audio["cursor"]), "/content/audio/cursor", background, "audio background", 3)
        need(colour(audio["timescaleText"]), "/content/audio/timescaleText", colour(audio["timescaleBackground"]),
             "timescale background", 4.5)

    for fore, fore_name, back, back_name, floor in checks:
        under = composite(back, roles["panel"])
        ratio = contrast_ratio(composite(fore, under), under)
        if ratio + 1e-9 < floor:
            return Problem(fore_name, f"{fore_name} {hex_text(fore)} on {back_name} {hex_text(back)}{note} measures "
                                      f"{ratio_text(ratio)}:1, below {ratio_text(floor)}:1", "contrast")
    return None


def problem(theme) -> Problem | None:
    """The first problem HikariSub's themes::load reports for a parsed theme file, or None."""
    if found := _schema_problem(theme):
        return found
    accents = theme.get("accents", [])
    seen = set()
    for i, choice in enumerate(accents):
        if choice["id"] in seen:
            return Problem(f"/accents/{i}/id", "repeats an earlier id", "schema")
        seen.add(choice["id"])
    if accents and not _default_accent(theme):
        return Problem("/accents", "no choice equals the roles' accent, onAccent and select", "schema")
    user = theme.get("userColours", [])
    for i, role in enumerate(user):
        if role in user[:i]:
            return Problem(f"/userColours/{i}", "repeats an earlier role", "schema")
    if not accents:
        return _check_one(theme, _resolve(theme, None), "")
    for choice in accents:
        if found := _check_one(theme, _resolve(theme, choice), f" with the accent {choice['id']}"):
            return found
    return None


def load(data: bytes) -> dict:
    try:
        theme = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ThemeError(Problem("", f"isn't JSON: {e}", "schema")) from e
    if found := problem(theme):
        raise ThemeError(found)
    return theme
