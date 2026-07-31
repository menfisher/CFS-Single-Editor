"""Port of createSharedGroups from gsWebApp.gs — import shared contacts into recipient Google account."""

from __future__ import annotations

import threading
import time
from copy import deepcopy
from typing import Any, Callable

from google.oauth2.credentials import Credentials

from .. import db
from ..google_auth import clear_recipient_connection, is_oauth_refresh_error, owner_credentials
from ..config import (
    RECIPIENT_IMPORT_BATCH_SIZE,
    RECIPIENT_IMPORT_CREATE_BATCH_SIZE,
    RECIPIENT_IMPORT_INCLUDE_PHOTOS,
    RECIPIENT_IMPORT_LARGE_GROUP_THRESHOLD,
    RECIPIENT_IMPORT_MAX_OPS_PER_LOAD,
    RECIPIENT_IMPORT_MAX_RETRIES,
    RECIPIENT_IMPORT_PHOTO_BATCH_SIZE,
    RECIPIENT_IMPORT_PROGRESS_BATCH_SIZE,
    RECIPIENT_IMPORT_TIME_BUDGET_MS,
)
from ..people_recipient import (
    batch_create_contacts_in_group,
    batch_upload_contact_photos,
    build_shared_group_name,
    create_contact_group,
    create_contact_in_group,
    find_group_by_name,
    get_group_details,
    group_exists,
    group_member_count,
    has_contact_photo,
    prefetch_photo_bytes,
)

_user_locks: dict[str, threading.Lock] = {}
_lock_guard = threading.Lock()


def _lock_for(email: str) -> threading.Lock:
    with _lock_guard:
        if email not in _user_locks:
            _user_locks[email] = threading.Lock()
        return _user_locks[email]


def _contact_group_resource(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("contactGroups/"):
        return text
    return ""


def _clear_recipient_import_state(group: dict[str, Any]) -> None:
    group["created"] = ""
    group["importCursor"] = 0
    group["actualCount"] = 0
    group["skippedCount"] = 0
    group["photoDoneCount"] = 0
    for key in (
        "retryIndexes",
        "retryAttempts",
        "skippedIndexes",
        "photoPendingIndexes",
        "photoRetryIndexes",
        "photoRetryAttempts",
        "photoTargets",
        "photoTargetCount",
        "importPhase",
    ):
        group.pop(key, None)


def _ensure_recipient_group(
    creds: Credentials, group: dict[str, Any], name: str
) -> str | None:
    group_id = _contact_group_resource(group.get("created"))
    if group_id and not group_exists(creds, group_id):
        _clear_recipient_import_state(group)
        group_id = ""

    if not group_id:
        shared_name = build_shared_group_name(name)
        group_id = find_group_by_name(creds, shared_name)
        if not group_id:
            group_id = create_contact_group(creds, name)
        if not group_id:
            return None
        group["created"] = group_id
        if int(group.get("importCursor") or 0) > 0 and not group_exists(creds, group_id):
            group["importCursor"] = 0
    return group_id


def _sync_mapping_count(group: dict[str, Any]) -> int:
    share_id = str(group.get("shareId") or "")
    owner = str(group.get("owner") or "")
    resource_name = str(group.get("resourceName") or "")
    recipient_email = str(group.get("recipientEmail") or "")
    if not share_id or not owner or not recipient_email:
        return 0
    by_owner, _ = db.load_sync_mappings(
        share_id, owner, resource_name, recipient_email
    )
    return len(by_owner)


def _contacts_import_complete(
    group: dict[str, Any],
    members_len: int,
    start: int,
    retry_indexes: list[int],
) -> bool:
    if retry_indexes:
        return False
    if members_len <= 0:
        return True
    if start >= members_len:
        return True
    return _sync_mapping_count(group) >= members_len


def _reconcile_import_progress(
    creds: Credentials,
    group: dict[str, Any],
    group_id: str,
    members_data_len: int,
) -> int:
    actual = group_member_count(creds, group_id)
    if actual is None:
        _clear_recipient_import_state(group)
        return 0

    group["actualCount"] = actual
    start = max(0, int(group.get("importCursor") or 0))
    mapped = _sync_mapping_count(group)

    # Trust sync mappings when Google group membership count lags (common on small groups).
    if mapped >= members_data_len and members_data_len > 0:
        group["importCursor"] = members_data_len
        return members_data_len

    if actual < members_data_len:
        if start >= members_data_len or start > actual:
            start = actual
            group["importCursor"] = start
    elif actual == 0 and start >= members_data_len and members_data_len > 0:
        if mapped < members_data_len:
            group["importCursor"] = 0
            start = 0

    return start


def _dedupe_shared_groups(shared_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    index_by_key: dict[str, int] = {}
    for group in shared_groups:
        if not group:
            continue
        key = f"{group.get('owner') or ''}|{group.get('resourceName') or ''}"
        if key not in index_by_key:
            index_by_key[key] = len(deduped)
            deduped.append(deepcopy(group))
            continue
        idx = index_by_key[key]
        current = deduped[idx]
        current.update(group)
        current["created"] = _contact_group_resource(
            group.get("created") or current.get("created")
        )
        current["importCursor"] = group.get("importCursor") or current.get("importCursor") or 0
    return deduped


def _filter_result(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for group in groups:
        if not group or not group.get("shareId"):
            continue
        if group.get("staleRemove"):
            continue
        if group.get("status") == "Missing data" and not group.get("created"):
            continue
        out.append(group)
    return out


def _queue_unmapped_member_retries(
    group: dict[str, Any],
    members_data: list[dict[str, Any]],
    retry_indexes: list[int],
) -> int:
    """Queue already-passed contacts that never received a recipient sync mapping.

    Only indexes below importCursor are queued (genuine gaps the sequential importer
    already moved past). Indexes at/above the cursor are left for the fast
    batchCreateContacts path — queuing them here would force slow one-at-a-time creates,
    which is what made reset/relink re-imports crawl.
    """
    share_id = str(group.get("shareId") or "")
    owner = str(group.get("owner") or "")
    resource_name = str(group.get("resourceName") or "")
    recipient_email = str(group.get("recipientEmail") or "")
    if not share_id or not owner or not recipient_email:
        return 0

    cursor = int(group.get("importCursor") or 0)
    if cursor <= 0:
        return 0

    by_owner, _ = db.load_sync_mappings(
        share_id, owner, resource_name, recipient_email
    )
    added = 0
    for idx, member in enumerate(members_data):
        if idx >= cursor:
            break
        owner_person_id = str(member.get("resourceName") or "")
        if not owner_person_id.startswith("people/"):
            continue
        if owner_person_id in by_owner:
            continue
        if idx not in retry_indexes:
            retry_indexes.append(idx)
            added += 1
    if added:
        group["retryIndexes"] = list(dict.fromkeys(retry_indexes))
    return added


def _hydrate_photo_targets(
    group: dict[str, Any],
    members_data: list[dict[str, Any]],
    photo_targets: dict[str, str],
) -> dict[str, str]:
    """Rebuild index→recipient person map from sync mappings (not stored in app_data)."""
    share_id = str(group.get("shareId") or "")
    owner = str(group.get("owner") or "")
    resource_name = str(group.get("resourceName") or "")
    recipient_email = str(group.get("recipientEmail") or "")
    if not share_id or not owner or not recipient_email:
        return dict(photo_targets)

    by_owner, _ = db.load_sync_mappings(
        share_id, owner, resource_name, recipient_email
    )
    hydrated: dict[str, str] = {}
    for idx, member in enumerate(members_data):
        owner_person_id = str(member.get("resourceName") or "")
        mapping = by_owner.get(owner_person_id) or {}
        recipient_person_id = str(mapping.get("recipient_person_id") or "").strip()
        if recipient_person_id.startswith("people/"):
            hydrated[str(idx)] = recipient_person_id
    return hydrated


def _dedupe_int_list(values: list[int]) -> list[int]:
    return list(dict.fromkeys(values))


def _normalize_photo_queues(
    photo_pending: list[int],
    photo_retry: list[int],
    photo_targets: dict[str, str],
    members_data: list[dict[str, Any]],
) -> None:
    """Drop duplicate/stale indices so done count and pending queue stay aligned."""
    valid: set[int] = set()
    for idx_str in photo_targets:
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(members_data) and has_contact_photo(members_data[idx]):
            valid.add(idx)
    photo_pending[:] = _dedupe_int_list([i for i in photo_pending if i in valid])
    photo_retry[:] = _dedupe_int_list([i for i in photo_retry if i in valid])


def _photos_import_complete(
    photo_done: int,
    photo_target_total: int,
    photo_pending: list[int],
    photo_retry: list[int],
) -> bool:
    """True when all target photos are uploaded (clears stale queue leftovers)."""
    if photo_target_total <= 0:
        return True
    if photo_done >= photo_target_total:
        photo_pending.clear()
        photo_retry.clear()
        return True
    return not photo_pending and not photo_retry and photo_done >= photo_target_total


def _ensure_photo_work_queue(
    members_data: list[dict[str, Any]],
    photo_targets: dict[str, str],
    photo_pending: list[int],
    photo_retry: list[int],
    photo_done: int,
    photo_target_total: int,
) -> None:
    """Re-seed photo pending when queue was lost but uploads remain."""
    _normalize_photo_queues(
        photo_pending, photo_retry, photo_targets, members_data
    )
    if photo_pending or photo_retry:
        return
    if photo_target_total <= 0 or photo_done >= photo_target_total:
        return
    blocked = set(photo_retry)
    for idx_str, _rid in photo_targets.items():
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            continue
        if idx in blocked:
            continue
        member = members_data[idx] if 0 <= idx < len(members_data) else None
        if member and has_contact_photo(member) and idx not in photo_pending:
            photo_pending.append(idx)


def _save_sync_mapping_after_create(
    group: dict[str, Any],
    member_data: dict[str, Any] | None,
    recipient_person_id: str | None,
) -> None:
    if not member_data or not recipient_person_id:
        return
    owner_person_id = str(member_data.get("resourceName") or "")
    if not owner_person_id.startswith("people/"):
        return
    share_id = str(group.get("shareId") or "")
    owner = str(group.get("owner") or "")
    resource_name = str(group.get("resourceName") or "")
    recipient_email = str(group.get("recipientEmail") or "")
    if not share_id or not owner or not resource_name or not recipient_email:
        return
    from .contact_hash import compute_contact_hash, compute_photo_hash

    db.upsert_sync_mapping(
        share_id=share_id,
        owner=owner,
        group_resource_name=resource_name,
        recipient_email=recipient_email,
        owner_person_id=owner_person_id,
        recipient_person_id=str(recipient_person_id),
        last_hash=compute_contact_hash(member_data),
        last_photo_hash=compute_photo_hash(member_data),
        owner_etag=str(member_data.get("etag") or ""),
    )


def _process_sync_mappings_before_batch(
    creds: Credentials,
    group: dict[str, Any],
    group_id: str,
    members_data: list[dict[str, Any]],
    start: int,
    ops_budget: int,
    *,
    photo_pending: list[int],
    photo_targets: dict[str, str],
    over_budget: Callable[[], bool],
) -> tuple[int, int]:
    """Skip or update mapped contacts before batch create."""
    share_id = str(group.get("shareId") or "")
    owner = str(group.get("owner") or "")
    resource_name = str(group.get("resourceName") or "")
    recipient_email = str(group.get("recipientEmail") or "")
    if not share_id or not owner or not recipient_email:
        return start, 0

    by_owner_id, _ = db.load_sync_mappings(
        share_id, owner, resource_name, recipient_email
    )
    if not by_owner_id:
        return start, 0

    from .contact_hash import compute_contact_hash, compute_photo_hash
    from ..people_recipient import (
        delete_contact_photo,
        ensure_contact_in_group,
        get_person_etags_batch,
        normalize_person_resource_name,
        set_contact_photo,
        update_contact,
    )

    owner_creds = owner_credentials(owner)
    owner_token = (owner_creds.token or "") if owner_creds else None

    ops_used = 0
    cursor = start
    while cursor < len(members_data) and ops_used < ops_budget:
        if over_budget():
            break
        member = members_data[cursor]
        owner_person_id = str(member.get("resourceName") or "")
        mapping = by_owner_id.get(owner_person_id)
        recipient_person_id = str((mapping or {}).get("recipient_person_id") or "")
        if not mapping or not recipient_person_id:
            break

        contact_hash = compute_contact_hash(member)
        photo_hash = compute_photo_hash(member)
        owner_etag = str(member.get("etag") or "")
        prev_hash = str(mapping.get("last_hash") or "")
        prev_photo = str(mapping.get("last_photo_hash") or "")
        prev_etag = str(mapping.get("owner_etag") or "")

        rid = normalize_person_resource_name(recipient_person_id)
        if not rid:
            break

        if contact_hash == prev_hash and photo_hash == prev_photo and owner_etag == prev_etag:
            ensure_contact_in_group(creds, rid, group_id)
            if photo_hash != "no-photo" and str(cursor) not in photo_targets:
                photo_pending.append(cursor)
                photo_targets[str(cursor)] = rid
            cursor += 1
            ops_used += 1
            continue

        etags = get_person_etags_batch(creds, [rid])
        etag = etags.get(rid)
        if not etag:
            break

        if contact_hash != prev_hash or owner_etag != prev_etag:
            if not update_contact(creds, rid, member, etag):
                break
        if photo_hash != prev_photo:
            if photo_hash == "no-photo":
                delete_contact_photo(creds, rid)
            else:
                set_contact_photo(creds, rid, member, owner_token=owner_token)

        db.upsert_sync_mapping(
            share_id=share_id,
            owner=owner,
            group_resource_name=resource_name,
            recipient_email=recipient_email,
            owner_person_id=owner_person_id,
            recipient_person_id=rid,
            last_hash=contact_hash,
            last_photo_hash=photo_hash,
            owner_etag=owner_etag,
        )
        ensure_contact_in_group(creds, rid, group_id)
        if photo_hash != "no-photo" and str(cursor) not in photo_targets:
            photo_pending.append(cursor)
            photo_targets[str(cursor)] = rid

        cursor += 1
        ops_used += 1

    return cursor, ops_used


def _record_create_result(
    index: int,
    resource_id: str | None,
    member_data: dict[str, Any] | None,
    *,
    retry_indexes: list[int],
    retry_attempts: dict[str, int],
    skipped_indexes: list[int],
    photo_pending: list[int],
    photo_targets: dict[str, str],
) -> bool:
    """Apply one contact create outcome. Returns True if the create failed."""
    if resource_id:
        retry_attempts.pop(str(index), None)
        skipped_indexes[:] = [x for x in skipped_indexes if x != index]
        if (
            RECIPIENT_IMPORT_INCLUDE_PHOTOS
            and member_data
            and has_contact_photo(member_data)
        ):
            photo_targets[str(index)] = resource_id
            if index not in photo_pending:
                photo_pending.append(index)
        return False

    key = str(index)
    nxt = retry_attempts.get(key, 0) + 1
    if nxt >= RECIPIENT_IMPORT_MAX_RETRIES:
        retry_attempts.pop(key, None)
        if index not in skipped_indexes:
            skipped_indexes.append(index)
    else:
        retry_attempts[key] = nxt
        retry_indexes.append(index)
    return True


def _notify_contact_import_progress(
    group_snapshot: dict[str, Any],
    *,
    cursor: int,
    members_total: int,
    retry_indexes: list[int],
    skipped_indexes: list[int],
    photo_pending: list[int],
    photo_targets: dict[str, str],
    on_group_updated: Callable[[dict[str, Any]], None] | None,
    failed: int = 0,
) -> None:
    if not on_group_updated:
        return
    group_snapshot["importCursor"] = cursor
    group_snapshot["retryIndexes"] = list(dict.fromkeys(retry_indexes))
    group_snapshot["skippedIndexes"] = list(dict.fromkeys(skipped_indexes))
    group_snapshot["skippedCount"] = len(skipped_indexes)
    group_snapshot["photoPendingIndexes"] = list(dict.fromkeys(photo_pending))
    group_snapshot["photoTargets"] = dict(photo_targets)
    photo_done = int(group_snapshot.get("photoDoneCount") or 0)
    photo_retry_list = list(group_snapshot.get("photoRetryIndexes") or [])
    photo_remaining = len(photo_pending) + len(photo_retry_list)
    photo_target_total = _photo_target_count(group_snapshot, photo_targets)
    group_snapshot["photoTargetCount"] = photo_target_total
    group_snapshot["photoDoneCount"] = photo_done
    retry_label = f", retry {len(retry_indexes)}" if retry_indexes else ""
    fail_label = f", {failed} failed this pass" if failed else ""
    if cursor >= members_total and photo_remaining > 0:
        photo_total = photo_target_total or (photo_done + photo_remaining)
        group_snapshot["importPhase"] = "photos"
        group_snapshot["status"] = f"Photos ({photo_done}/{photo_total})"
    else:
        group_snapshot["importPhase"] = "contacts"
        group_snapshot["status"] = (
            f"Importing ({cursor}/{members_total}{retry_label}{fail_label})"
        )
    on_group_updated(deepcopy(group_snapshot))


def _contact_import_chunk_size(members_total: int, ops_budget: int) -> int:
    """Small groups use progress-sized chunks; large groups use full API batch size."""
    api_cap = min(
        RECIPIENT_IMPORT_CREATE_BATCH_SIZE,
        RECIPIENT_IMPORT_BATCH_SIZE,
        200,
    )
    if members_total <= RECIPIENT_IMPORT_LARGE_GROUP_THRESHOLD:
        api_cap = min(api_cap, RECIPIENT_IMPORT_PROGRESS_BATCH_SIZE)
    return max(1, min(api_cap, ops_budget))


def _import_contacts_batch(
    creds: Credentials,
    members_data: list[dict[str, Any]],
    group_id: str,
    start: int,
    ops_budget: int,
    *,
    retry_indexes: list[int],
    retry_attempts: dict[str, int],
    skipped_indexes: list[int],
    photo_pending: list[int],
    photo_targets: dict[str, str],
    over_budget,
    on_group_updated: Callable[[dict[str, Any]], None] | None = None,
    group_snapshot: dict[str, Any] | None = None,
) -> tuple[int, int, int]:
    """Import a slice of contacts using batchCreateContacts. Returns (new_start, attempts_used, failed)."""
    attempts_used = 0
    failed = 0
    cursor = start
    members_total = len(members_data)

    while cursor < members_total and attempts_used < ops_budget:
        if over_budget():
            break
        chunk_size = min(
            _contact_import_chunk_size(members_total, ops_budget - attempts_used),
            members_total - cursor,
        )
        if chunk_size <= 0:
            break

        indexed_members = [
            (idx, members_data[idx]) for idx in range(cursor, cursor + chunk_size)
        ]
        results = batch_create_contacts_in_group(creds, indexed_members, group_id)
        chunk_failed = 0
        for idx, member in indexed_members:
            resource_id = results.get(idx)
            if _record_create_result(
                idx,
                resource_id,
                member,
                retry_indexes=retry_indexes,
                retry_attempts=retry_attempts,
                skipped_indexes=skipped_indexes,
                photo_pending=photo_pending,
                photo_targets=photo_targets,
            ):
                chunk_failed += 1
                failed += 1
            elif resource_id and group_snapshot is not None:
                _save_sync_mapping_after_create(group_snapshot, member, resource_id)
            attempts_used += 1

        cursor += chunk_size
        if on_group_updated and group_snapshot is not None:
            _notify_contact_import_progress(
                group_snapshot,
                cursor=cursor,
                members_total=len(members_data),
                retry_indexes=retry_indexes,
                skipped_indexes=skipped_indexes,
                photo_pending=photo_pending,
                photo_targets=photo_targets,
                on_group_updated=on_group_updated,
                failed=chunk_failed,
            )

    return cursor, attempts_used, failed


def _photo_target_count(group: dict[str, Any], photo_targets: dict[str, str]) -> int:
    count = len(photo_targets)
    if count > 0:
        group["photoTargetCount"] = count
        return count
    return int(group.get("photoTargetCount") or 0)


def _apply_photo_progress_fields(
    group: dict[str, Any],
    *,
    photo_pending: list[int],
    photo_retry: list[int],
    photo_retry_attempts: dict[str, int],
    photo_targets: dict[str, str],
    photo_done: int,
) -> int:
    group["photoPendingIndexes"] = list(dict.fromkeys(photo_pending))
    group["photoRetryIndexes"] = list(dict.fromkeys(photo_retry))
    group["photoRetryAttempts"] = photo_retry_attempts
    group["photoTargets"] = photo_targets
    photo_target = _photo_target_count(group, photo_targets)
    if photo_target > 0:
        photo_done = min(max(0, int(photo_done)), photo_target)
    group["photoDoneCount"] = photo_done
    return photo_target


def _queue_photo_failure(
    src: int,
    *,
    photo_retry: list[int],
    photo_retry_attempts: dict[str, int],
) -> None:
    key = str(src)
    nxt = photo_retry_attempts.get(key, 0) + 1
    if nxt >= RECIPIENT_IMPORT_MAX_RETRIES:
        photo_retry_attempts.pop(key, None)
    else:
        photo_retry_attempts[key] = nxt
        photo_retry.append(src)


def _invalidate_stale_photo_mapping(
    group: dict[str, Any],
    members_data: list[dict[str, Any]],
    idx: int,
    *,
    photo_pending: list[int],
    photo_retry: list[int],
    photo_retry_attempts: dict[str, int],
    photo_targets: dict[str, str],
    retry_indexes: list[int],
) -> None:
    """Drop dead recipient person id and queue contact re-create before photos."""
    share_id = str(group.get("shareId") or "")
    owner = str(group.get("owner") or "")
    resource_name = str(group.get("resourceName") or "")
    recipient_email = str(group.get("recipientEmail") or "")
    member = members_data[idx] if 0 <= idx < len(members_data) else None
    owner_person_id = str((member or {}).get("resourceName") or "")
    if share_id and owner and resource_name and recipient_email and owner_person_id:
        db.delete_sync_mapping_rows(
            share_id, owner, resource_name, recipient_email, [owner_person_id]
        )
    photo_targets.pop(str(idx), None)
    photo_retry_attempts.pop(str(idx), None)
    if idx in photo_pending:
        photo_pending[:] = [i for i in photo_pending if i != idx]
    if idx in photo_retry:
        photo_retry[:] = [i for i in photo_retry if i != idx]
    if idx not in retry_indexes:
        retry_indexes.append(idx)
    cursor = min(int(group.get("importCursor") or 0), idx)
    group["importCursor"] = cursor
    group["importPhase"] = "contacts"
    total = len(members_data)
    group["status"] = f"Importing ({cursor}/{total})" if total else "Importing"


def _import_photos_pass(
    creds: Credentials,
    members_data: list[dict[str, Any]],
    photo_ops: int,
    *,
    photo_pending: list[int],
    photo_retry: list[int],
    photo_retry_attempts: dict[str, int],
    photo_targets: dict[str, str],
    over_budget,
    group: dict[str, Any] | None = None,
    on_group_updated: Callable[[dict[str, Any]], None] | None = None,
    photo_done: int = 0,
    retry_indexes: list[int] | None = None,
) -> tuple[int, int, int]:
    """Prefetch + batch-upload photos. Returns (used_ops, failures, successes)."""
    if photo_ops <= 0:
        return 0, 0, 0

    retry_indexes = retry_indexes if retry_indexes is not None else []
    from ..people_recipient import recipient_person_exists

    work: list[tuple[int, dict[str, Any], str]] = []
    while photo_retry and len(work) < photo_ops:
        if over_budget():
            break
        src = int(photo_retry.pop(0))
        member = members_data[src] if 0 <= src < len(members_data) else None
        rid = photo_targets.get(str(src))
        if not member or not rid:
            photo_retry_attempts.pop(str(src), None)
            continue
        work.append((src, member, rid))

    while photo_pending and len(work) < photo_ops:
        if over_budget():
            break
        src = int(photo_pending.pop(0))
        member = members_data[src] if 0 <= src < len(members_data) else None
        rid = photo_targets.get(str(src))
        if not member or not rid:
            photo_retry_attempts.pop(str(src), None)
            continue
        work.append((src, member, rid))

    if not work:
        return 0, 0, 0

    live_work: list[tuple[int, dict[str, Any], str]] = []
    for src, member, rid in work:
        if recipient_person_exists(creds, rid):
            live_work.append((src, member, rid))
        elif group is not None:
            _invalidate_stale_photo_mapping(
                group,
                members_data,
                src,
                photo_pending=photo_pending,
                photo_retry=photo_retry,
                photo_retry_attempts=photo_retry_attempts,
                photo_targets=photo_targets,
                retry_indexes=retry_indexes,
            )
    attempted = len(work)
    work = live_work
    if not work:
        return attempted, attempted, 0

    token = creds.token or ""
    owner_token: str | None = None
    if group:
        owner_email = str(group.get("owner") or "").strip()
        if owner_email:
            owner_creds = owner_credentials(owner_email)
            if owner_creds and owner_creds.token:
                owner_token = owner_creds.token
    prefetched = prefetch_photo_bytes(
        [(src, member) for src, member, _ in work],
        token,
        owner_token=owner_token,
    )

    uploads: list[tuple[int, str, str]] = []
    missing_bytes: list[int] = []
    for src, _member, rid in work:
        payload = prefetched.get(src)
        if payload:
            uploads.append((src, rid, payload))
        else:
            missing_bytes.append(src)

    outcomes = batch_upload_contact_photos(creds, uploads) if uploads else {}

    failures = 0
    successes = 0
    for src, _member, rid in work:
        if src in missing_bytes or not outcomes.get(src):
            failures += 1
            if not recipient_person_exists(creds, rid) and group is not None:
                _invalidate_stale_photo_mapping(
                    group,
                    members_data,
                    src,
                    photo_pending=photo_pending,
                    photo_retry=photo_retry,
                    photo_retry_attempts=photo_retry_attempts,
                    photo_targets=photo_targets,
                    retry_indexes=retry_indexes,
                )
            else:
                _queue_photo_failure(
                    src,
                    photo_retry=photo_retry,
                    photo_retry_attempts=photo_retry_attempts,
                )
        else:
            photo_retry_attempts.pop(str(src), None)
            successes += 1

    if group is not None and on_group_updated and work:
        new_photo_done = photo_done + successes
        photo_total = _apply_photo_progress_fields(
            group,
            photo_pending=photo_pending,
            photo_retry=photo_retry,
            photo_retry_attempts=photo_retry_attempts,
            photo_targets=photo_targets,
            photo_done=new_photo_done,
        )
        if not photo_total:
            photo_remaining = len(photo_pending) + len(photo_retry)
            photo_total = new_photo_done + photo_remaining
        group["status"] = f"Photos ({new_photo_done}/{photo_total})"
        group["importPhase"] = "photos"
        on_group_updated(deepcopy(group))

    return len(work), failures, successes


def create_shared_groups(
    shared_groups: list[dict[str, Any]],
    creds: Credentials,
    *,
    max_ops: int | None = None,
    time_budget_ms: int = RECIPIENT_IMPORT_TIME_BUDGET_MS,
    on_group_updated: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    if not shared_groups:
        return []

    started_at = time.time() * 1000
    deduped = _dedupe_shared_groups(shared_groups)
    remaining_ops = RECIPIENT_IMPORT_MAX_OPS_PER_LOAD if max_ops is None else max_ops

    def over_budget() -> bool:
        return (time.time() * 1000 - started_at) > time_budget_ms

    if remaining_ops <= 0:
        for group in deduped:
            total = int(group.get("memberCount") or 0)
            cursor = int(group.get("importCursor") or 0)
            status = str(group.get("status") or "")
            if status.startswith("Waiting for owner sync"):
                continue
            if not status:
                if total > 0 and cursor < total:
                    group["status"] = f"Queued ({cursor}/{total})"
                elif total > 0 and cursor >= total:
                    group["status"] = "Shared"
                    _sync_push_status_field(group)
        return _filter_result(deduped)

    def notify_group_progress(group: dict[str, Any]) -> None:
        if on_group_updated:
            on_group_updated(deepcopy(group))

    for index, group in enumerate(deduped):
        name = group.get("name") or ""
        share_id = group.get("shareId") or ""
        created = _contact_group_resource(group.get("created"))
        if group.get("created") and not created:
            group["created"] = ""
        import_cursor = group.get("importCursor")

        try:
            members_data = db.read_shared_contacts(share_id)
            if not members_data:
                if created and group_exists(creds, created):
                    actual = group_member_count(creds, created)
                    if actual is not None:
                        group["actualCount"] = actual
                        group["importCursor"] = max(
                            int(group.get("importCursor") or 0),
                            actual,
                        )
                    group["status"] = "Shared"
                    _sync_push_status_field(group)
                elif group.get("recovered") or int(group.get("actualCount") or 0) > 0:
                    if created and group_exists(creds, created):
                        actual = group_member_count(creds, created)
                        if actual is not None:
                            group["actualCount"] = actual
                            group["importCursor"] = max(
                                int(group.get("importCursor") or 0),
                                actual,
                            )
                    group["status"] = "Shared (recovered)"
                    _sync_push_status_field(group)
                elif str(group.get("status") or "").startswith("Waiting for owner sync"):
                    continue
                else:
                    group["status"] = "Missing data"
                    group["staleRemove"] = True
                continue

            group_id = _ensure_recipient_group(creds, group, name)
            if not group_id:
                group["status"] = "Error"
                continue

            start = _reconcile_import_progress(creds, group, group_id, len(members_data))
            import_cursor = start
            status_text = str(group.get("status") or "")
            if status_text.startswith("Ready"):
                group["status"] = f"Importing ({start}/{len(members_data)})"
                group["importPhase"] = "contacts"
                notify_group_progress(group)
            retry_indexes: list[int] = list(group.get("retryIndexes") or [])
            retry_attempts: dict[str, int] = dict(group.get("retryAttempts") or {})
            skipped_indexes: list[int] = list(group.get("skippedIndexes") or [])
            _queue_unmapped_member_retries(group, members_data, retry_indexes)
            photo_pending: list[int] = list(group.get("photoPendingIndexes") or [])
            photo_retry: list[int] = list(group.get("photoRetryIndexes") or [])
            photo_retry_attempts: dict[str, int] = dict(group.get("photoRetryAttempts") or {})
            photo_targets: dict[str, str] = _hydrate_photo_targets(
                group,
                members_data,
                dict(group.get("photoTargets") or {}),
            )
            group.pop("photoTargets", None)
            photo_done = int(group.get("photoDoneCount") or 0)
            photo_target_total = _photo_target_count(group, photo_targets)
            _ensure_photo_work_queue(
                members_data,
                photo_targets,
                photo_pending,
                photo_retry,
                photo_done,
                photo_target_total,
            )
            if photo_target_total > 0:
                photo_done = min(photo_done, photo_target_total)

            contacts_exhausted = _contacts_import_complete(
                group, len(members_data), start, retry_indexes
            )
            needs_more = not contacts_exhausted
            if contacts_exhausted:
                group["importCursor"] = len(members_data)
            if not needs_more:
                pass
            else:
                if remaining_ops <= 0:
                    retry_label = f", retry {len(retry_indexes)}" if retry_indexes else ""
                    group["status"] = f"Queued ({start}/{len(members_data)}{retry_label})"
                    group["importPhase"] = "contacts"
                    continue

                ops_budget = min(RECIPIENT_IMPORT_BATCH_SIZE, remaining_ops)
                attempts_used = 0
                failed = 0

                if on_group_updated:
                    _notify_contact_import_progress(
                        group,
                        cursor=start,
                        members_total=len(members_data),
                        retry_indexes=retry_indexes,
                        skipped_indexes=skipped_indexes,
                        photo_pending=photo_pending,
                        photo_targets=photo_targets,
                        on_group_updated=on_group_updated,
                    )

                while retry_indexes and attempts_used < ops_budget:
                    if over_budget():
                        break
                    ri = retry_indexes.pop(0)
                    member = members_data[ri] if ri < len(members_data) else None
                    if not member:
                        continue
                    try:
                        rid = create_contact_in_group(
                            creds, member, group_id, skip_photo=True
                        )
                    except Exception:
                        rid = None
                    retry_failed = _record_create_result(
                        ri,
                        rid,
                        member,
                        retry_indexes=retry_indexes,
                        retry_attempts=retry_attempts,
                        skipped_indexes=skipped_indexes,
                        photo_pending=photo_pending,
                        photo_targets=photo_targets,
                    )
                    if retry_failed:
                        failed += 1
                    elif rid:
                        _save_sync_mapping_after_create(group, member, rid)
                    attempts_used += 1
                    if on_group_updated:
                        _notify_contact_import_progress(
                            group,
                            cursor=start,
                            members_total=len(members_data),
                            retry_indexes=retry_indexes,
                            skipped_indexes=skipped_indexes,
                            photo_pending=photo_pending,
                            photo_targets=photo_targets,
                            on_group_updated=on_group_updated,
                            failed=failed,
                        )

                if start < len(members_data) and attempts_used < ops_budget:
                    mapped_start, mapped_ops = _process_sync_mappings_before_batch(
                        creds,
                        group,
                        group_id,
                        members_data,
                        start,
                        ops_budget - attempts_used,
                        photo_pending=photo_pending,
                        photo_targets=photo_targets,
                        over_budget=over_budget,
                    )
                    start = mapped_start
                    attempts_used += mapped_ops
                    group["importCursor"] = start

                if start < len(members_data) and attempts_used < ops_budget:
                    start, batch_used, batch_failed = _import_contacts_batch(
                        creds,
                        members_data,
                        group_id,
                        start,
                        ops_budget - attempts_used,
                        retry_indexes=retry_indexes,
                        retry_attempts=retry_attempts,
                        skipped_indexes=skipped_indexes,
                        photo_pending=photo_pending,
                        photo_targets=photo_targets,
                        over_budget=over_budget,
                        on_group_updated=on_group_updated,
                        group_snapshot=group,
                    )
                    attempts_used += batch_used
                    failed += batch_failed

                remaining_ops -= attempts_used
                group["importCursor"] = start
                group["retryIndexes"] = list(dict.fromkeys(retry_indexes))
                group["retryAttempts"] = retry_attempts
                group["skippedIndexes"] = list(dict.fromkeys(skipped_indexes))
                group["skippedCount"] = len(group["skippedIndexes"])
                group["photoPendingIndexes"] = list(dict.fromkeys(photo_pending))
                group["photoRetryIndexes"] = list(dict.fromkeys(photo_retry))
                group["photoRetryAttempts"] = photo_retry_attempts
                group["photoTargets"] = photo_targets
                group["photoDoneCount"] = photo_done

                actual = group_member_count(creds, group_id)
                if actual is not None:
                    group["actualCount"] = actual

                if start < len(members_data) or group["retryIndexes"]:
                    retry_label = (
                        f", retry {len(group['retryIndexes'])}"
                        if group["retryIndexes"]
                        else ""
                    )
                    fail_label = f", {failed} failed this pass" if failed else ""
                    group["status"] = (
                        f"Importing ({start}/{len(members_data)}{retry_label}{fail_label})"
                    )
                    group["importPhase"] = "contacts"
                    notify_group_progress(group)
                    continue

            if RECIPIENT_IMPORT_INCLUDE_PHOTOS:
                photo_target_total = _photo_target_count(group, photo_targets)
                _normalize_photo_queues(
                    photo_pending, photo_retry, photo_targets, members_data
                )
                if _photos_import_complete(
                    photo_done,
                    photo_target_total,
                    photo_pending,
                    photo_retry,
                ):
                    skipped_count = len(group.get("skippedIndexes") or [])
                    group["status"] = (
                        f"Shared ({skipped_count} skipped)"
                        if skipped_count
                        else "Shared"
                    )
                    group["importPhase"] = "done"
                    _apply_photo_progress_fields(
                        group,
                        photo_pending=photo_pending,
                        photo_retry=photo_retry,
                        photo_retry_attempts=photo_retry_attempts,
                        photo_targets=photo_targets,
                        photo_done=photo_done,
                    )
                    notify_group_progress(group)
                    _sync_push_status_field(group)
                    continue
                photo_remaining = len(photo_pending) + len(photo_retry)
                if (
                    start >= len(members_data)
                    and not retry_indexes
                    and photo_target_total > 0
                    and photo_done < photo_target_total
                ):
                    group["importPhase"] = "photos"
                    group["status"] = f"Photos ({photo_done}/{photo_target_total})"
                    _apply_photo_progress_fields(
                        group,
                        photo_pending=photo_pending,
                        photo_retry=photo_retry,
                        photo_retry_attempts=photo_retry_attempts,
                        photo_targets=photo_targets,
                        photo_done=photo_done,
                    )
                    notify_group_progress(group)

                while True:
                    photo_remaining = len(photo_pending) + len(photo_retry)
                    if photo_remaining <= 0 and (
                        not photo_target_total or photo_done >= photo_target_total
                    ):
                        break
                    if remaining_ops <= 0 or over_budget():
                        break

                    photo_ops = min(
                        RECIPIENT_IMPORT_PHOTO_BATCH_SIZE, max(1, remaining_ops)
                    )
                    if photo_remaining <= 30:
                        photo_ops = min(
                            photo_ops, RECIPIENT_IMPORT_PROGRESS_BATCH_SIZE
                        )
                    if photo_remaining > 0 or photo_done > 0:
                        photo_total = photo_target_total or (photo_done + photo_remaining)
                        group["importPhase"] = "photos"
                        group["status"] = f"Photos ({photo_done}/{photo_total})"
                        _apply_photo_progress_fields(
                            group,
                            photo_pending=photo_pending,
                            photo_retry=photo_retry,
                            photo_retry_attempts=photo_retry_attempts,
                            photo_targets=photo_targets,
                            photo_done=photo_done,
                        )
                        notify_group_progress(group)

                    photo_used, photo_failures, photo_successes = _import_photos_pass(
                        creds,
                        members_data,
                        photo_ops,
                        photo_pending=photo_pending,
                        photo_retry=photo_retry,
                        photo_retry_attempts=photo_retry_attempts,
                        photo_targets=photo_targets,
                        over_budget=over_budget,
                        group=group,
                        on_group_updated=on_group_updated,
                        photo_done=photo_done,
                        retry_indexes=retry_indexes,
                    )
                    if retry_indexes:
                        notify_group_progress(group)
                        break
                    if photo_used <= 0:
                        break

                    photo_done += photo_successes
                    if photo_target_total > 0:
                        photo_done = min(photo_done, photo_target_total)
                    remaining_ops -= photo_used
                    photo_target_total = _apply_photo_progress_fields(
                        group,
                        photo_pending=photo_pending,
                        photo_retry=photo_retry,
                        photo_retry_attempts=photo_retry_attempts,
                        photo_targets=photo_targets,
                        photo_done=photo_done,
                    )

                    photo_remaining = len(photo_pending) + len(photo_retry)
                    photo_total = photo_target_total or (photo_done + photo_remaining)
                    fail_label = (
                        f", {photo_failures} photo failures this pass"
                        if photo_failures
                        else ""
                    )
                    group["status"] = (
                        f"Photos ({photo_done}/{photo_total}{fail_label})"
                    )
                    group["importPhase"] = "photos"
                    notify_group_progress(group)
                    if photo_remaining <= 0 and (
                        not photo_target_total or photo_done >= photo_target_total
                    ):
                        break

            skipped_count = len(group.get("skippedIndexes") or [])
            photo_target_total = _photo_target_count(group, photo_targets)
            _normalize_photo_queues(
                photo_pending, photo_retry, photo_targets, members_data
            )
            photos_complete = not RECIPIENT_IMPORT_INCLUDE_PHOTOS or _photos_import_complete(
                photo_done,
                photo_target_total,
                photo_pending,
                photo_retry,
            )
            if photos_complete:
                group["status"] = (
                    f"Shared ({skipped_count} skipped)"
                    if skipped_count
                    else "Shared"
                )
                group["importPhase"] = "done"
            else:
                photo_total = photo_target_total or (photo_done + photo_remaining)
                group["importPhase"] = "photos"
                group["status"] = f"Photos ({photo_done}/{photo_total})"
                _apply_photo_progress_fields(
                    group,
                    photo_pending=photo_pending,
                    photo_retry=photo_retry,
                    photo_retry_attempts=photo_retry_attempts,
                    photo_targets=photo_targets,
                    photo_done=photo_done,
                )
                notify_group_progress(group)
            _sync_push_status_field(group)

        except Exception as exc:
            if is_oauth_refresh_error(exc):
                recipient = str(group.get("recipientEmail") or "").strip().lower()
                if recipient:
                    clear_recipient_connection(recipient)
                group["status"] = "Reconnect account to import"
            else:
                group["status"] = f"Error: {exc}"
            _sync_push_status_field(group)

    return _filter_result(deduped)


def _sync_push_status_field(group: dict[str, Any]) -> None:
    """Keep pushStatus aligned with status so owner UI does not stay on Pushing."""
    status = str(group.get("status") or "")
    if status:
        group["pushStatus"] = status


def _persist_shared_group(
    recipient_email: str,
    owner_key: str,
    resource_name: str,
    group_state: dict[str, Any],
) -> None:
    app_data = db.get_app_data(recipient_email) or {}
    shared_groups: list[dict[str, Any]] = list(app_data.get("sharedGroups") or [])
    target_index = next(
        (
            i
            for i, g in enumerate(shared_groups)
            if g
            and g.get("owner") == owner_key
            and g.get("resourceName") == resource_name
        ),
        -1,
    )
    if target_index < 0:
        return
    persisted = deepcopy(group_state)
    persisted.pop("photoTargets", None)
    shared_groups[target_index] = persisted
    app_data["sharedGroups"] = shared_groups
    db.save_app_data(recipient_email, app_data)


def _slim_shared_group_for_poll(group: dict[str, Any]) -> dict[str, Any]:
    """Lightweight group payload for progress polling (avoids huge photoTargets blobs)."""
    photo_pending = list(group.get("photoPendingIndexes") or [])
    photo_retry = list(group.get("photoRetryIndexes") or [])
    return {
        "name": group.get("name"),
        "owner": group.get("owner"),
        "resourceName": group.get("resourceName"),
        "shareId": group.get("shareId"),
        "memberCount": group.get("memberCount"),
        "status": group.get("status"),
        "importCursor": group.get("importCursor"),
        "importPhase": group.get("importPhase"),
        "photoDoneCount": group.get("photoDoneCount"),
        "photoTargetCount": group.get("photoTargetCount"),
        "photoPendingCount": len(photo_pending),
        "photoPendingIndexes": photo_pending,
        "photoRetryIndexes": photo_retry,
        "skippedCount": group.get("skippedCount"),
        "actualCount": group.get("actualCount"),
        "retryIndexes": list(group.get("retryIndexes") or []),
        "created": group.get("created"),
    }


def get_recipient_shared_groups(recipient_email: str) -> dict[str, Any]:
    from .app_data import _filter_dismissed_shared_groups

    app_data = db.get_app_data(recipient_email) or {}
    shared_groups = [
        _slim_shared_group_for_poll(g)
        for g in _filter_dismissed_shared_groups(app_data)
        if g
    ]
    return {"sharedGroups": shared_groups}


def _visible_shared_groups(recipient_email: str) -> list[dict[str, Any]]:
    from .app_data import _filter_dismissed_shared_groups

    app_data = db.get_app_data(recipient_email) or {}
    return _filter_dismissed_shared_groups(app_data)


def process_shared_import_for_group(
    recipient_email: str,
    owner_key: str,
    resource_name: str,
    max_ops: int = 200,
) -> dict[str, Any]:
    from ..google_auth import recipient_credentials

    creds = recipient_credentials(recipient_email)
    if not creds:
        raise PermissionError("Connect your account first, then retry import.")

    lock = _lock_for(recipient_email)
    if not lock.acquire(blocking=False):
        return {"sharedGroups": _visible_shared_groups(recipient_email), "busy": True}

    try:
        app_data = db.get_app_data(recipient_email) or {}
        shared_groups: list[dict[str, Any]] = list(
            app_data.get("sharedGroups") or []
        )
        owner_key_normalized = db.normalize_email(owner_key)
        target_index = next(
            (
                i
                for i, g in enumerate(shared_groups)
                if g
                and db.normalize_email(str(g.get("owner") or "")) == owner_key_normalized
                and g.get("resourceName") == resource_name
            ),
            -1,
        )
        if target_index < 0:
            from .recipient_recovery import repair_recipient_app_data_from_staged

            if repair_recipient_app_data_from_staged(recipient_email):
                app_data = db.get_app_data(recipient_email) or {}
                shared_groups = list(app_data.get("sharedGroups") or [])
                target_index = next(
                    (
                        i
                        for i, g in enumerate(shared_groups)
                        if g
                        and db.normalize_email(str(g.get("owner") or "")) == owner_key_normalized
                        and g.get("resourceName") == resource_name
                    ),
                    -1,
                )
            if target_index < 0:
                return {"sharedGroups": _visible_shared_groups(recipient_email), "busy": False}

        target = shared_groups[target_index]
        target["recipientEmail"] = recipient_email
        progress_cb = lambda group_state: _persist_shared_group(
            recipient_email, owner_key, resource_name, group_state
        )
        processed = create_shared_groups(
            [deepcopy(target)],
            creds,
            max_ops=max_ops,
            time_budget_ms=RECIPIENT_IMPORT_TIME_BUDGET_MS,
            on_group_updated=progress_cb,
        )

        shared_groups = [
            g
            for g in shared_groups
            if not (
                g
                and g.get("owner") == owner_key
                and g.get("resourceName") == resource_name
            )
        ]
        if processed:
            insert_at = min(target_index, len(shared_groups))
            cleaned = deepcopy(processed[0])
            cleaned.pop("photoTargets", None)
            shared_groups.insert(insert_at, cleaned)

        app_data["sharedGroups"] = shared_groups
        db.save_app_data(recipient_email, app_data)
        return {
            "sharedGroups": _visible_shared_groups(recipient_email),
            "busy": False,
        }
    finally:
        lock.release()
