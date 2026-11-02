"""Icon, screenshot and README rules, read from file headers only."""

import html
import re
import struct
import xml.etree.ElementTree as ET

MAX_PNG_ICON = 256 * 1024
MAX_SVG_ICON = 64 * 1024
MAX_SCREENSHOT = 2 * 1024 * 1024
MAX_MEDIA_TOTAL = 8 * 1024 * 1024
MAX_README = 128 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class MediaError(Exception):
    """Worded like HikariSub's Media refusal: the file's path, then what's wrong with it."""

    def __init__(self, path: str, detail: str):
        super().__init__(f"The package breaks the media rules: {path} {detail}.")
        self.path, self.detail = path, detail


def _pixels(w: int, h: int) -> str:
    return f"{w}x{h} px"


def png_size(path: str, data: bytes) -> tuple[int, int]:
    if not data.startswith(PNG_SIGNATURE) or len(data) < 24 or data[12:16] != b"IHDR":
        raise MediaError(path, "isn't a PNG")
    return struct.unpack(">II", data[16:24])


def jpeg_size(path: str, data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\xff\xd8\xff"):
        raise MediaError(path, "isn't a JPEG")
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    raise MediaError(path, "has no JPEG frame header")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))", re.S)


def css_unescape(text: str) -> str:
    def one(m: re.Match) -> str:
        if m.group(1):
            n = int(m.group(1), 16)
            return chr(n) if 0 < n <= 0x10FFFF and not 0xD800 <= n <= 0xDFFF else "\ufffd"
        return "" if m.group(2) == "\n" else m.group(2)
    return _CSS_ESCAPE.sub(one, text)


def _css_problem(value: str) -> str | None:
    """Presentation attributes are parsed as CSS, so read them the way a browser would."""
    folded = css_unescape(value).lower()
    if "@import" in folded:
        return "imports a style sheet"
    if "expression(" in folded:
        return "has a CSS expression("
    for target in re.findall(r"url\(\s*['\"]?(.?)", folded):
        if target != "#":
            return "has a url( that points outside the file"
    return None


def check_svg(path: str, data: bytes) -> None:
    """PL1's SVG rules, plus one of the registry's own: no <style> element and no style attribute."""
    if len(data) > MAX_SVG_ICON:
        raise MediaError(path, "is larger than 64 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise MediaError(path, "isn't UTF-8") from e
    doctype = re.search(r"<!DOCTYPE[^>]*", text, re.I)
    if doctype and "[" in doctype.group(0) or "<!ENTITY" in text:
        raise MediaError(path, "declares entities or attributes in its DOCTYPE")
    try:
        root = ET.fromstring(text)  # character references are decoded here, before any check below
    except ET.ParseError as e:
        raise MediaError(path, "isn't well-formed XML") from e
    if _local(root.tag) != "svg":
        raise MediaError(path, "has a root element other than <svg>")
    box = (root.get("viewBox") or "").replace(",", " ").split()
    try:
        square = len(box) == 4 and float(box[2]) == float(box[3]) > 0
    except ValueError:
        square = False
    if not square:
        raise MediaError(path, "has no square viewBox on its <svg>")
    for el in root.iter():
        tag = _local(el.tag)
        if tag in ("script", "foreignObject", "style"):
            raise MediaError(path, f"holds a <{tag}> element")
        for name, value in el.attrib.items():
            local = _local(name)
            if local.lower().startswith("on"):
                raise MediaError(path, f"has an {local} attribute")
            if local == "style":
                raise MediaError(path, "has a style attribute; use presentation attributes such as fill instead")
            if local.lower() == "href" and not value.startswith("#"):
                raise MediaError(path, f"has an {local} that points outside the file")
            if problem := _css_problem(value):
                raise MediaError(path, problem)
    if (problem := _css_problem(html.unescape(text))) and "url(" in problem:  # anywhere in the file, as PL1 does
        raise MediaError(path, problem)


def check_icon(path: str, data: bytes) -> None:
    if path.endswith(".svg"):
        check_svg(path, data)
        return
    if len(data) > MAX_PNG_ICON:
        raise MediaError(path, "is larger than 256 KiB")
    size = png_size(path, data)
    if size != (256, 256):
        raise MediaError(path, f"is {_pixels(*size)}, not 256x256 px")


def check_screenshot(path: str, data: bytes) -> tuple[int, int]:
    if len(data) > MAX_SCREENSHOT:
        raise MediaError(path, "is larger than 2 MiB")
    w, h = png_size(path, data) if path.endswith(".png") else jpeg_size(path, data)
    if not (1280 <= w <= 1920 and 720 <= h <= 1080) or abs(w * 9 - h * 16) > 16:
        raise MediaError(path, f"is {_pixels(w, h)}; a screenshot is 16:9, from 1280x720 to 1920x1080 px")
    return w, h


def check_readme(data: bytes) -> str:
    if len(data) > MAX_README:
        raise MediaError("README.md", "is larger than 128 KiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise MediaError("README.md", "isn't UTF-8") from e
