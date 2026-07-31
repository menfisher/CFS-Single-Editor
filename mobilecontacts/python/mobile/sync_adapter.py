from __future__ import annotations

from urllib.error import HTTPError

from app.services.google_sync_service import (
    describe_google_http_error,
    export_mobile_pending_shared_contacts_to_google_drive,
    get_google_sync_summary,
    import_field_list_from_google_drive,
    import_google_contacts,
    sync_google_contact_changes,
    sync_mobile_contact_app_fields_for_contact,
    sync_mobile_contact_app_fields_from_google_drive,
    sync_mobile_editor_contact_changes,
    upload_google_contacts_only,
)


def _try_import_field_list() -> bool:
    try:
        return import_field_list_from_google_drive(export_if_missing=False)
    except Exception:
        return False


def _try_sync_contact_app_fields() -> int:
    try:
        return int(sync_mobile_contact_app_fields_from_google_drive() or 0)
    except Exception:
        return 0


def _try_export_pending_drive_contacts() -> int:
    try:
        return int(export_mobile_pending_shared_contacts_to_google_drive() or 0)
    except Exception:
        return 0


def mobile_editor_needs_sync() -> bool:
    """True when an Editor has local contact edits waiting for Sync."""
    state = (get_google_sync_summary().get("state") or {})
    if bool(state.get("upload_in_progress") or 0):
        return False
    pending_upload = int(state.get("pending_upload_count") or 0)
    pending_drive_contacts = int(state.get("pending_contact_drive_export_count") or 0)
    return pending_upload > 0 or pending_drive_contacts > 0


def import_field_list() -> bool:
    return _try_import_field_list()


def ensure_field_list_print_order() -> None:
    from app.database import fetch_one

    row = fetch_one("SELECT print_order_json FROM address_book_settings WHERE id = 1")
    if row and str(row["print_order_json"] or "").strip():
        return
    _try_import_field_list()


def _local_contact_count() -> int:
    from app.database import fetch_one

    row = fetch_one("SELECT COUNT(*) AS count FROM contacts")
    return int(row["count"] or 0) if row else 0


def _has_google_contacts_sync_token() -> bool:
    from app.database import fetch_one

    row = fetch_one("SELECT google_contacts_sync_token FROM google_sync_state WHERE id = 1")
    if not row:
        return False
    return bool(str(row["google_contacts_sync_token"] or "").strip())


def import_sign_in_contacts(*, include_app_fields: bool = False) -> int:
    _try_import_field_list()
    imported = import_google_contacts()
    if include_app_fields:
        _try_sync_contact_app_fields()
    return imported


def import_all_contacts() -> int:
    return import_sign_in_contacts(include_app_fields=True)


def sync_contact_changes(*, upload: bool = True) -> dict:
    if upload:
        return sync_mobile_editor_contact_changes()
    force_full_fetch = not _has_google_contacts_sync_token()
    download_result = sync_google_contact_changes(force_full_fetch=force_full_fetch)
    return {
        **download_result,
        "contact_upload_count": 0,
        "upload_processed_count": 0,
        "drive_field_updates": 0,
        "drive_exported": 0,
    }


def has_cached_contacts() -> bool:
    return _local_contact_count() > 0


def refresh_contacts_from_google(
    *,
    include_app_fields: bool = False,
    upload: bool = False,
    prefer_cached: bool = False,
) -> dict:
    """Download contacts from Google, using incremental sync when a local cache exists."""
    _try_import_field_list()
    download_kwargs = {"skip_drive_reconcile": True}
    try:
        contact_count = _local_contact_count()
        if contact_count <= 0:
            imported = import_sign_in_contacts(include_app_fields=False)
            return {
                "ok": True,
                "changed_count": imported,
                "created_count": imported,
                "updated_count": 0,
                "deleted_count": 0,
                "contact_upload_count": 0,
                "drive_field_updates": 0,
            }
        if upload:
            sync_result = sync_contact_changes(upload=True)
            return {"ok": True, **sync_result}
        if prefer_cached:
            force_full_fetch = not _has_google_contacts_sync_token()
            download_result = sync_google_contact_changes(
                force_full_fetch=force_full_fetch,
                **download_kwargs,
            )
            return {
                "ok": True,
                **download_result,
                "contact_upload_count": 0,
                "drive_field_updates": 0,
            }
        force_full_fetch = not _has_google_contacts_sync_token()
        download_result = sync_google_contact_changes(
            force_full_fetch=force_full_fetch,
            **download_kwargs,
        )
        return {
            "ok": True,
            **download_result,
            "contact_upload_count": 0,
            "drive_field_updates": 0,
        }
    except Exception as exc:
        if isinstance(exc, HTTPError):
            message = describe_google_http_error(exc)
        else:
            message = str(exc).strip() or "Could not download contacts from Google."
        return {
            "ok": False,
            "error": message,
            "changed_count": 0,
            "created_count": 0,
            "updated_count": 0,
            "deleted_count": 0,
            "contact_upload_count": 0,
        }


def refresh_contacts_from_google_for_read_only(
    *,
    force_full_fetch: bool = False,
    include_app_fields: bool = False,
) -> dict:
    """Download-only sync for read-only mobile roles (Pending Edits)."""
    del force_full_fetch
    return refresh_contacts_from_google(include_app_fields=include_app_fields, upload=False)


def refresh_contact_app_fields_from_google_drive(contact_id: int) -> bool:
    try:
        return bool(sync_mobile_contact_app_fields_for_contact(int(contact_id)))
    except Exception:
        return False


def mobile_sync_message(result: dict) -> str:
    upload_count = int(result.get("contact_upload_count") or 0)
    download_changed = int(result.get("changed_count") or 0)
    skipped_conflicts = int(result.get("skipped_conflict_count") or 0)
    parts: list[str] = []
    if upload_count > 0:
        noun = "contact" if upload_count == 1 else "contacts"
        parts.append(f"Uploaded {upload_count} {noun} to Google.")
    if download_changed > 0:
        noun = "change" if download_changed == 1 else "changes"
        parts.append(f"Merged {download_changed} {noun} from Google.")
    drive_field_updates = int(result.get("drive_field_updates") or 0)
    if drive_field_updates > 0:
        noun = "contact" if drive_field_updates == 1 else "contacts"
        parts.append(f"Updated {drive_field_updates} {noun} from Drive (photos and app fields).")
    drive_exported = int(result.get("drive_exported") or 0)
    if drive_exported > 0:
        noun = "contact" if drive_exported == 1 else "contacts"
        parts.append(f"Saved {drive_exported} {noun} to Google Drive.")
    if skipped_conflicts > 0:
        noun = "contact" if skipped_conflicts == 1 else "contacts"
        parts.append(f"Skipped {skipped_conflicts} {noun} with local edits pending.")
    if parts:
        return " ".join(parts)
    return "Already up to date with Google Contacts."


def upload_contacts_only() -> dict:
    return upload_google_contacts_only()
