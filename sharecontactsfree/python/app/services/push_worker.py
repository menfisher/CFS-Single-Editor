"""Background push job queue for server-side recipient sync."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from .. import db
from ..google_auth import recipient_credentials
from .app_data import maybe_refresh_staged_shared_contacts_before_import
from .contact_sync import sync_contact_changes
from .recipient_import import process_shared_import_for_group

logger = logging.getLogger(__name__)

_worker_thread: threading.Thread | None = None
_worker_started = False
_worker_lock = threading.Lock()

MAX_FULL_GROUP_PASSES = 120
MAX_CONTACT_BATCH_PASSES = 20
# Release a long-running job so other queued groups (e.g. TriState) can make progress.
MAX_FULL_GROUP_WALL_SECONDS = 240

# Hard ceiling for a single job. A job is *supposed* to self-yield at
# MAX_FULL_GROUP_WALL_SECONDS, but that check only runs between passes — it cannot
# fire while the thread is blocked inside a single pass that makes many sequential
# Google calls (each only individually bounded). The watchdog runs every job in a
# daemon subthread and abandons it if it blows past this ceiling, so one stalled
# job can never permanently wedge the single worker thread (and the whole queue).
MAX_JOB_HARD_SECONDS = int(os.getenv("SHARE_JOB_HARD_TIMEOUT", "360"))
# How many watchdog timeouts a single job may rack up before it is parked as an
# error instead of retried (prevents an unfixable "poison" job looping forever).
MAX_WATCHDOG_STRIKES = int(os.getenv("SHARE_JOB_WATCHDOG_STRIKES", "3"))

# Jobs whose worker thread was abandoned after a watchdog timeout but may still be
# alive — used to avoid running the same job twice concurrently.
_abandoned_jobs: dict[int, threading.Thread] = {}
_watchdog_strikes: dict[int, int] = {}


def start_push_worker() -> None:
    global _worker_thread, _worker_started
    with _worker_lock:
        reclaimed = db.reclaim_all_running_push_jobs_on_startup()
        if reclaimed:
            logger.info("Reclaimed %s running share push jobs after cold start", reclaimed)
        _repair_recipient_app_data_if_needed()
        if _worker_started and _worker_thread and _worker_thread.is_alive():
            return
        # Cold start only: re-drive any import that was left mid-flight (e.g. a
        # redeploy/instance-kill during the photo phase) so it resumes instead of
        # silently stalling with no job behind it.
        _resume_orphaned_imports()
        _worker_started = True
        _worker_thread = threading.Thread(target=_worker_loop, name="share-push-worker", daemon=True)
        _worker_thread.start()


def kick_push_queue(max_jobs: int = 1) -> None:
    """Process queued push jobs while this Cloud Run instance is warm."""
    start_push_worker()
    if db.count_queued_push_jobs() <= 0:
        return
    run_pending_jobs(max_jobs=max_jobs)


def _repair_recipient_app_data_if_needed() -> None:
    from .recipient_recovery import repair_recipient_app_data_from_staged

    for recipient_email in db.list_recipient_emails():
        try:
            repair_recipient_app_data_from_staged(recipient_email)
        except Exception:
            logger.exception("Failed to repair recipient app_data for %s", recipient_email)


def _resume_orphaned_imports() -> None:
    """Re-enqueue connected shared groups left mid-import with no active push job.

    If a worker thread dies mid-pass (instance killed, redeploy during the photo
    phase, etc.), a group can be stranded in an in-progress state like
    'Photos (0/20)' with its job already marked done — nothing left to drive it.
    On cold start we scan for those and queue a fresh full_group job so the import
    always finishes. Enqueue directly (no kick) to avoid re-entering the worker
    lock; the worker loop picks the jobs up within ~1s.
    """
    in_progress_prefixes = ("Importing", "Photos", "Pushing", "Queued")
    resumed = 0
    for recipient_email in db.list_recipient_emails():
        # NB: don't call recipient_credentials() here — it refreshes the OAuth
        # token over the network and this runs inside the worker-startup lock.
        # The job handler (_run_full_group_job) re-checks credentials when it runs
        # and parks disconnected recipients as "Waiting for recipient to connect".
        data = db.get_app_data(recipient_email) or {}
        seen: set[tuple[str, str]] = set()
        for group in data.get("sharedGroups") or []:
            status = str(group.get("status") or "")
            phase = str(group.get("importPhase") or "")
            if status.startswith(("Shared", "Error")):
                continue
            if not (phase in ("contacts", "photos") or status.startswith(in_progress_prefixes)):
                continue
            owner = str(group.get("owner") or "").strip()
            resource_name = str(group.get("resourceName") or "").strip()
            if not owner or not resource_name:
                continue
            key = (owner, resource_name)
            if key in seen:
                continue
            seen.add(key)
            if db.has_active_push_job(owner, resource_name, recipient_email):
                continue
            try:
                db.enqueue_push_job(
                    owner=owner,
                    resource_name=resource_name,
                    recipient_email=recipient_email,
                    job_type="full_group",
                    payload={},
                )
                resumed += 1
                logger.info(
                    "Resumed orphaned import: %s -> %s (%s)",
                    owner,
                    recipient_email,
                    status or phase,
                )
            except Exception:
                logger.exception(
                    "Failed to resume orphaned import for %s", recipient_email
                )
    if resumed:
        logger.info("Re-enqueued %s orphaned import(s) on startup", resumed)


def _worker_loop() -> None:
    while True:
        try:
            db.reclaim_stale_push_jobs()
            run_pending_jobs(max_jobs=1)
        except Exception:
            logger.exception("share push worker loop error")
        time.sleep(1.0)


def enqueue_full_group_push(owner: str, resource_name: str, recipient_email: str) -> int:
    if db.has_active_push_job(owner, resource_name, recipient_email):
        rows = db.list_push_jobs(owner, resource_name, limit=20)
        for row in rows:
            if (
                row.get("recipient_email") == db.normalize_email(recipient_email)
                and row.get("status") in ("queued", "running")
            ):
                return int(row["id"])
    job_id = db.enqueue_push_job(
        owner=owner,
        resource_name=resource_name,
        recipient_email=recipient_email,
        job_type="full_group",
        payload={},
    )
    kick_push_queue(max_jobs=1)
    return job_id


def enqueue_contact_push(owner: str, owner_person_ids: list[str]) -> int:
    normalized = [
        str(pid).strip()
        for pid in owner_person_ids
        if str(pid or "").strip().startswith("people/")
    ]
    job_id = db.enqueue_push_job(
        owner=owner,
        job_type="contact_batch",
        payload={"owner_person_ids": normalized},
    )
    kick_push_queue(max_jobs=1)
    return job_id


def _dispatch_job(job: dict[str, Any]) -> dict[str, Any]:
    if job["job_type"] == "full_group":
        return _run_full_group_job(job)
    if job["job_type"] == "contact_batch":
        return _run_contact_batch_job(job)
    db.update_push_job(job["id"], status="error", last_error="Unknown job type")
    return {"job_id": job["id"], "status": "error"}


def _handle_job_exception(job: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    logger.exception("push job %s failed", job.get("id"))
    err = str(exc)
    disk_io = "disk I/O error" in err.lower()
    if disk_io or int(job.get("attempts") or 0) < 5:
        db.update_push_job(job["id"], requeue=True, last_error=err[:500])
    else:
        db.update_push_job(job["id"], status="error", last_error=err[:500])
    return {"job_id": job["id"], "status": "error", "error": err}


def _run_job_with_watchdog(job: dict[str, Any]) -> dict[str, Any]:
    """Run a job in a daemon subthread bounded by MAX_JOB_HARD_SECONDS.

    If the job blows past the hard ceiling the worker abandons the (still-running)
    subthread and moves on, so a single stalled Google call can never wedge the
    queue. The abandoned thread is tracked so the same job isn't run twice at once.
    """
    job_id = int(job["id"])

    prev = _abandoned_jobs.get(job_id)
    if prev is not None:
        if prev.is_alive():
            # A prior abandoned run of this exact job is still winding down. Don't
            # start a second worker for it — requeue and let it settle first.
            db.update_push_job(
                job_id, requeue=True, last_error="awaiting abandoned worker exit"
            )
            time.sleep(0.5)
            return {"job_id": job_id, "status": "queued", "reason": "abandoned_alive"}
        _abandoned_jobs.pop(job_id, None)

    holder: dict[str, Any] = {}

    def _target() -> None:
        try:
            holder["outcome"] = _dispatch_job(job)
        except BaseException as exc:  # noqa: BLE001 - surfaced to caller below
            holder["exc"] = exc

    thread = threading.Thread(
        target=_target, name=f"share-push-job-{job_id}", daemon=True
    )
    thread.start()
    thread.join(MAX_JOB_HARD_SECONDS)

    if thread.is_alive():
        _abandoned_jobs[job_id] = thread
        strikes = _watchdog_strikes.get(job_id, 0) + 1
        _watchdog_strikes[job_id] = strikes
        msg = (
            f"job exceeded hard time limit ({MAX_JOB_HARD_SECONDS}s) and was "
            f"abandoned (stalled Google API call); strike {strikes}"
        )
        logger.error("push job %s watchdog timeout (strike %s)", job_id, strikes)
        if strikes >= MAX_WATCHDOG_STRIKES:
            db.update_push_job(job_id, status="error", last_error=msg[:500])
            return {"job_id": job_id, "status": "error", "reason": "watchdog_timeout"}
        db.update_push_job(job_id, requeue=True, last_error=msg[:500])
        return {"job_id": job_id, "status": "queued", "reason": "watchdog_timeout"}

    _watchdog_strikes.pop(job_id, None)
    if "exc" in holder:
        return _handle_job_exception(job, holder["exc"])
    return holder.get("outcome") or {"job_id": job_id, "status": "done"}


def run_pending_jobs(max_jobs: int = 1) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for job in db.claim_push_jobs(max_jobs):
        results.append(_run_job_with_watchdog(job))
    return results


def _mark_recipient_push_running(
    recipient_email: str,
    owner: str,
    resource_name: str,
) -> None:
    """Mark push as running without clobbering in-progress photo import status."""
    data = db.get_app_data(recipient_email) or {}
    groups = list(data.get("sharedGroups") or [])
    changed = False
    for group in groups:
        if group.get("owner") == owner and group.get("resourceName") == resource_name:
            status = str(group.get("status") or "")
            import_phase = str(group.get("importPhase") or "")
            group["pushStatus"] = "Running"
            if import_phase == "photos" or status.startswith(
                ("Photos (", "Importing photos", "Importing (", "Queued (")
            ):
                changed = True
                break
            if not status.startswith(("Pushing", "Importing")):
                group["status"] = "Pushing"
            changed = True
            break
    if changed:
        data["sharedGroups"] = groups
        db.save_app_data(recipient_email, data)


def _set_recipient_push_status(
    recipient_email: str,
    owner: str,
    resource_name: str,
    status: str,
) -> None:
    data = db.get_app_data(recipient_email) or {}
    groups = list(data.get("sharedGroups") or [])
    changed = False
    for group in groups:
        if group.get("owner") == owner and group.get("resourceName") == resource_name:
            group["status"] = status
            group["pushStatus"] = status
            changed = True
            break
    if changed:
        data["sharedGroups"] = groups
        db.save_app_data(recipient_email, data)


def _finalize_group_if_photos_done(
    recipient_email: str, owner: str, resource_name: str
) -> bool:
    """Mark Shared when photoDoneCount reached target (clears stale pending leftovers)."""
    data = db.get_app_data(recipient_email) or {}
    groups = list(data.get("sharedGroups") or [])
    changed = False
    for group in groups:
        if group.get("owner") == owner and group.get("resourceName") == resource_name:
            status = str(group.get("status") or "")
            if status.startswith("Shared"):
                return True
            photo_done = int(group.get("photoDoneCount") or 0)
            photo_target = int(group.get("photoTargetCount") or 0)
            if photo_target > 0 and photo_done >= photo_target:
                skipped = len(group.get("skippedIndexes") or [])
                group["status"] = (
                    f"Shared ({skipped} skipped)" if skipped else "Shared"
                )
                group["importPhase"] = "done"
                group["pushStatus"] = group["status"]
                group["photoPendingIndexes"] = []
                group["photoRetryIndexes"] = []
                group.pop("photoTargets", None)
                changed = True
            break
    if changed:
        data["sharedGroups"] = groups
        db.save_app_data(recipient_email, data)
        return True
    return False


def _group_is_complete(recipient_email: str, owner: str, resource_name: str) -> bool:
    _finalize_group_if_photos_done(recipient_email, owner, resource_name)
    data = db.get_app_data(recipient_email) or {}
    for group in data.get("sharedGroups") or []:
        if group.get("owner") == owner and group.get("resourceName") == resource_name:
            status = str(group.get("status") or "")
            return status.startswith("Shared") or status.startswith("Error")
    return False


def _group_is_pending(recipient_email: str, owner: str, resource_name: str) -> bool:
    data = db.get_app_data(recipient_email) or {}
    for group in data.get("sharedGroups") or []:
        if group.get("owner") == owner and group.get("resourceName") == resource_name:
            status = str(group.get("status") or "")
            if status.startswith("Shared") or status.startswith("Error"):
                return False
            if status.startswith("Waiting for owner sync"):
                return False
            return True
    return False


def _sync_recipient_push_status_from_group(
    recipient_email: str, owner: str, resource_name: str
) -> None:
    data = db.get_app_data(recipient_email) or {}
    groups = list(data.get("sharedGroups") or [])
    changed = False
    for group in groups:
        if group.get("owner") == owner and group.get("resourceName") == resource_name:
            status = str(group.get("status") or "")
            if status:
                group["pushStatus"] = status
                changed = True
            break
    if changed:
        data["sharedGroups"] = groups
        db.save_app_data(recipient_email, data)


def _run_full_group_job(job: dict[str, Any]) -> dict[str, Any]:
    owner = job["owner"]
    resource_name = job["resource_name"]
    recipient_email = job["recipient_email"]
    job_id = int(job["id"])

    if not recipient_credentials(recipient_email):
        db.update_push_job(job_id, status="error", last_error="Recipient not connected")
        _set_recipient_push_status(
            recipient_email, owner, resource_name, "Waiting for recipient to connect"
        )
        return {"job_id": job_id, "status": "error", "reason": "not_connected"}

    _mark_recipient_push_running(recipient_email, owner, resource_name)

    maybe_refresh_staged_shared_contacts_before_import(
        recipient_email=recipient_email,
        owner_email=owner,
        resource_name=resource_name,
    )

    started_at = time.time()
    for _ in range(MAX_FULL_GROUP_PASSES):
        if time.time() - started_at >= MAX_FULL_GROUP_WALL_SECONDS:
            db.update_push_job(job_id, requeue=True, last_error="yielding for fair queue")
            return {"job_id": job_id, "status": "queued", "reason": "wall_clock"}

        db.update_push_job(job_id, touch=True)

        if _group_is_complete(recipient_email, owner, resource_name):
            _sync_recipient_push_status_from_group(recipient_email, owner, resource_name)
            db.update_push_job(job_id, status="done", last_error="")
            return {"job_id": job_id, "status": "done"}

        result = process_shared_import_for_group(
            recipient_email, owner, resource_name, max_ops=200
        )
        if result.get("busy"):
            time.sleep(0.5)
            continue

        if not _group_is_pending(recipient_email, owner, resource_name):
            _sync_recipient_push_status_from_group(recipient_email, owner, resource_name)
            db.update_push_job(job_id, status="done", last_error="")
            return {"job_id": job_id, "status": "done"}

        time.sleep(0.3)

    db.update_push_job(job_id, requeue=True, last_error="")
    return {"job_id": job_id, "status": "queued", "reason": "in_progress"}


def _run_contact_batch_job(job: dict[str, Any]) -> dict[str, Any]:
    owner = job["owner"]
    job_id = int(job["id"])
    person_ids = list((job.get("payload") or {}).get("owner_person_ids") or [])
    if not person_ids:
        db.update_push_job(job_id, status="done", last_error="")
        return {"job_id": job_id, "status": "done", "updated": 0}

    summary = sync_contact_changes(owner, person_ids)
    pending = int(summary.get("pending") or 0)
    if pending > 0 and int(job.get("attempts") or 0) < MAX_CONTACT_BATCH_PASSES:
        db.update_push_job(job_id, requeue=True, last_error="")
        return {"job_id": job_id, "status": "queued", "summary": summary}

    if int(summary.get("errors") or 0) > 0 and not summary.get("updated"):
        db.update_push_job(
            job_id,
            status="error",
            last_error=json.dumps(summary.get("error_details") or [])[:500],
        )
    else:
        db.update_push_job(job_id, status="done", last_error="")
    return {"job_id": job_id, "status": "done", "summary": summary}
