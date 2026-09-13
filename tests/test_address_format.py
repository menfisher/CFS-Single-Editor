import unittest

from app.services.address_format import address_display_lines


class AddressDisplayLinesTests(unittest.TestCase):
    def test_street_and_unit_prefer_one_line_with_city(self) -> None:
        lines = address_display_lines(
            street_address="2130 E McNeese St",
            extended_address="#11",
            city="Lake Charles",
            region="LA",
            postal_code="70607",
        )
        self.assertEqual(lines, ["2130 E McNeese St, #11, Lake Charles, LA 70607"])

    def test_wrap_keeps_street_and_unit_together(self) -> None:
        lines = address_display_lines(
            street_address="2130 E McNeese St",
            extended_address="#11",
            city="Lake Charles",
            region="LA",
            postal_code="70607",
            # Wide enough for street+#11 (22 chars), not the full address (46).
            max_width=130,
            text_width=lambda text: float(len(text) * 5),
        )
        self.assertEqual(
            lines,
            ["2130 E McNeese St, #11", "Lake Charles, LA 70607"],
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
                "Regency Retirement Village of Tuscaloosa,",
                "5001 Old Montgomery Hwy #204",
                "Tuscaloosa, AL 35405",
            ],
        )


if __name__ == "__main__":
    unittest.main()
