"""Durable worker for extract_jobs.

One process claims one job at a time; Postgres leases make restarts and
stale claims recoverable. Production Compose runs this alongside the API.
"""

from __future__ import annotations

import logging
import signal
from threading import Event, Thread
from uuid import UUID

from app.apns import apns_client
from app.config import settings
from app.db import db_context, fetch_one, get_pool, reset_db
from app.pipeline import run_extract_job, run_translation_job
from app.store import claim_push_delivery, finish_push_delivery

logger = logging.getLogger(__name__)
_stop = Event()
_CLAIM_NEXT_EXTRACT_JOB = "claim_next_extract_job"


def _handle_signal(signum: int, _frame) -> None:
    logger.info("worker stopping signal=%s", signum)
    _stop.set()


def _claim_next_job() -> dict | None:
    if settings.maintenance_mode:
        return None
    return fetch_one(f"select * from {_CLAIM_NEXT_EXTRACT_JOB}(%s)", (settings.worker_lease_seconds,))


def _run_claimed_job(row: dict) -> None:
    if settings.maintenance_mode:
        return
    job_id = UUID(str(row["id"]))
    user_id = UUID(str(row["user_id"]))
    kind = str(row.get("job_kind") or "extract")
    if kind == "translation":
        recipe_id = row.get("recipe_id")
        if not recipe_id:
            logger.error("worker invalid translation claim job_id=%s", job_id)
            return
        run_translation_job(
            job_id,
            user_id,
            UUID(str(recipe_id)),
            str(row.get("language_code") or "en-US"),
        )
        return
    if kind != "extract":
        logger.error("worker unsupported job kind job_id=%s kind=%s", job_id, kind)
        return
    run_extract_job(
        job_id,
        user_id,
        str(row["source_url_raw"]),
        str(row["source_url_norm"]),
        str(row.get("language_code") or "en-US"),
    )


def _run_push_delivery(row: dict) -> None:
    try:
        job_id = UUID(str(row["job_id"]))
        delivery_id = UUID(str(row["id"]))
        recipe_id = UUID(str(row["recipe_id"]))
        user_id = UUID(str(row["user_id"]))
        token_hash = str(row["token_hash"])
        result = apns_client.send(
            token=str(row["device_token"]),
            environment=str(row["environment"]),
            job_id=job_id,
            recipe_id=recipe_id,
            language_code=str(row.get("language_code") or "en-US"),
        )
        attempt = int(row.get("attempts") or 1)
        backoff = min(5 * (2 ** max(0, attempt - 1)), 3600)
        retry_after = max(result.retry_after_seconds, backoff)
        finish_push_delivery(
            delivery_id=delivery_id,
            token_hash=token_hash,
            user_id=user_id,
            status=result.status,
            error_code=result.error_code,
            retry_after_seconds=retry_after,
        )
    except Exception as exc:
        logger.error("push delivery failed error_type=%s", type(exc).__name__)
        try:
            finish_push_delivery(
                delivery_id=UUID(str(row["id"])),
                token_hash=str(row["token_hash"]),
                user_id=UUID(str(row["user_id"])),
                status="retry",
                error_code="worker_error",
                retry_after_seconds=60,
            )
        except Exception:
            logger.error("push delivery state update failed error_type=%s", type(exc).__name__)


def _heartbeat_loop() -> None:
    while not _stop.is_set():
        try:
            with db_context(actor="service"):
                fetch_one(
                    """
                    insert into app_runtime_heartbeats (service_name, last_seen_at)
                    values ('recipe-worker', now())
                    on conflict (service_name) do update set last_seen_at = excluded.last_seen_at
                    returning service_name
                    """
                )
        except Exception as exc:
            logger.error("worker heartbeat failed error_type=%s", type(exc).__name__)
        _stop.wait(settings.worker_heartbeat_interval_seconds)


def _push_loop() -> None:
    with db_context(actor="service"):
        while not _stop.is_set():
            try:
                if apns_client.enabled:
                    row = claim_push_delivery()
                    if row:
                        _run_push_delivery(row)
                        continue
            except Exception as exc:
                logger.error("push poll failed error_type=%s", type(exc).__name__)
            _stop.wait(max(0.5, min(settings.worker_poll_seconds, 60.0)))


def main() -> None:
    if not settings.worker_enabled:
        logger.info("worker disabled; set WORKER_ENABLED=true to run")
        return
    settings.validate_database()
    get_pool()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    logger.info("worker started poll_seconds=%s lease_seconds=%s", settings.worker_poll_seconds, settings.worker_lease_seconds)
    Thread(target=_heartbeat_loop, name="worker-heartbeat", daemon=True).start()
    if settings.apns_enabled:
        Thread(target=_push_loop, name="apns-delivery", daemon=True).start()
    try:
        with db_context(actor="service"):
            while not _stop.is_set():
                try:
                    row = _claim_next_job()
                    if row:
                        _run_claimed_job(row)
                    else:
                        _stop.wait(max(0.5, min(settings.worker_poll_seconds, 60.0)))
                except Exception as exc:
                    # A transient database/network issue must not kill the worker. Do
                    # not log exception text because providers can echo source data.
                    logger.error(
                        "worker poll or job dispatch failed error_type=%s",
                        type(exc).__name__,
                    )
                    _stop.wait(max(1.0, min(settings.worker_poll_seconds, 60.0)))
    finally:
        reset_db()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
