from __future__ import annotations

import re


_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?),\s*(?P<region>[A-Za-z]{2})\s+(?P<postal>\d{5}(?:-\d{4})?)\s*$"
)
_STATE_ZIP_ONLY_RE = re.compile(
    r"^(?P<region>[A-Za-z]{2})\s+(?P<postal>\d{5}(?:-\d{4})?)\s*$"
)
_TRAILING_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<street>.+?),\s*(?P<city_line>[^,]+,\s*[A-Za-z]{2}\s+\d{5}(?:-\d{4})?)\s*$"
)


def _clean(value: object) -> str:
    return str(value or "").strip()


def build_city_line(
    city: str = "",
    region: str = "",
    postal_code: str = "",
) -> str:
    city_text = _clean(city)
    region_text = _clean(region)
    postal = _clean(postal_code)
    if city_text and region_text and postal:
        return f"{city_text}, {region_text} {postal}"
    if city_text and region_text:
        return f"{city_text}, {region_text}"
    if city_text and postal:
        return f"{city_text} {postal}"
    return ", ".join(part for part in (city_text, region_text, postal) if part)


def build_street_block(
    street_address: str = "",
    extended_address: str = "",
) -> str:
    return ", ".join(
        part
        for part in (_clean(street_address), _clean(extended_address))
        if part
    )


def build_formatted_address(
    street_address: str = "",
    extended_address: str = "",
    city: str = "",
    region: str = "",
    postal_code: str = "",
) -> str:
    street_block = build_street_block(street_address, extended_address)
    city_line = build_city_line(city, region, postal_code)
    return ", ".join(part for part in (street_block, city_line) if part)


def parse_formatted_address(value: str) -> dict[str, str]:
    text = _clean(str(value or "").replace("\r", "\n"))
    empty = {
        "street_address": "",
        "extended_address": "",
        "city": "",
        "region": "",
        "postal_code": "",
    }
    if not text:
        return empty

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) == 1 and "," in lines[0]:
        parts = [part.strip() for part in lines[0].split(",") if part.strip()]
        if len(parts) >= 3:
            state_zip = _STATE_ZIP_ONLY_RE.match(parts[-1])
            if state_zip:
                street_parts = parts[:-2]
                return {
                    "street_address": street_parts[0],
                    "extended_address": ", ".join(street_parts[1:]) if len(street_parts) > 1 else "",
                    "city": parts[-2],
                    "region": state_zip.group("region").upper(),
                    "postal_code": state_zip.group("postal"),
                }
        if len(parts) == 2:
            state_zip = _CITY_STATE_ZIP_RE.match(f"{parts[0]}, {parts[1]}")
            if state_zip:
                return {
                    "street_address": "",
                    "extended_address": "",
                    "city": state_zip.group("city").strip(),
                    "region": state_zip.group("region").upper(),
                    "postal_code": state_zip.group("postal"),
                }
            trailing = _STATE_ZIP_ONLY_RE.match(parts[1])
            if trailing:
                return {
                    "street_address": parts[0],
                    "extended_address": "",
                    "city": "",
                    "region": trailing.group("region").upper(),
                    "postal_code": trailing.group("postal"),
                }

    if len(lines) >= 2:
        city_match = _CITY_STATE_ZIP_RE.match(lines[-1])
        if city_match:
            street_lines = lines[:-1]
            return {
                "street_address": street_lines[0] if street_lines else "",
                "extended_address": "\n".join(street_lines[1:]) if len(street_lines) > 1 else "",
                "city": city_match.group("city").strip(),
                "region": city_match.group("region").upper(),
                "postal_code": city_match.group("postal"),
            }

    if len(lines) == 1:
        city_match = _CITY_STATE_ZIP_RE.match(lines[0])
        if city_match:
            return {
                "street_address": "",
                "extended_address": "",
                "city": city_match.group("city").strip(),
                "region": city_match.group("region").upper(),
                "postal_code": city_match.group("postal"),
            }
        return {
            "street_address": lines[0],
            "extended_address": "",
            "city": "",
            "region": "",
            "postal_code": "",
        }

    return {
        "street_address": lines[0],
        "extended_address": "\n".join(lines[1:]),
        "city": "",
        "region": "",
        "postal_code": "",
    }


def enrich_address_parts(
    *,
    street_address: str = "",
    extended_address: str = "",
    city: str = "",
    region: str = "",
    postal_code: str = "",
    formatted_address: str = "",
) -> dict[str, str]:
    street = _clean(street_address)
    extended = _clean(extended_address)
    city_text = _clean(city)
    region_text = _clean(region)
    postal = _clean(postal_code)
    formatted = _clean(str(formatted_address or "").replace("\r", "\n"))

    has_structured = any((street, extended, city_text, region_text, postal))
    if not has_structured and formatted:
        parsed = parse_formatted_address(formatted)
        street = parsed["street_address"]
        extended = parsed["extended_address"]
        city_text = parsed["city"]
        region_text = parsed["region"]
        postal = parsed["postal_code"]
        has_structured = any((street, extended, city_text, region_text, postal))

    if has_structured:
        rebuilt = build_formatted_address(street, extended, city_text, region_text, postal)
        if rebuilt:
            formatted = rebuilt

    return {
        "street_address": street,
        "extended_address": extended,
        "city": city_text,
        "region": region_text,
        "postal_code": postal,
        "formatted_address": formatted,
    }


def split_address_street_and_city(value: str) -> tuple[str, str]:
    """Return (street_block, city_line) for display wrapping."""
    text = _clean(str(value or "").replace("\r", "\n"))
    if not text:
        return "", ""

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) >= 2 and _CITY_STATE_ZIP_RE.match(lines[-1]):
        return ", ".join(lines[:-1]), lines[-1]

    match = _TRAILING_CITY_STATE_ZIP_RE.match(" ".join(lines) if len(lines) > 1 else text)
    if match:
        return match.group("street").strip(), match.group("city_line").strip()

    enriched = enrich_address_parts(formatted_address=text)
    street_block = build_street_block(enriched["street_address"], enriched["extended_address"])
    city_line = build_city_line(enriched["city"], enriched["region"], enriched["postal_code"])
    if street_block or city_line:
        return street_block, city_line
    return text, ""


def address_display_lines(
    value: str = "",
    *,
    street_address: str = "",
    extended_address: str = "",
    city: str = "",
    region: str = "",
    postal_code: str = "",
    max_width: float | None = None,
    text_width=None,
) -> list[str]:
    """Print lines for address book / field list.

    - Street 1 and street 2 stay on separate lines when both exist.
    - City/state/ZIP joins the last street line when it fits; otherwise its own line.
    - Single-street addresses prefer one line; wrap to street / city when too wide.
    """
    def _fits(line: str) -> bool:
        if max_width is None or text_width is None:
            return True
        try:
            return float(text_width(line)) <= float(max_width)
        except Exception:
            return True

    enriched = enrich_address_parts(
        street_address=street_address,
        extended_address=extended_address,
        city=city,
        region=region,
        postal_code=postal_code,
        formatted_address=value,
    )
    street1 = _clean(enriched["street_address"])
    street2 = _clean(str(enriched["extended_address"] or "").replace("\r", "\n"))
    street2_parts = [part.strip() for part in street2.split("\n") if part.strip()]
    city_line = build_city_line(enriched["city"], enriched["region"], enriched["postal_code"])

    if street1 and street2_parts:
        prior = [street1, *street2_parts[:-1]]
        last_street = street2_parts[-1]
        if city_line:
            combined = f"{last_street}, {city_line}"
            if _fits(combined):
                return [*prior, combined]
            return [*prior, last_street, city_line]
        return [street1, *street2_parts]

    street_block = street1 or (street2_parts[0] if street2_parts else "")
    if len(street2_parts) > 1:
        prior = [street_block, *street2_parts[1:-1]]
        last_street = street2_parts[-1]
        if city_line:
            combined = f"{last_street}, {city_line}"
            if _fits(combined):
                return [part for part in [*prior, combined] if part]
            return [part for part in [*prior, last_street, city_line] if part]
        return [part for part in [street_block, *street2_parts[1:]] if part]

    one_line = enriched["formatted_address"] or ", ".join(
        part for part in (street_block, city_line) if part
    )
    if not one_line:
        return []
    if not street_block or not city_line:
        return [one_line]
    if _fits(one_line):
        return [one_line]
    return [street_block, city_line]
