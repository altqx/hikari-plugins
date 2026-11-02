"""Icon, screenshot and README rules, read from file headers only."""

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
    pass


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR" or len(data) < 24:
        raise MediaError("not a PNG")
    return struct.unpack(">II", data[16:24])


def jpeg_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\xff\xd8\xff"):
        raise MediaError("not a JPEG")
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            raise MediaError("damaged JPEG")
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + length
    raise MediaError("JPEG without a frame header")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def check_svg(data: bytes) -> None:
    if len(data) > MAX_SVG_ICON:
        raise MediaError("SVG icon over 64 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise MediaError("SVG icon is not UTF-8") from e
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise MediaError("SVG icon has a DOCTYPE")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise MediaError("SVG icon is not XML") from e
    if _local(root.tag) != "svg":
        raise MediaError("SVG icon's root is not <svg>")
    box = (root.get("viewBox") or "").replace(",", " ").split()
    if len(box) != 4 or float(box[2]) != float(box[3]) or float(box[2]) <= 0:
        raise MediaError("SVG icon needs a square viewBox")
    for el in root.iter():
        if _local(el.tag) in ("script", "foreignObject"):
            raise MediaError(f"SVG icon has <{_local(el.tag)}>")
        for name, value in el.attrib.items():
            local = _local(name)
            if local.lower().startswith("on"):
                raise MediaError(f"SVG icon has an {local} attribute")
            if local == "href" and not value.startswith("#"):
                raise MediaError("SVG icon links outside itself")
        css = [*el.attrib.values(), el.text or ""] if _local(el.tag) == "style" else list(el.attrib.values())
        if "@import" in " ".join(css):
            raise MediaError("SVG icon imports a style sheet")
        for target in re.findall(r"url\(\s*['\"]?([^'\")]*)", " ".join(css)):
            if not target.startswith("#"):
                raise MediaError("SVG icon refers to an outside url()")


def check_icon(path: str, data: bytes) -> None:
    if path.endswith(".svg"):
        check_svg(data)
        return
    if len(data) > MAX_PNG_ICON:
        raise MediaError("PNG icon over 256 KiB")
    if png_size(data) != (256, 256):
        raise MediaError("PNG icon must be 256 × 256 px")


def check_screenshot(path: str, data: bytes) -> tuple[int, int]:
    if len(data) > MAX_SCREENSHOT:
        raise MediaError(f"{path} is over 2 MiB")
    w, h = png_size(data) if path.endswith(".png") else jpeg_size(data)
    if not (1280 <= w <= 1920 and 720 <= h <= 1080) or abs(w * 9 - h * 16) > 16:
        raise MediaError(f"{path} is {w} × {h}; screenshots are 16:9, from 1280 × 720 to 1920 × 1080")
    return w, h


def check_readme(data: bytes) -> str:
    if len(data) > MAX_README:
        raise MediaError("README.md is over 128 KiB")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise MediaError("README.md is not UTF-8") from e
