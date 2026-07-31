#!/usr/bin/env python3
"""Smoke test: insert file page after TOC with corrected page numbers."""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from app.services.address_book_pdf_service import (  # noqa: E402
    _build_insert_pages,
    build_address_book_pdf,
)

_LAYOUT = {
    "trim_width_in": 3.5,
    "trim_height_in": 5.5,
    "margin_left_in": 0.14,
    "margin_right_in": 0.14,
    "margin_top_in": 0.14,
    "margin_bottom_in": 0.14,
    "base_font_size_pt": 7,
    "line_height": 1.2,
    "font_family": "Arial",
    "book_title": "Insert Test Book",
}

_SETTINGS = {
    "address_map_provider": "google",
    "address_align": "right",
    "include_bible_study_union_info": 0,
    "meeting_page_mode": "one_meeting",
}

_FIELDS = [
    {
        "name": "Field A",
        "meetings": [
            {
                "name": "Meeting 1",
                "contacts": [
                    {
                        "name": "DOE, John",
                        "phones": [{"value": "5551234567", "code": "HM"}],
                        "addresses": [],
                        "children": [],
                        "other_relationships": [],
                    }
                ],
            }
        ],
    }
]


def _pdf_page_count(pdf: bytes) -> int:
    document = fitz.open(stream=pdf, filetype="pdf")
    try:
        return document.page_count
    finally:
        document.close()


def _pdf_texts(pdf: bytes) -> list[str]:
    texts: list[str] = []
    stream = pdf.decode("latin-1", errors="replace")
    for part in re.findall(r"stream\n(.*?)\nendstream", stream, re.DOTALL):
        for block in re.findall(r"BT(.*?)ET", part, re.DOTALL):
            for match in re.finditer(r"\(([^)]*)\)\s*Tj", block):
                texts.append(match.group(1).replace("\\(", "(").replace("\\)", ")"))
    return texts


def _tiny_jpeg_data_url() -> tuple[str, int, int]:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 180), 0)
    pixmap.set_rect(fitz.Rect(0, 0, 120, 180), (20, 80, 160))
    jpeg_bytes = pixmap.tobytes("jpeg", jpg_quality=85)
    encoded = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}", 120, 180


def _sample_pdf_base64() -> str:
    document = fitz.open()
    page = document.new_page(width=252, height=396)
    page.insert_text((36, 48), "Inserted PDF page")
    pdf_bytes = document.tobytes()
    document.close()
    return base64.b64encode(pdf_bytes).decode("ascii")


def _build_pdf(**kwargs) -> bytes:
    return build_address_book_pdf(
        fields=_FIELDS,
        meeting_preview_sections=[],
        layout=_LAYOUT,
        settings=_SETTINGS,
        print_order_mode="alphabetical",
        print_order_json="{}",
        **kwargs,
    )


def test_insert_page_builder_returns_one_page_for_jpeg() -> None:
    data_url, width, height = _tiny_jpeg_data_url()
    page_width = float(_LAYOUT["trim_width_in"]) * 72.0
    page_height = float(_LAYOUT["trim_height_in"]) * 72.0
    pages = _build_insert_pages(
        include_insert_file=True,
        insert_file_data=data_url,
        insert_file_mime="image/jpeg",
        insert_file_width=width,
        insert_file_height=height,
        page_width=page_width,
        page_height=page_height,
    )
    assert len(pages) == 1
    assert pages[0].images


def test_insert_page_builder_returns_one_page_for_pdf() -> None:
    page_width = float(_LAYOUT["trim_width_in"]) * 72.0
    page_height = float(_LAYOUT["trim_height_in"]) * 72.0
    pages = _build_insert_pages(
        include_insert_file=True,
        insert_file_data=_sample_pdf_base64(),
        insert_file_mime="application/pdf",
        insert_file_width=0,
        insert_file_height=0,
        page_width=page_width,
        page_height=page_height,
    )
    assert len(pages) == 1
    assert pages[0].images


def test_insert_file_adds_page_and_shifts_content_footer() -> None:
    baseline = _build_pdf()
    data_url, width, height = _tiny_jpeg_data_url()
    with_insert = _build_pdf(
        include_insert_file=True,
        insert_file_data=data_url,
        insert_file_mime="image/jpeg",
        insert_file_width=width,
        insert_file_height=height,
    )
    assert _pdf_page_count(with_insert) == _pdf_page_count(baseline) + 1
    assert "Field A - Page 3" in _pdf_texts(with_insert)


def test_cover_and_insert_both_shift_content_footer() -> None:
    data_url, width, height = _tiny_jpeg_data_url()
    pdf = _build_pdf(
        cover_image_data=data_url,
        cover_image_width=width,
        cover_image_height=height,
        include_insert_file=True,
        insert_file_data=data_url,
        insert_file_mime="image/jpeg",
        insert_file_width=width,
        insert_file_height=height,
    )
    assert "Field A - Page 4" in _pdf_texts(pdf)


if __name__ == "__main__":
    test_insert_page_builder_returns_one_page_for_jpeg()
    test_insert_page_builder_returns_one_page_for_pdf()
    test_insert_file_adds_page_and_shifts_content_footer()
    test_cover_and_insert_both_shift_content_footer()
    print("address book insert file smoke tests passed")
