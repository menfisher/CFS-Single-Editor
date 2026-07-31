from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import is_local_web_url
from app.services.google_sync_service import get_share_web_api_key
from app.services.share_web_app_service import share_web_app_url


def sync_share_app_contact_changes(owner_email: str, person_ids: list[str]) -> dict | None:
    """Ask the hosted share app to push updated contacts to connected recipients."""
    owner = str(owner_email or "").strip().lower()
    normalized_ids = list(
        dict.fromkeys(
            str(item or "").strip()
            for item in person_ids
            if str(item or "").strip()
        )
    )
    if not owner or not normalized_ids:
        return {"ok": False, "reason": "missing_owner_or_person_ids"}

    base_url = share_web_app_url().rstrip("/")
    api_key = get_share_web_api_key()
    if not base_url:
        return {"ok": False, "reason": "missing_share_web_app_url"}
    if not api_key:
        return None

    payload = json.dumps(
        {"owner_email": owner, "person_ids": normalized_ids},
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        f"{base_url}/api/cfs/sync-contact-changes",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-CFS-API-Key": api_key,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
            data = json.loads(body) if body else {}
            if isinstance(data, dict):
                return data
            return {"ok": True, "result": data}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code == 404:
            return {
                "ok": False,
                "reason": "share_app_endpoint_missing_redeploy_required",
                "detail": detail,
            }
        raise RuntimeError(f"Share app sync failed ({exc.code}): {detail}") from exc
    except URLError as exc:
        if is_local_web_url(base_url):
            return {"ok": False, "reason": "share_app_unreachable", "detail": str(exc.reason)}
        raise RuntimeError(f"Share app sync unreachable: {exc.reason}") from exc
