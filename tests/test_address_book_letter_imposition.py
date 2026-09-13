import unittest

from app.services.address_book_pdf_service import (
    _PdfPage,
    _draw_cut_marks,
    _letter_block_offset_x,
    _letter_positions,
)


class AddressBookLetterImpositionTests(unittest.TestCase):
    def test_front_odd_pages_are_left_flush(self) -> None:
        page_width = 3.5 * 72.0
        page_height = 5.0 * 72.0
        letter_height = 11.0 * 72.0
        positions = _letter_positions(page_width, page_height, right_justify=False)
        self.assertEqual(positions[0][0], 0.0)
        self.assertEqual(positions[1][0], page_width)
        self.assertEqual(positions[0][1], letter_height - page_height)
        self.assertEqual(positions[2][1], letter_height - (2.0 * page_height))
        self.assertEqual(positions[0][1] + page_height, letter_height)

    def test_back_even_pages_are_fully_right_flush(self) -> None:
        page_width = 3.5 * 72.0
        letter_width = 8.5 * 72.0
        spare = letter_width - (page_width * 2.0)
        positions = _letter_positions(page_width, 5.0 * 72.0, right_justify=True)
        self.assertEqual(positions[0][0], spare)
        self.assertEqual(positions[1][0] + page_width, letter_width)
        self.assertEqual(_letter_block_offset_x(page_width, right_justify=True), spare)

    def test_front_cut_marks_align_with_left_flush_content(self) -> None:
        page_width = 3.5 * 72.0
        page_height = 5.0 * 72.0
        letter_height = 11.0 * 72.0
        letter = _PdfPage(8.5 * 72.0, letter_height)
        _draw_cut_marks(letter, page_width, page_height, right_justify=False)
        xs = {round(rule.x1, 3) for rule in letter.rules} | {round(rule.x2, 3) for rule in letter.rules}
        ys = {round(rule.y1, 3) for rule in letter.rules} | {round(rule.y2, 3) for rule in letter.rules}
        self.assertIn(0.0, xs)
        self.assertIn(round(page_width, 3), xs)
        self.assertIn(round(page_width * 2.0, 3), xs)
        self.assertEqual(_letter_positions(page_width, page_height, right_justify=False)[0][0], 0.0)
        self.assertIn(round(letter_height, 3), ys)


if __name__ == "__main__":
    unittest.main()
