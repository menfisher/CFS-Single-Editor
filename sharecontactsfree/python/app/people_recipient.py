"""People API helpers for recipient contact import."""

from __future__ import annotations

import base64
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Any

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import BatchHttpRequest

from .config import (
    RECIPIENT_IMPORT_PHOTO_FETCH_WORKERS,
    RECIPIENT_IMPORT_PHOTO_HTTP_BATCH_SIZE,
)
from .people_api import people_service

ALLOWED_CREATE_KEYS = (
    "addresses",
    "biographies",
    "birthdays",
    "emailAddresses",
    "memberships",
    "names",
    "nicknames",
    "organizations",
    "phoneNumbers",
    "relations",
    "urls",
    "userDefined",
)

_GOOGLE_ADDRESS_STRUCTURED_KEYS = (
    "streetAddress",
    "extendedAddress",
    "poBox",
    "city",
    "region",
    "postalCode",
    "country",
    "countryCode",
)


def _normalize_shared_formatted_address(value: str) -> str:
    text = str(value or "").replace("\r", "\n").strip()
    if not text or "\n" not in text:
        return text
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) == 2 and re.search(r",\s*[A-Z]{2}\s+\d{5}", lines[1]):
        return f"{lines[0]}, {lines[1]}"
    return "\n".join(lines)


def _prepare_shared_address_item(item: dict[str, Any]) -> dict[str, Any]:
    """Keep Google structured address fields so recipients get the same split as the owner."""
    next_item = {k: v for k, v in item.items() if k != "metadata"}
    next_item.pop("formattedType", None)

    structured: dict[str, str] = {}
    for key in _GOOGLE_ADDRESS_STRUCTURED_KEYS:
        value = str(next_item.get(key) or "").strip()
        if value:
            structured[key] = value
        next_item.pop(key, None)

    formatted = str(next_item.get("formattedValue") or "").strip()
    if not formatted and structured:
        parts = [
            structured.get("streetAddress", ""),
            structured.get("extendedAddress", ""),
            structured.get("city", ""),
            structured.get("region", ""),
            structured.get("postalCode", ""),
        ]
        formatted = ", ".join(part for part in parts if part)
    formatted = _normalize_shared_formatted_address(formatted)

    # Prefer owner structured fields; never strip them down to formatted-only.
    # Google often mis-parses formattedValue alone (e.g. "#119" → street "119 ...").
    next_item.update(structured)
    if formatted:
        next_item["formattedValue"] = formatted
    else:
        next_item.pop("formattedValue", None)
    return next_item


def build_shared_group_name(group_name: str) -> str:
    base = str(group_name or "").strip()
    if re.search(r"\(shared\)$", base, re.I):
        return base
    return f"{base} (shared)"


def group_names_equivalent(left: str, right: str) -> bool:
    def variants(name: str) -> set[str]:
        cleaned = str(name or "").strip()
        if not cleaned:
            return set()
        names = {cleaned.casefold()}
        base = re.sub(r"\s*\(shared\)\s*$", "", cleaned, flags=re.I).strip()
        if base:
            names.add(base.casefold())
            names.add(f"{base} (Shared)".casefold())
            names.add(f"{base} (shared)".casefold())
        return names

    return bool(variants(left) & variants(right))


def normalize_person_resource_name(resource_name: str) -> str:
    if not resource_name:
        return resource_name
    return resource_name if resource_name.startswith("people/") else f"people/{resource_name}"


def sanitize_contact_for_create(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    clean = {k: deepcopy(data[k]) for k in ALLOWED_CREATE_KEYS if data.get(k)}

    def strip_items(items: list[Any] | None) -> list[Any]:
        stripped: list[Any] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            next_item = {k: v for k, v in item.items() if k != "metadata"}
            stripped.append(next_item)
        return stripped

    for key in list(clean.keys()):
        if isinstance(clean.get(key), list):
            clean[key] = strip_items(clean[key])

    for item in clean.get("names") or []:
        item.pop("displayName", None)
        item.pop("displayNameLastFirst", None)
        item.pop("unstructuredName", None)
    for item in clean.get("phoneNumbers") or []:
        item.pop("canonicalForm", None)
    clean["addresses"] = [
        _prepare_shared_address_item(item)
        for item in clean.get("addresses") or []
        if isinstance(item, dict)
    ]

    birthdays = clean.get("birthdays") or []
    seen: set[str] = set()
    filtered = []
    for item in birthdays:
        date = (item or {}).get("date") or {}
        key = f"{date.get('year', '')}-{date.get('month', '')}-{date.get('day', '')}"
        if not date or key in seen:
            continue
        seen.add(key)
        filtered.append(item)
    if birthdays:
        clean["birthdays"] = filtered

    return clean


def list_shared_contact_groups(creds: Credentials) -> list[dict[str, Any]]:
    """Google contact groups created by share import (name ends with '(shared)')."""
    service = people_service(creds)
    groups: list[dict[str, Any]] = []
    page_token = None
    while True:
        kwargs: dict[str, Any] = {"pageSize": 200}
        if page_token:
            kwargs["pageToken"] = page_token
        data = service.contactGroups().list(**kwargs).execute()
        for group in data.get("contactGroups") or []:
            name = str(group.get("name") or "").strip()
            if not name or not re.search(r"\(shared\)$", name, re.I):
                continue
            resource_name = str(group.get("resourceName") or "").strip()
            if not resource_name:
                continue
            member_count = int(group.get("memberCount") or 0)
            base_name = re.sub(r"\s*\(shared\)$", "", name, flags=re.I).strip() or name
            groups.append(
                {
                    "googleGroupId": resource_name,
                    "displayName": name,
                    "baseName": base_name,
                    "memberCount": member_count,
                }
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return groups


def find_group_by_name(creds: Credentials, group_name: str) -> str | None:
    service = people_service(creds)
    page_token = None
    while True:
        kwargs: dict[str, Any] = {"pageSize": 200}
        if page_token:
            kwargs["pageToken"] = page_token
        data = service.contactGroups().list(**kwargs).execute()
        for g in data.get("contactGroups") or []:
            if g.get("name") == group_name:
                return g.get("resourceName")
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return None


def create_contact_group(creds: Credentials, group_name: str) -> str | None:
    service = people_service(creds)
    name = build_shared_group_name(group_name)
    body = {"contactGroup": {"name": name}}
    try:
        data = service.contactGroups().create(body=body).execute()
        return data.get("resourceName")
    except HttpError as exc:
        if exc.resp.status == 409 or "ALREADY_EXISTS" in str(exc):
            return find_group_by_name(creds, name)
        raise


def update_contact_group_name(
    creds: Credentials, resource_name: str, group_name: str
) -> str | None:
    group_id = str(resource_name or "").strip()
    next_name = build_shared_group_name(group_name)
    if not group_id or not next_name:
        return None
    details = get_group_details(creds, group_id, 1)
    current_name = str(details.get("formattedName") or details.get("name") or "").strip()
    if group_names_equivalent(current_name, next_name) and current_name == next_name:
        return group_id
    body = {
        "contactGroup": {"name": next_name},
        "updateGroupFields": "name",
    }
    etag = str(details.get("etag") or "").strip()
    if etag:
        body["contactGroup"]["etag"] = etag
    service = people_service(creds)
    data = service.contactGroups().update(
        resourceName=group_id,
        body=body,
    ).execute()
    return str(data.get("resourceName") or group_id)


def get_group_details(creds: Credentials, resource_name: str, max_members: int = 2000) -> dict[str, Any]:
    service = people_service(creds)
    return (
        service.contactGroups()
        .get(resourceName=resource_name, maxMembers=max_members)
        .execute()
    )


def group_exists(creds: Credentials, resource_name: str) -> bool:
    if not resource_name:
        return False
    try:
        get_group_details(creds, resource_name, 1)
        return True
    except HttpError:
        return False


def group_member_count(creds: Credentials, resource_name: str) -> int | None:
    if not resource_name:
        return None
    try:
        details = get_group_details(creds, resource_name, 2000)
        return len(details.get("memberResourceNames") or [])
    except HttpError:
        return None


def prepare_contact_person(
    member_data: dict[str, Any], group_id: str
) -> dict[str, Any]:
    payload = sanitize_contact_for_create(member_data)
    payload["memberships"] = [
        {"contactGroupMembership": {"contactGroupResourceName": group_id}}
    ]
    return payload


def create_contact_in_group(
    creds: Credentials,
    member_data: dict[str, Any],
    group_id: str,
    *,
    skip_photo: bool = True,
) -> str | None:
    if not member_data:
        return None
    service = people_service(creds)
    payload = prepare_contact_person(member_data, group_id)
    data = service.people().createContact(body=payload).execute()
    resource_name = data.get("resourceName")
    if resource_name and not skip_photo:
        set_contact_photo(creds, resource_name, member_data)
    return resource_name


BATCH_CREATE_READ_MASK = (
    "names,emailAddresses,phoneNumbers,memberships,photos"
)


def batch_create_contacts_in_group(
    creds: Credentials,
    indexed_members: list[tuple[int, dict[str, Any]]],
    group_id: str,
) -> dict[int, str | None]:
    """Create up to 200 contacts in one People API call.

    Returns a map of member index -> created resource name (or None on failure).
    """
    if not indexed_members:
        return {}

    if len(indexed_members) == 1:
        idx, member = indexed_members[0]
        try:
            return {idx: create_contact_in_group(creds, member, group_id, skip_photo=True)}
        except Exception:
            return {idx: None}

    service = people_service(creds)
    contacts = [
        {"contactPerson": prepare_contact_person(member, group_id)}
        for _, member in indexed_members
    ]
    try:
        data = (
            service.people()
            .batchCreateContacts(
                body={"contacts": contacts, "readMask": BATCH_CREATE_READ_MASK}
            )
            .execute()
        )
        created = data.get("createdPeople") or []
        result: dict[int, str | None] = {}
        for i, (idx, _) in enumerate(indexed_members):
            if i < len(created):
                person = (created[i] or {}).get("person") or {}
                result[idx] = person.get("resourceName")
            else:
                result[idx] = None
        return result
    except HttpError:
        if len(indexed_members) > 10:
            mid = len(indexed_members) // 2
            left = batch_create_contacts_in_group(
                creds, indexed_members[:mid], group_id
            )
            right = batch_create_contacts_in_group(
                creds, indexed_members[mid:], group_id
            )
            return {**left, **right}

        result = {}
        for idx, member in indexed_members:
            try:
                result[idx] = create_contact_in_group(
                    creds, member, group_id, skip_photo=True
                )
            except Exception:
                result[idx] = None
        return result


def _photo_url_from_entry(photo: Any) -> str:
    if not isinstance(photo, dict):
        return ""
    return str(photo.get("url") or "").strip()


def contact_photo_url(member_data: dict[str, Any] | None) -> str:
    """Prefer the owner's primary custom photo over Google's default avatar."""
    if not member_data:
        return ""
    photos = [photo for photo in (member_data.get("photos") or []) if isinstance(photo, dict)]
    if not photos:
        return ""

    def _is_default(photo: dict[str, Any]) -> bool:
        metadata = photo.get("metadata") if isinstance(photo.get("metadata"), dict) else {}
        return bool(metadata.get("default"))

    def _is_primary(photo: dict[str, Any]) -> bool:
        metadata = photo.get("metadata") if isinstance(photo.get("metadata"), dict) else {}
        return bool(metadata.get("primary"))

    for photo in photos:
        url = _photo_url_from_entry(photo)
        if url and _is_primary(photo) and not _is_default(photo):
            return url
    for photo in photos:
        url = _photo_url_from_entry(photo)
        if url and not _is_default(photo):
            return url
    for photo in photos:
        url = _photo_url_from_entry(photo)
        if url:
            return url
    return ""


def has_contact_photo(member_data: dict[str, Any] | None) -> bool:
    return bool(contact_photo_url(member_data))


def build_photo_url_candidates(url: str) -> list[str]:
    """Build Google People photo URL variants (ported from gsWebApp buildPhotoUrlCandidates_)."""
    raw = str(url or "").strip()
    if not raw:
        return []

    out = [raw]
    if "?" in raw:
        if re.search(r"([?&])sz=\d+", raw, re.I):
            out.append(re.sub(r"([?&])sz=\d+", r"\1sz=0", raw, flags=re.I))
            out.append(re.sub(r"([?&])sz=\d+", r"\1sz=2048", raw, flags=re.I))
        else:
            out.append(raw + "&sz=0")
            out.append(raw + "&sz=2048")
    else:
        out.append(raw + "?sz=0")
        out.append(raw + "?sz=2048")

    if re.search(r"=s\d+", raw, re.I):
        out.append(re.sub(r"=s\d+(-[a-z0-9]+)?", "=s0", raw, flags=re.I))
        out.append(re.sub(r"=s\d+(-[a-z0-9]+)?", "=s2048", raw, flags=re.I))
    else:
        out.append(raw + "=s0")
        out.append(raw + "=s2048")

    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in out:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)

    # Prefer full-resolution variants before the default thumbnail URL.
    def rank(candidate: str) -> tuple[int, int]:
        hi = 0
        if "2048" in candidate or "=s2048" in candidate:
            hi = 0
        elif "sz=0" in candidate or "=s0" in candidate:
            hi = 1
        elif re.search(r"sz=\d+", candidate, re.I) or re.search(r"=s\d+", candidate, re.I):
            hi = 3
        else:
            hi = 2
        return (hi, len(candidate))

    return sorted(ordered, key=rank)


def _fetch_url_bytes(url: str, token: str) -> bytes | None:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            if 200 <= resp.status_code < 300 and resp.content:
                return resp.content
    except httpx.HTTPError:
        pass
    return None


def fetch_photo_bytes(url: str, token: str) -> str | None:
    """Fetch photo bytes at the highest resolution available.

    Google stores photos[].url as a small (=s100) thumbnail. Request the
    full-resolution variants first (build_photo_url_candidates ranks them
    full-res first) and only fall back to the raw thumbnail URL if those fail,
    so imported contacts keep the original photo quality.
    """
    if not url or not token:
        return None

    best: bytes | None = None
    for candidate in build_photo_url_candidates(url):
        content = _fetch_url_bytes(candidate, token)
        if not content:
            continue
        if best is None or len(content) > len(best):
            best = content
        # A genuine full-resolution image is far larger than a thumbnail; stop
        # once we have one rather than fetching every remaining variant.
        if len(content) >= 200_000:
            break
    if not best:
        return None
    return base64.b64encode(best).decode("ascii")


def fetch_photo_bytes_for_member(
    member_data: dict[str, Any] | None,
    *,
    owner_token: str | None = None,
    recipient_token: str | None = None,
) -> str | None:
    """Download shared contact photo bytes (owner URLs need the owner OAuth token)."""
    url = contact_photo_url(member_data)
    if not url:
        return None
    for token in (owner_token, recipient_token):
        if not token:
            continue
        payload = fetch_photo_bytes(url, token)
        if payload:
            return payload
    return None


def prefetch_photo_bytes(
    indexed_members: list[tuple[int, dict[str, Any]]],
    token: str,
    *,
    owner_token: str | None = None,
    max_workers: int | None = None,
) -> dict[int, str | None]:
    """Download photo bytes in parallel for a slice of contacts."""
    if not indexed_members:
        return {}

    workers = max(1, min(max_workers or RECIPIENT_IMPORT_PHOTO_FETCH_WORKERS, len(indexed_members)))
    if len(indexed_members) == 1:
        idx, member = indexed_members[0]
        return {
            idx: fetch_photo_bytes_for_member(
                member,
                owner_token=owner_token,
                recipient_token=token,
            )
        }

    result: dict[int, str | None] = {}

    def _one(item: tuple[int, dict[str, Any]]) -> tuple[int, str | None]:
        idx, member = item
        return idx, fetch_photo_bytes_for_member(
            member,
            owner_token=owner_token,
            recipient_token=token,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, item): item[0] for item in indexed_members}
        for future in as_completed(futures):
            idx, payload = future.result()
            result[idx] = payload
    return result


def recipient_person_exists(creds: Credentials, resource_name: str) -> bool:
    normalized = normalize_person_resource_name(resource_name)
    if not normalized:
        return False
    service = people_service(creds)
    try:
        service.people().get(resourceName=normalized, personFields="metadata").execute()
        return True
    except HttpError as exc:
        if exc.resp is not None and exc.resp.status == 404:
            return False
        return True


def upload_contact_photo_bytes(
    creds: Credentials, resource_name: str, photo_bytes_b64: str
) -> bool:
    if not photo_bytes_b64:
        return False
    if not recipient_person_exists(creds, resource_name):
        return False
    service = people_service(creds)
    normalized = normalize_person_resource_name(resource_name)
    body = {"photoBytes": photo_bytes_b64}
    try:
        service.people().updateContactPhoto(resourceName=normalized, body=body).execute()
        return True
    except Exception:
        # Treat timeouts / 404 / quota blips as soft failures so bulk refresh can continue.
        return False


def batch_upload_contact_photos(
    creds: Credentials,
    uploads: list[tuple[int, str, str]],
) -> dict[int, bool]:
    """Upload multiple contact photos via HTTP batch (one round trip per chunk)."""
    if not uploads:
        return {}

    if len(uploads) == 1:
        idx, resource_name, photo_bytes = uploads[0]
        return {idx: upload_contact_photo_bytes(creds, resource_name, photo_bytes)}

    service = people_service(creds)
    chunk_size = max(1, min(RECIPIENT_IMPORT_PHOTO_HTTP_BATCH_SIZE, 200))
    outcomes: dict[int, bool] = {}

    for offset in range(0, len(uploads), chunk_size):
        chunk = uploads[offset : offset + chunk_size]
        pending: dict[str, tuple[int, str, str]] = {}

        def _callback(request_id: str, _response: Any, exception: Exception | None) -> None:
            idx, resource_name, photo_bytes = pending[request_id]
            if exception is None:
                outcomes[idx] = True
                return
            outcomes[idx] = upload_contact_photo_bytes(creds, resource_name, photo_bytes)

        batch = BatchHttpRequest(callback=_callback)
        for idx, resource_name, photo_bytes in chunk:
            request_id = str(idx)
            pending[request_id] = (idx, resource_name, photo_bytes)
            normalized = normalize_person_resource_name(resource_name)
            batch.add(
                service.people().updateContactPhoto(
                    resourceName=normalized,
                    body={"photoBytes": photo_bytes},
                ),
                request_id=request_id,
            )
        try:
            batch.execute()
        except Exception:
            for idx, resource_name, photo_bytes in chunk:
                if idx not in outcomes:
                    outcomes[idx] = upload_contact_photo_bytes(
                        creds, resource_name, photo_bytes
                    )

        for idx, resource_name, photo_bytes in chunk:
            if idx not in outcomes:
                outcomes[idx] = upload_contact_photo_bytes(
                    creds, resource_name, photo_bytes
                )

    return outcomes


def set_contact_photo(
    creds: Credentials,
    resource_name: str,
    member_data: dict[str, Any],
    *,
    photo_bytes_b64: str | None = None,
    owner_token: str | None = None,
) -> bool:
    payload = photo_bytes_b64
    if not payload:
        payload = fetch_photo_bytes_for_member(
            member_data,
            owner_token=owner_token,
            recipient_token=creds.token or "",
        )
    if not payload:
        return False
    return upload_contact_photo_bytes(creds, resource_name, payload)


def get_person_etag(creds: Credentials, resource_name: str) -> str | None:
    normalized = normalize_person_resource_name(resource_name)
    if not normalized:
        return None
    service = people_service(creds)
    try:
        data = (
            service.people()
            .get(resourceName=normalized, personFields="metadata")
            .execute()
        )
        return str(data.get("etag") or "") or None
    except HttpError:
        return None


def get_person_etags_batch(creds: Credentials, resource_names: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in resource_names:
        normalized = normalize_person_resource_name(name)
        if not normalized:
            continue
        etag = get_person_etag(creds, normalized)
        if etag:
            out[normalized] = etag
    return out


def update_contact(
    creds: Credentials,
    recipient_person_id: str,
    member_data: dict[str, Any],
    etag: str,
) -> bool:
    from .services.contact_hash import SYNC_WRITABLE_FIELDS, build_update_payload

    normalized = normalize_person_resource_name(recipient_person_id)
    if not normalized or not etag:
        return False
    service = people_service(creds)
    body = build_update_payload(member_data)
    body["resourceName"] = normalized
    body["etag"] = etag
    try:
        service.people().updateContact(
            resourceName=normalized,
            updatePersonFields=",".join(SYNC_WRITABLE_FIELDS),
            body=body,
        ).execute()
        return True
    except HttpError:
        return False


def delete_contact(creds: Credentials, recipient_person_id: str) -> bool:
    normalized = normalize_person_resource_name(recipient_person_id)
    if not normalized:
        return False
    service = people_service(creds)
    try:
        service.people().deleteContact(resourceName=normalized).execute()
        return True
    except HttpError:
        return False


def delete_contact_photo(creds: Credentials, recipient_person_id: str) -> bool:
    normalized = normalize_person_resource_name(recipient_person_id)
    if not normalized:
        return False
    service = people_service(creds)
    try:
        service.people().deleteContactPhoto(resourceName=normalized).execute()
        return True
    except HttpError:
        return False


def ensure_contact_in_group(
    creds: Credentials,
    recipient_person_id: str,
    group_id: str,
) -> bool:
    normalized = normalize_person_resource_name(recipient_person_id)
    if not normalized or not group_id:
        return False
    service = people_service(creds)
    try:
        service.contactGroups().members().modify(
            resourceName=group_id,
            body={"resourceNamesToAdd": [normalized]},
        ).execute()
        return True
    except HttpError:
        return False

