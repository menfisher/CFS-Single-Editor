import unittest
from unittest import mock

from app.services.address_format import address_display_lines, build_formatted_address, enrich_address_parts


class AddressDisplayLinesTests(unittest.TestCase):
    def test_formatted_address_puts_street2_on_its_own_line(self) -> None:
        self.assertEqual(
            build_formatted_address(
                "Riverwalk Senior's Living",
                "50001 Old Montgomery Hwy #204",
                "Tuscaloosa",
                "AL",
                "35005",
            ),
            "Riverwalk Senior's Living\n50001 Old Montgomery Hwy #204\nTuscaloosa, AL 35005",
        )

    def test_enrich_rebuilds_multiline_formatted_from_structured_fields(self) -> None:
        enriched = enrich_address_parts(
            street_address="Riverwalk Senior's Living",
            extended_address="50001 Old Montgomery Hwy #204",
            city="Tuscaloosa",
            region="AL",
            postal_code="35005",
            formatted_address="Riverwalk Senior's Living, 50001 Old Montgomery Hwy #204, Tuscaloosa, AL 35005",
        )
        self.assertEqual(
            enriched["formatted_address"],
            "Riverwalk Senior's Living\n50001 Old Montgomery Hwy #204\nTuscaloosa, AL 35005",
        )

    def test_street_and_unit_print_on_separate_lines(self) -> None:
        lines = address_display_lines(
            street_address="2130 E McNeese St",
            extended_address="#11",
            city="Lake Charles",
            region="LA",
            postal_code="70607",
        )
        self.assertEqual(lines, ["2130 E McNeese St", "#11, Lake Charles, LA 70607"])

    def test_wrap_keeps_street_and_unit_together(self) -> None:
        lines = address_display_lines(
            street_address="2130 E McNeese St",
            extended_address="#11",
            city="Lake Charles",
            region="LA",
            postal_code="70607",
            # Wide enough for street 1, not street 2 + city.
            max_width=130,
            text_width=lambda text: float(len(text) * 5),
        )
        self.assertEqual(
            lines,
            ["2130 E McNeese St", "#11", "Lake Charles, LA 70607"],
        )

    def test_single_street_wraps_city_only(self) -> None:
        lines = address_display_lines(
            street_address="123 Main St",
            city="Lake Charles",
            region="LA",
            postal_code="70601",
            max_width=80,
            text_width=lambda text: float(len(text) * 5),
        )
        self.assertEqual(lines, ["123 Main St", "Lake Charles, LA 70601"])

    def test_long_street_wraps_at_comma_before_city(self) -> None:
        lines = address_display_lines(
            street_address="Regency Retirement Village of Tuscaloosa",
            extended_address="5001 Old Montgomery Hwy #204",
            city="Tuscaloosa",
            region="AL",
            postal_code="35405",
            max_width=220,
            text_width=lambda text: float(len(text) * 5),
        )
        self.assertEqual(
            lines,
            [
                "Regency Retirement Village of Tuscaloosa",
                "5001 Old Montgomery Hwy #204",
                "Tuscaloosa, AL 35405",
            ],
        )

    def test_work_label_stays_on_one_line_when_full_width_fits(self) -> None:
        one_line = "Work: 2650 Corporate Park Dr, Opelika, AL 36801"
        lines = address_display_lines(
            street_address="2650 Corporate Park Dr",
            city="Opelika",
            region="AL",
            postal_code="36801",
            label="Work",
            max_width=float(len(one_line) * 5),
            text_width=lambda text: float(len(text) * 5),
        )
        self.assertEqual(lines, [one_line])

    def test_work_prefix_in_text_is_not_treated_as_street_two(self) -> None:
        lines = address_display_lines(
            "Work: 2650 Corporate Park Dr\nOpelika, AL 36801",
            max_width=float(len("Work: 2650 Corporate Park Dr, Opelika, AL 36801") * 5),
            text_width=lambda text: float(len(text) * 5),
        )
        self.assertEqual(lines, ["Work: 2650 Corporate Park Dr, Opelika, AL 36801"])

    def test_work_label_is_included_in_one_line_fit(self) -> None:
        unlabeled = "2650 Corporate Park Dr, Opelika, AL 36801"
        labeled = "Work: 2650 Corporate Park Dr, Opelika, AL 36801"
        lines = address_display_lines(
            street_address="2650 Corporate Park Dr",
            city="Opelika",
            region="AL",
            postal_code="36801",
            label="Work",
            max_width=float(len(unlabeled) * 5),
            text_width=lambda text: float(len(text) * 5),
        )
        self.assertEqual(lines, ["Work: 2650 Corporate Park Dr", "Opelika, AL 36801"])
        self.assertGreater(len(labeled) * 5, len(unlabeled) * 5)


class WorkAddressPrintWidthTests(unittest.TestCase):
    def _name_column_width(self, font_size: float, family: str = "Arial") -> float:
        from app.services.address_book_pdf_service import _text_width

        page_width = 3.5 * 72
        margin = 0.14 * 72
        space = _text_width(" ", font_size, family)
        phone_code_x = margin + _text_width("000-000-0000", font_size, family) + space
        main_text_x = phone_code_x + _text_width("MM", font_size, family) + space
        return page_width - margin - (main_text_x + space)

    def test_pocket_book_work_address_fits_in_name_column(self) -> None:
        from app.services.address_book_pdf_service import _text_width

        font_size = 7.0
        name_column = self._name_column_width(font_size)
        one_line = "Work: 2650 Corporate Park Dr, Opelika, AL 36801"
        self.assertLessEqual(_text_width(one_line, font_size, "Arial"), name_column)
        lines = address_display_lines(
            street_address="2650 Corporate Park Dr",
            city="Opelika",
            region="AL",
            postal_code="36801",
            label="Work",
            max_width=name_column,
            text_width=lambda value, size=font_size: _text_width(value, size, "Arial"),
        )
        self.assertEqual(lines, [one_line])

    def test_long_home_address_wraps_instead_of_using_phone_column(self) -> None:
        from app.services.address_book_pdf_service import _text_width

        font_size = 7.0
        name_column = self._name_column_width(font_size)
        one_line = "1740 Edgar D Nixon Ave #104, Montgomery, AL 36104"
        self.assertGreater(_text_width(one_line, font_size, "Arial"), name_column)
        lines = address_display_lines(
            street_address="1740 Edgar D Nixon Ave #104",
            city="Montgomery",
            region="AL",
            postal_code="36104",
            max_width=name_column,
            text_width=lambda value, size=font_size: _text_width(value, size, "Arial"),
        )
        self.assertEqual(lines, ["1740 Edgar D Nixon Ave #104", "Montgomery, AL 36104"])


class AddressBookPrintMeetingDataTests(unittest.TestCase):
    def test_print_settings_always_include_meeting_data(self) -> None:
        from app.routes.address_book import _address_book_pdf_print_settings

        class _Form(dict):
            def get(self, key, default=None):
                return super().get(key, default)

        with mock.patch(
            "app.routes.address_book.save_address_book_settings",
            side_effect=lambda values: dict(values),
        ):
            settings = _address_book_pdf_print_settings(_Form())
        self.assertEqual(settings.get("include_bible_study_union_info"), 1)


if __name__ == "__main__":
    unittest.main()

