import unittest

import fitz

from app.services.address_book_pdf_service import (
    _PdfImage,
    _PdfLine,
    _PdfWriter,
    _TocEntry,
    _build_toc_pages,
    _center_text_x,
    _contact_text_columns,
    _text_width,
    build_address_book_pdf,
)
from app.services.pdf_font_service import load_pdf_font, resolve_pdf_font_path


def _pdf_font_names(pdf: bytes) -> set[str]:
    document = fitz.open(stream=pdf, filetype="pdf")
    try:
        names = set()
        for page in document:
            for font in page.get_fonts(full=True):
                names.add(str(font[3] or ""))
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            names.add(str(span.get("font") or ""))
        return names
    finally:
        document.close()


class PdfEmbeddedFontTests(unittest.TestCase):
    def test_system_verdana_is_embedded_as_truetype(self) -> None:
        if not resolve_pdf_font_path("Verdana", "regular"):
            self.skipTest("Verdana is not installed on this computer")
        writer = _PdfWriter(200, 200, font_family="Verdana")
        page = writer.add_page()
        page.lines.append(_PdfLine("Verdana Sample", x=20, y=100, size=12))
        pdf = writer.bytes()
        names = " ".join(_pdf_font_names(pdf)).lower()
        self.assertIn("verdana", names)
        self.assertNotIn("helvetica", names)
        self.assertNotIn("arial", names)
        self.assertIn(b"/FontFile2", pdf)
        self.assertIn(b"/BaseFont/Verdana", pdf)
        self.assertNotIn(b"/CFSRegular", pdf)
        self.assertIn(b"/WinAnsiEncoding", pdf)
        self.assertNotIn(b"/Verdana#20", pdf)

    def test_verdana_contact_name_wraps_like_the_html_preview(self) -> None:
        if not resolve_pdf_font_path("Verdana", "regular"):
            self.skipTest("Verdana is not installed on this computer")
        def tw(text: str, size: float) -> float:
            return _text_width(text, size, "Verdana")

        _phone_x, _code_x, _main_x, _cont_x, name_max = _contact_text_columns(
            0.14 * 72, 3.5 * 72, 0.14 * 72, 7, tw
        )
        full = "GRAHAM, Mark & Stephanie; Ruth, Harrison, Extra"
        head = "GRAHAM, Mark & Stephanie;"
        self.assertGreater(tw(full, 7), name_max)
        self.assertLessEqual(tw(head, 7), name_max)

    def test_phone_letter_slot_is_two_characters_plus_one_space(self) -> None:
        if not resolve_pdf_font_path("Verdana", "regular"):
            self.skipTest("Verdana is not installed on this computer")

        def tw(text: str, size: float) -> float:
            return _text_width(text, size, "Verdana")

        phone_x, phone_code_x, main_text_x, _cont_x, _name_max = _contact_text_columns(
            0.14 * 72, 3.5 * 72, 0.14 * 72, 7, tw
        )
        ch = tw("0", 7)
        self.assertAlmostEqual((phone_code_x - phone_x) / ch, 13.0, places=5)
        self.assertAlmostEqual((main_text_x - phone_code_x) / ch, 3.0, places=5)
        after_two_letter_code = phone_code_x + (ch * 2)
        self.assertAlmostEqual((main_text_x - after_two_letter_code) / ch, 1.0, places=5)

    def test_toc_uses_chosen_title_and_entry_font_sizes(self) -> None:
        pages = _build_toc_pages(
            entries=[_TocEntry(field_name="Field A", meeting_heading="APEX", page=1)],
            book_title="NC Address Book",
            page_width=252,
            page_height=360,
            margin_left=10,
            margin_right=10,
            margin_top=10,
            margin_bottom=10,
            base_font_size=7,
            page_offset=0,
            compact=False,
            font_family="Arial",
            title_font_size=8,
            toc_font_size=7,
        )
        self.assertTrue(pages)
        sizes = {line.size for line in pages[0].lines}
        self.assertIn(8.0, sizes)
        self.assertIn(7.0, sizes)
        title = next(line for line in pages[0].lines if line.text.startswith("NC Address Book"))
        entry = next(line for line in pages[0].lines if line.text == "APEX")
        self.assertEqual(title.size, 8.0)
        self.assertEqual(entry.size, 7.0)
        self.assertAlmostEqual(
            title.x,
            _center_text_x(title.text, title.size, 252, 10, 10, "Arial", bold=True),
            places=4,
        )

    def test_address_book_pdf_keeps_verdana_instead_of_arial(self) -> None:
        if not resolve_pdf_font_path("Verdana", "regular"):
            self.skipTest("Verdana is not installed on this computer")
        pdf = build_address_book_pdf(
            fields=[
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
            ],
            meeting_preview_sections=[],
            settings={
                "address_map_provider": "google",
                "address_align": "right",
                "include_bible_study_union_info": 0,
                "meeting_page_mode": "one_meeting",
            },
            layout={
                "trim_width_in": 3.5,
                "trim_height_in": 5.5,
                "margin_left_in": 0.14,
                "margin_right_in": 0.14,
                "margin_top_in": 0.14,
                "margin_bottom_in": 0.14,
                "base_font_size_pt": 7,
                "line_height": 1.2,
                "font_family": "Verdana",
                "book_title": "Verdana Book",
            },
            print_order_mode="alphabetical",
            print_order_json="",
        )
        names = " ".join(_pdf_font_names(pdf)).lower()
        self.assertIn("verdana", names)
        self.assertNotIn("helvetica", names)
        self.assertNotIn("arial", names)
        self.assertIn(b"/FontFile2", pdf)

    def test_helvetica_keeps_the_built_in_pdf_font_when_no_ttf_is_installed(self) -> None:
        if resolve_pdf_font_path("Helvetica", "regular"):
            self.skipTest("A Helvetica TTF is installed, so it would be embedded")
        writer = _PdfWriter(200, 200, font_family="Helvetica")
        page = writer.add_page()
        page.lines.append(_PdfLine("Helvetica Sample", x=20, y=100, size=12))
        pdf = writer.bytes()
        names = " ".join(_pdf_font_names(pdf)).lower()
        self.assertIn("helv", names)
        self.assertNotIn("verdana", names)
        self.assertNotIn(b"/FontFile2", pdf)

    def test_flate_rgb_insert_image_does_not_crash_the_writer(self) -> None:
        import zlib

        rgb = bytes([20, 80, 160]) * (8 * 10)
        writer = _PdfWriter(80, 100, font_family="Verdana")
        page = writer.add_page()
        page.images.append(
            _PdfImage(
                zlib.compress(rgb, 9),
                8,
                10,
                0,
                0,
                80,
                100,
                filter_name="FlateDecode",
            )
        )
        page.lines.append(_PdfLine("Verdana Sample", x=8, y=20, size=10))
        pdf = writer.bytes()
        document = fitz.open(stream=pdf, filetype="pdf")
        try:
            self.assertTrue(document[0].get_images())
            names = " ".join(_pdf_font_names(pdf)).lower()
            self.assertIn("verdana", names)
        finally:
            document.close()

    def test_embedded_font_width_differs_from_helvetica_fallback(self) -> None:
        if load_pdf_font("Verdana", "regular") is None:
            self.skipTest("Verdana is not installed on this computer")
        verdana = _text_width("Washington", 12, "Verdana")
        helvetica = _text_width("Washington", 12, "Helvetica")
        self.assertNotAlmostEqual(verdana, helvetica, places=2)


if __name__ == "__main__":
    unittest.main()
