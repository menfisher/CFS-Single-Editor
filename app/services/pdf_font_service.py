from __future__ import annotations

import os
import zlib
from functools import lru_cache
from pathlib import Path

from app.services.preset_service import FONT_FAMILY_OPTIONS

_FONT_FILE_CANDIDATES = {
    "Arial": {
        "regular": ("Arial.ttf", "arial.ttf", "ARIAL.TTF"),
        "bold": ("Arial Bold.ttf", "arialbd.ttf", "Arial-Bold.ttf", "ARIALBD.TTF"),
        "italic": ("Arial Italic.ttf", "ariali.ttf", "Arial-Italic.ttf", "ARIALI.TTF"),
    },
    "Helvetica": {
        "regular": ("Helvetica.ttf", "Helvetica.otf", "helvetica.ttf"),
        "bold": ("Helvetica Bold.ttf", "Helvetica-Bold.ttf", "helvetica-bold.ttf"),
        "italic": ("Helvetica Italic.ttf", "Helvetica-Oblique.ttf", "helvetica-oblique.ttf"),
    },
    "Verdana": {
        "regular": ("Verdana.ttf", "verdana.ttf", "VERDANA.TTF"),
        "bold": ("Verdana Bold.ttf", "verdanab.ttf", "Verdana-Bold.ttf", "VERDANAB.TTF"),
        "italic": ("Verdana Italic.ttf", "verdanai.ttf", "Verdana-Italic.ttf", "VERDANAI.TTF"),
    },
    "Tahoma": {
        "regular": ("Tahoma.ttf", "tahoma.ttf", "TAHOMA.TTF"),
        "bold": ("Tahoma Bold.ttf", "tahomabd.ttf", "Tahoma-Bold.ttf", "TAHOMABD.TTF"),
        "italic": ("Tahoma Italic.ttf", "tahomai.ttf", "Tahoma-Italic.ttf"),
    },
    "Times New Roman": {
        "regular": ("Times New Roman.ttf", "times.ttf", "timesnr.ttf", "TIMES.TTF"),
        "bold": ("Times New Roman Bold.ttf", "timesbd.ttf", "Times New Roman-Bold.ttf", "TIMESBD.TTF"),
        "italic": ("Times New Roman Italic.ttf", "timesi.ttf", "Times New Roman-Italic.ttf", "TIMESI.TTF"),
    },
    "Georgia": {
        "regular": ("Georgia.ttf", "georgia.ttf", "GEORGIA.TTF"),
        "bold": ("Georgia Bold.ttf", "georgiab.ttf", "Georgia-Bold.ttf", "GEORGIAB.TTF"),
        "italic": ("Georgia Italic.ttf", "georgiai.ttf", "Georgia-Italic.ttf", "GEORGIAI.TTF"),
    },
    "Courier New": {
        "regular": ("Courier New.ttf", "cour.ttf", "cour.ttf", "COUR.TTF"),
        "bold": ("Courier New Bold.ttf", "courbd.ttf", "Courier New-Bold.ttf", "COURBD.TTF"),
        "italic": ("Courier New Italic.ttf", "couri.ttf", "Courier New-Italic.ttf", "COURI.TTF"),
    },
}


def _font_search_dirs() -> list[Path]:
    windir = Path(os.environ.get("WINDIR") or r"C:\Windows")
    return [
        Path("/System/Library/Fonts/Supplemental"),
        Path("/Library/Fonts"),
        Path.home() / "Library" / "Fonts",
        windir / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ]


def _find_font_file(filenames: tuple[str, ...]) -> Path | None:
    wanted = {name.lower(): name for name in filenames}
    for folder in _font_search_dirs():
        try:
            if not folder.is_dir():
                continue
            available = {item.name.lower(): item for item in folder.iterdir() if item.is_file()}
        except OSError:
            continue
        for key in wanted:
            match = available.get(key)
            if match and match.suffix.lower() in {".ttf", ".otf"}:
                return match
    return None


@lru_cache(maxsize=32)
def resolve_pdf_font_path(font_family: str, style: str = "regular") -> str | None:
    family = str(font_family or "").strip()
    if family not in FONT_FAMILY_OPTIONS:
        family = "Arial"
    faces = _FONT_FILE_CANDIDATES.get(family) or {}
    filenames = faces.get(style) or faces.get("regular") or ()
    path = _find_font_file(tuple(filenames))
    return str(path) if path else None


def _import_fitz():
    try:
        import fitz

        return fitz
    except ImportError:
        return None


@lru_cache(maxsize=32)
def load_pdf_font(font_family: str, style: str = "regular"):
    path = resolve_pdf_font_path(font_family, style)
    if not path:
        return None
    fitz = _import_fitz()
    if fitz is None:
        return None
    try:
        font = fitz.Font(fontfile=path)
    except Exception:
        return None
    if not font.buffer or font.buffer[:4] == b"ttcf":
        return None
    return font


def pdf_text_width(text: str, size: float, font_family: str, *, bold: bool = False) -> float | None:
    font = load_pdf_font(font_family, "bold" if bold else "regular")
    if font is None:
        return None
    try:
        return float(font.text_length(text, fontsize=size))
    except Exception:
        return None


def _pdf_font_name(font_family: str, style: str) -> str:
    base = "".join(ch for ch in str(font_family) if ch.isalnum()) or "BookFont"
    suffix = {"regular": "", "bold": "-Bold", "italic": "-Italic"}.get(style, "")
    return f"CFS{base}{suffix}"


def add_embedded_pdf_font(add_object, font, font_family: str, style: str) -> int | None:
    if font is None or not getattr(font, "buffer", None) or font.buffer[:4] == b"ttcf":
        return None
    data = bytes(font.buffer)
    compressed = zlib.compress(data, 9)
    fontfile_ref = add_object(
        (
            f"<< /Length {len(compressed)} /Length1 {len(data)} /Filter /FlateDecode >>\nstream\n"
        ).encode("latin-1")
        + compressed
        + b"\nendstream"
    )
    pdf_name = _pdf_font_name(font_family, style)
    bbox = font.bbox
    font_bbox = " ".join(
        str(int(round(value * 1000)))
        for value in (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
    )
    flags = 32
    font_flags = font.flags if isinstance(font.flags, dict) else {}
    if font_flags.get("mono"):
        flags |= 1
    if font_flags.get("serif"):
        flags |= 2
    if style == "italic" or font_flags.get("italic"):
        flags |= 64
    ascent = int(round(float(font.ascender) * 1000))
    descent = int(round(float(font.descender) * 1000))
    stem_v = 120 if style == "bold" or font_flags.get("bold") else 80
    italic_angle = -12 if style == "italic" or font_flags.get("italic") else 0
    descriptor_ref = add_object(
        "<< /Type /FontDescriptor "
        f"/FontName /{pdf_name} /Flags {flags} /FontBBox [{font_bbox}] "
        f"/ItalicAngle {italic_angle} /Ascent {ascent} /Descent {descent} "
        f"/CapHeight {max(ascent, 0)} /StemV {stem_v} /FontFile2 {fontfile_ref} 0 R >>"
    )
    widths = []
    for code in range(32, 256):
        character = bytes([code]).decode("latin-1")
        try:
            widths.append(str(int(round(font.glyph_advance(ord(character)) * 1000))))
        except Exception:
            widths.append("500")
    return add_object(
        "<< /Type /Font /Subtype /TrueType "
        f"/BaseFont /{pdf_name} /Encoding /WinAnsiEncoding "
        f"/FirstChar 32 /LastChar 255 /Widths [{' '.join(widths)}] "
        f"/FontDescriptor {descriptor_ref} 0 R >>"
    )


def add_embedded_pdf_font_set(add_object, font_family: str) -> tuple[int, int, int] | None:
    regular = load_pdf_font(font_family, "regular")
    if regular is None:
        return None
    bold = load_pdf_font(font_family, "bold") or regular
    italic = load_pdf_font(font_family, "italic") or regular
    regular_ref = add_embedded_pdf_font(add_object, regular, font_family, "regular")
    bold_ref = add_embedded_pdf_font(add_object, bold, font_family, "bold")
    italic_ref = add_embedded_pdf_font(add_object, italic, font_family, "italic")
    if None in {regular_ref, bold_ref, italic_ref}:
        return None
    return regular_ref, bold_ref, italic_ref
