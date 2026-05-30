import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import signal
import zipfile
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow
from .ignition import IGNITION_DATASETS, fetch_ignition_collection, get_ignition_connection_for_user, ignition_oauth_configured
from .xero import (
    CONTACTS_URL,
    CREDIT_NOTES_URL,
    INVOICES_URL,
    OVERPAYMENTS_URL,
    PAYMENTS_URL,
    XERO_RATE_LIMIT_RETRIES,
    allocate_credit_note,
    allocate_overpayment,
    attach_file_to_invoice,
    create_credit_note,
    create_history_record,
    create_sales_invoice,
    fetch_paginated_collection,
    merge_contacts,
    normalise_contact,
    normalise_invoice,
    normalise_payment,
    xero_api_get,
)

logger = logging.getLogger(__name__)
ACTIVE_SYNC_STATUSES = ("queued", "running")
SYNC_STALE_AFTER = timedelta(minutes=15)
PANEL_PAYMENT_LIMIT = 1000
JENIUS_NOTE_SIGNATURE = "By Jenius AI"
ACCREC_INVOICE_WHERE = 'Type=="ACCREC"'
OUTSTANDING_INVOICE_WHERE = 'Type=="ACCREC"&&Status!="VOIDED"&&Status!="DELETED"&&Status!="PAID"'
PAID_INVOICE_WHERE = 'Type=="ACCREC"&&Status=="PAID"'
RECEIVABLE_CREDIT_NOTE_WHERE = 'Type=="ACCRECCREDIT"&&Status=="AUTHORISED"'
RECEIVABLE_CREDIT_NOTE_INCREMENTAL_WHERE = 'Type=="ACCRECCREDIT"'
AUTHORISED_CREDIT_STATUS = "AUTHORISED"
OUTSTANDING_READY_STEP = "Backfilling paid invoices"
WORKING_DATA_STEPS = (OUTSTANDING_READY_STEP, "Fetching paid invoices from Xero", "Backfilling paid invoices")
DEFAULT_SYNC_SCOPE = "full_history"
INCREMENTAL_SYNC_OVERLAP = timedelta(minutes=5)
SYNC_SCOPE_OPTIONS = {
    "outstanding_only": {
        "label": "Outstanding invoices only",
        "paid_page_limit": 0,
        "summary": "Import outstanding invoices only.",
    },
    "outstanding_plus_500_paid": {
        "label": "Outstanding + 500 paid invoices",
        "paid_page_limit": 5,
        "summary": "Import outstanding invoices and the next 500 paid invoices.",
    },
    "outstanding_plus_2500_paid": {
        "label": "Outstanding + 2,500 paid invoices",
        "paid_page_limit": 25,
        "summary": "Import outstanding invoices and the next 2,500 paid invoices.",
    },
    "full_history": {
        "label": "Full invoice history",
        "paid_page_limit": None,
        "summary": "Import outstanding invoices and all paid invoice history.",
    },
}
SYNC_SCOPE_RANK = {scope: index for index, scope in enumerate(SYNC_SCOPE_OPTIONS)}
LATE_PAYMENT_CHARGE_BASE_AMOUNTS = (Decimal("20.00"), Decimal("30.00"), Decimal("50.00"))
DEFAULT_LATE_PAYMENT_CHARGE_BASE_AMOUNT = LATE_PAYMENT_CHARGE_BASE_AMOUNTS[0]
LATE_PAYMENT_CHARGE_VAT_RATE = Decimal("0.20")
OPENAI_INSIGHTS_TIMEOUT_SECONDS = 135
SYNC_PHASE_OUTSTANDING = "outstanding_invoices"
SYNC_PHASE_PAYMENTS = "payments"
SYNC_PHASE_CREDITS = "customer_credits"
SYNC_PHASE_PAID_INVOICES = "paid_invoices"
_PREVIOUS_SIGTERM_HANDLER = None
_SYNC_SIGNAL_HANDLERS_INSTALLED = False


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def _safe_json(value):
    if isinstance(value, dict):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item) for item in value]
    if isinstance(value, (date, datetime, Decimal, UUID)):
        return _json_default(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _with_jenius_signature(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return JENIUS_NOTE_SIGNATURE
    if text.lower().endswith(JENIUS_NOTE_SIGNATURE.lower()):
        return text
    return f"{text} {JENIUS_NOTE_SIGNATURE}"


def record_audit_event(entity_type: str, entity_id: str, event_type: str, payload: dict, user_id: str | None) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (entity_type, entity_id, event_type, json.dumps(payload, default=_json_default), user_id),
            )
        connection.commit()


def get_xero_connection_for_user(user_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM xero_connections WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT *
                    FROM xero_connections
                    WHERE (SELECT COUNT(*) FROM xero_connections) = 1
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User has not linked Xero yet.")
    return row


def disconnect_xero(user: dict) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH user_connection AS (
                    SELECT id
                    FROM xero_connections
                    WHERE user_id = %s
                ),
                fallback_connection AS (
                    SELECT id
                    FROM xero_connections
                    WHERE (SELECT COUNT(*) FROM xero_connections) = 1
                      AND NOT EXISTS (SELECT 1 FROM user_connection)
                    LIMIT 1
                )
                DELETE FROM xero_connections
                WHERE id IN (
                    SELECT id FROM user_connection
                    UNION
                    SELECT id FROM fallback_connection
                )
                RETURNING tenant_name
                """,
                (user["id"],),
            )
            row = cursor.fetchone()
            cursor.execute(
                """
                UPDATE sync_runs
                SET status = %s,
                    current_step = %s,
                    summary = %s,
                    error_message = %s,
                    failed_count = GREATEST(failed_count, 1),
                    completed_at = %s
                WHERE provider = %s
                  AND initiated_by_user_id = %s
                  AND status IN ('queued', 'running')
                """,
                (
                    "failed",
                    "Sync stopped",
                    "Xero was disconnected before the sync completed.",
                    "Xero is disconnected. Reconnect Xero before syncing again.",
                    utcnow(),
                    "xero",
                    user["id"],
                ),
            )
        connection.commit()

    if row:
        record_audit_event(
            "xero_connection",
            str(user["id"]),
            "xero.disconnected",
            {"tenant_name": row.get("tenant_name")},
            user["id"],
        )
    return {"disconnected": bool(row), "tenant_name": row.get("tenant_name") if row else None}


def factory_reset_console(user: dict) -> dict:
    tenant_id = None
    try:
        tenant_id = get_xero_connection_for_user(user["id"]).get("tenant_id")
    except HTTPException:
        tenant_id = None

    with get_connection() as connection:
        with connection.cursor() as cursor:
            if tenant_id:
                cursor.execute("DELETE FROM customers WHERE tenant_id = %s", (tenant_id,))
            else:
                cursor.execute("DELETE FROM customers")
            customers_deleted = cursor.rowcount
            cursor.execute(
                "DELETE FROM sync_runs WHERE provider = %s AND initiated_by_user_id = %s",
                ("xero", user["id"]),
            )
            sync_runs_deleted = cursor.rowcount
            cursor.execute("DELETE FROM audit_events")
        connection.commit()

    record_audit_event(
        "console",
        str(user["id"]),
        "factory_reset.completed",
        {
            "tenant_id": tenant_id,
            "customers_deleted": customers_deleted,
            "sync_runs_deleted": sync_runs_deleted,
        },
        user["id"],
    )
    return {
        "tenantId": tenant_id,
        "customersDeleted": customers_deleted,
        "syncRunsDeleted": sync_runs_deleted,
    }


def _sync_error_message(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)
        return str(detail)
    return str(exc) or exc.__class__.__name__


def _sync_error_payload(exc: Exception) -> dict:
    if isinstance(exc, HTTPException):
        return {
            "type": "HTTPException",
            "status_code": exc.status_code,
            "detail": exc.detail,
        }
    return {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }


def _format_wait_seconds(seconds: int) -> str:
    seconds = max(int(seconds or 0), 0)
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _xero_rate_limit_retry_seconds(exc: Exception) -> int | None:
    if not isinstance(exc, HTTPException) or not isinstance(exc.detail, dict):
        return None
    if exc.detail.get("status_code") != status.HTTP_429_TOO_MANY_REQUESTS:
        return None
    raw_delay = exc.detail.get("retry_after_seconds") or exc.detail.get("retry_after") or 0
    try:
        return max(0, int(raw_delay))
    except (TypeError, ValueError):
        return 0


def _active_xero_rate_limit(user_id: str) -> dict | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT rate_limit_until, retry_after_seconds, error_message
                FROM sync_runs
                WHERE provider = %s
                  AND initiated_by_user_id = %s
                  AND rate_limit_until IS NOT NULL
                  AND rate_limit_until > NOW()
                ORDER BY rate_limit_until DESC, completed_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """,
                ("xero", user_id),
            )
            row = cursor.fetchone()
            cursor.execute(
                """
                SELECT created_at, payload
                FROM audit_events
                WHERE user_id = %s
                  AND event_type = %s
                ORDER BY created_at DESC
                LIMIT 20
                """,
                (user_id, "sync.failed"),
            )
            audit_rows = cursor.fetchall()
        connection.commit()

    if row is not None:
        remaining_seconds = max(0, int((row["rate_limit_until"] - utcnow()).total_seconds()) + 1)
        return {
            "rate_limit_until": row["rate_limit_until"],
            "retry_after_seconds": remaining_seconds,
            "message": (
                f"Xero has rate-limited this connection. Wait about {_format_wait_seconds(remaining_seconds)} "
                "before starting another sync."
            ),
            "previous_error": row.get("error_message") or "",
        }

    for audit_row in audit_rows:
        payload = audit_row.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
        nested_detail = detail.get("detail") if isinstance(detail.get("detail"), dict) else {}
        message = str(payload.get("error") or nested_detail.get("message") or "")
        if "rate-limit" not in message.lower() or "contacts request" in message.lower():
            continue
        retry_after_seconds = nested_detail.get("retry_after_seconds") or nested_detail.get("retry_after") or 0
        try:
            retry_after_seconds = max(0, int(retry_after_seconds))
        except (TypeError, ValueError):
            continue
        rate_limit_until = audit_row["created_at"] + timedelta(seconds=retry_after_seconds)
        if rate_limit_until <= utcnow():
            continue
        remaining_seconds = max(0, int((rate_limit_until - utcnow()).total_seconds()) + 1)
        return {
            "rate_limit_until": rate_limit_until,
            "retry_after_seconds": remaining_seconds,
            "message": (
                f"Xero has rate-limited this connection. Wait about {_format_wait_seconds(remaining_seconds)} "
                "before starting another sync."
            ),
            "previous_error": message,
        }

    return None


def normalise_sync_options(options: dict | None = None) -> dict:
    options = options or {}
    invoice_scope = str(options.get("invoiceScope") or DEFAULT_SYNC_SCOPE)
    if invoice_scope not in SYNC_SCOPE_OPTIONS:
        invoice_scope = DEFAULT_SYNC_SCOPE
    scope = SYNC_SCOPE_OPTIONS[invoice_scope]
    raw_years = options.get("invoiceYears") or options.get("invoice_years") or []
    if not isinstance(raw_years, list):
        raw_years = [raw_years]
    current_year = utcnow().year
    invoice_years = sorted(
        {
            int(year)
            for year in raw_years
            if str(year).isdigit() and 2000 <= int(year) <= current_year + 1
        }
    )
    year_summary = "Years: all available invoice years." if not invoice_years else f"Years: {', '.join(str(year) for year in invoice_years)}."
    return {
        "invoice_scope": invoice_scope,
        "label": scope["label"],
        "paid_page_limit": scope["paid_page_limit"],
        "invoice_years": invoice_years,
        "year_label": "No years selected" if not invoice_years else ", ".join(str(year) for year in invoice_years),
        "summary": f"{scope['summary']} {year_summary}",
    }


def _sync_options_signature(sync_options: dict) -> str:
    return json.dumps(
        {
            "invoice_scope": sync_options.get("invoice_scope") or DEFAULT_SYNC_SCOPE,
            "invoice_years": sync_options.get("invoice_years") or [],
            "paid_page_limit": sync_options.get("paid_page_limit"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _upsert_sync_checkpoint(
    sync_run_id: str,
    user_id: str,
    tenant_id: str,
    sync_signature: str,
    phase: str,
    status_value: str,
    page_number: int = 0,
    records_seen: int = 0,
    records_stored: int = 0,
    payload: dict | None = None,
) -> dict | None:
    now = utcnow()
    completed_at = now if status_value == "completed" else None
    sync_run_key = str(sync_run_id)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_checkpoints (
                    sync_run_id, provider, initiated_by_user_id, tenant_id, sync_signature,
                    phase, status, page_number, records_seen, records_stored, payload,
                    updated_at, completed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (sync_run_id, phase) DO UPDATE
                SET status = EXCLUDED.status,
                    page_number = EXCLUDED.page_number,
                    records_seen = EXCLUDED.records_seen,
                    records_stored = EXCLUDED.records_stored,
                    payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at,
                    completed_at = EXCLUDED.completed_at
                RETURNING *
                """,
                (
                    sync_run_key,
                    "xero",
                    user_id,
                    tenant_id,
                    sync_signature,
                    phase,
                    status_value,
                    max(int(page_number or 0), 0),
                    max(int(records_seen or 0), 0),
                    max(int(records_stored or 0), 0),
                    json.dumps(payload or {}, default=_json_default),
                    now,
                    completed_at,
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def _latest_resumable_sync_state(user_id: str, tenant_id: str, sync_signature: str) -> dict | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT sync_runs.*
                FROM sync_runs
                WHERE sync_runs.provider = %s
                  AND sync_runs.initiated_by_user_id = %s
                  AND sync_runs.status = %s
                  AND EXISTS (
                      SELECT 1
                      FROM sync_checkpoints
                      WHERE sync_checkpoints.sync_run_id = sync_runs.id::text
                        AND sync_checkpoints.tenant_id = %s
                        AND sync_checkpoints.sync_signature = %s
                  )
                ORDER BY sync_runs.completed_at DESC NULLS LAST, sync_runs.created_at DESC
                LIMIT 1
                """,
                ("xero", user_id, "failed", tenant_id, sync_signature),
            )
            sync_run = cursor.fetchone()
            if sync_run is None:
                connection.commit()
                return None

            cursor.execute(
                """
                SELECT *
                FROM sync_checkpoints
                WHERE sync_run_id = %s
                  AND tenant_id = %s
                  AND sync_signature = %s
                """,
                (str(sync_run["id"]), tenant_id, sync_signature),
            )
            checkpoints = {row["phase"]: row for row in cursor.fetchall()}
        connection.commit()

    return {"sync_run": sync_run, "checkpoints": checkpoints}


def _checkpoint_completed(checkpoints: dict, phase: str) -> bool:
    checkpoint = checkpoints.get(phase)
    return bool(checkpoint and checkpoint.get("status") == "completed")


def _checkpoint_payload_count(checkpoint: dict | None, key: str, fallback: int = 0) -> int:
    payload = (checkpoint or {}).get("payload") or {}
    try:
        return int(payload.get(key, fallback) or 0)
    except (TypeError, ValueError):
        return fallback


def _invoice_year_sql_filter(invoice_years: list[int], column: str = "invoices.invoice_date") -> tuple[str, list]:
    if not invoice_years:
        return "", []
    return f" AND EXTRACT(YEAR FROM {column})::INT = ANY(%s)", [invoice_years]


def _local_resume_counts(tenant_id: str, invoice_years: list[int]) -> dict:
    year_filter, year_params = _invoice_year_sql_filter(invoice_years)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM customers WHERE tenant_id = %s", (tenant_id,))
            customer_count = int((cursor.fetchone() or {}).get("count") or 0)
            cursor.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE customers.tenant_id = %s
                  AND invoices.status NOT IN ('VOIDED', 'DELETED', 'PAID')
                  {year_filter}
                """,
                (tenant_id, *year_params),
            )
            outstanding_count = int((cursor.fetchone() or {}).get("count") or 0)
            cursor.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE customers.tenant_id = %s
                  AND invoices.status = 'PAID'
                  {year_filter}
                """,
                (tenant_id, *year_params),
            )
            paid_count = int((cursor.fetchone() or {}).get("count") or 0)
        connection.commit()

    return {"customers": customer_count, "outstanding_invoices": outstanding_count, "paid_invoices": paid_count}


def _xero_year_filter(invoice_years: list[int]) -> str:
    clauses = [
        f"(Date>=DateTime({year}, 1, 1)&&Date<DateTime({year + 1}, 1, 1))"
        for year in invoice_years
    ]
    return f"({'||'.join(clauses)})" if clauses else ""


def _with_invoice_year_filter(base_where: str, invoice_years: list[int]) -> str:
    year_filter = _xero_year_filter(invoice_years)
    return f"{base_where}&&{year_filter}" if year_filter else base_where


def _latest_completed_sync_started_at(user_id: str) -> datetime | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COALESCE(started_at, created_at, completed_at) AS sync_started_at
                FROM sync_runs
                WHERE provider = %s
                  AND initiated_by_user_id = %s
                  AND status = %s
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC, created_at DESC
                LIMIT 1
                """,
                ("xero", user_id, "completed"),
            )
            row = cursor.fetchone()
        connection.commit()

    return row["sync_started_at"] if row and row.get("sync_started_at") else None


def _incremental_modified_since(user_id: str) -> datetime | None:
    latest_started_at = _latest_completed_sync_started_at(user_id)
    if latest_started_at is None:
        return None
    return latest_started_at - INCREMENTAL_SYNC_OVERLAP


def _local_invoice_years_cover(tenant_id: str, invoice_years: list[int]) -> bool:
    if not invoice_years:
        return False
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT EXTRACT(YEAR FROM invoices.invoice_date)::INT AS invoice_year
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE customers.tenant_id = %s
                  AND invoices.invoice_date IS NOT NULL
                """,
                (tenant_id,),
            )
            imported_years = {row["invoice_year"] for row in cursor.fetchall() if row.get("invoice_year")}
        connection.commit()
    return set(invoice_years).issubset(imported_years)


def _sync_scope_from_summary(summary: str) -> str | None:
    summary = summary.lower()
    for scope, option in SYNC_SCOPE_OPTIONS.items():
        if option["label"].lower() in summary:
            return scope
    if "outstanding sync complete" in summary or "outstanding invoices only" in summary:
        return "outstanding_only"
    return None


def _completed_sync_covers_scope(user_id: str, invoice_scope: str) -> bool:
    requested_rank = SYNC_SCOPE_RANK.get(invoice_scope, 0)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT summary, current_step
                FROM sync_runs
                WHERE provider = %s
                  AND initiated_by_user_id = %s
                  AND status = %s
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC, created_at DESC
                LIMIT 20
                """,
                ("xero", user_id, "completed"),
            )
            rows = cursor.fetchall()
        connection.commit()

    for row in rows:
        completed_scope = _sync_scope_from_summary(" ".join([row.get("summary") or "", row.get("current_step") or ""]))
        if completed_scope is not None and SYNC_SCOPE_RANK.get(completed_scope, 0) >= requested_rank:
            return True
    return False


def _refresh_customer_totals(cursor, tenant_id: str, updated_at: datetime) -> None:
    cursor.execute(
        """
        WITH invoice_totals AS (
            SELECT invoices.customer_id,
                   COALESCE(SUM(invoices.amount_due), 0) AS gross_total_due,
                   COALESCE(SUM(CASE WHEN invoices.due_date < CURRENT_DATE AND invoices.amount_due > 0 THEN invoices.amount_due ELSE 0 END), 0) AS overdue_amount
            FROM invoices
            JOIN customers AS invoice_customers ON invoice_customers.id = invoices.customer_id
            WHERE invoice_customers.tenant_id = %s
            GROUP BY invoices.customer_id
        ),
        credit_totals AS (
            SELECT customer_credits.customer_id,
                   COALESCE(SUM(customer_credits.remaining_credit), 0) AS credit_balance
            FROM customer_credits
            JOIN customers AS credit_customers ON credit_customers.id = customer_credits.customer_id
            WHERE credit_customers.tenant_id = %s
              AND customer_credits.remaining_credit > 0
            GROUP BY customer_credits.customer_id
        ),
        totals AS (
            SELECT invoice_totals.customer_id,
                   invoice_totals.gross_total_due,
                   invoice_totals.overdue_amount,
                   COALESCE(credit_totals.credit_balance, 0) AS credit_balance,
                   GREATEST(invoice_totals.gross_total_due - COALESCE(credit_totals.credit_balance, 0), 0) AS net_total_due
            FROM invoice_totals
            LEFT JOIN credit_totals ON credit_totals.customer_id = invoice_totals.customer_id
        )
        UPDATE customers
        SET total_due = totals.net_total_due,
            overdue_amount = LEAST(totals.overdue_amount, totals.net_total_due),
            updated_at = %s
        FROM totals
        WHERE customers.id = totals.customer_id
        """,
        (tenant_id, tenant_id, updated_at),
    )


async def _sync_xero_payments(
    connection_row: dict,
    now: datetime,
    modified_since: datetime | None = None,
    start_page: int = 1,
    already_processed: int = 0,
    already_synced: int = 0,
    on_page=None,
    on_retry=None,
    on_store=None,
    on_checkpoint=None,
    on_request=None,
) -> int:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, xero_contact_id FROM customers WHERE tenant_id = %s",
                (connection_row["tenant_id"],),
            )
            customer_lookup = {
                row["xero_contact_id"]: row["id"]
                for row in cursor.fetchall()
                if row.get("xero_contact_id")
            }
            cursor.execute(
                """
                SELECT invoices.id, invoices.xero_invoice_id, invoices.customer_id
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE customers.tenant_id = %s
                """,
                (connection_row["tenant_id"],),
            )
            invoice_lookup = {
                row["xero_invoice_id"]: {"id": row["id"], "customer_id": row["customer_id"]}
                for row in cursor.fetchall()
                if row.get("xero_invoice_id")
            }
        connection.commit()

    synced = max(int(already_synced or 0), 0)
    processed = max(int(already_processed or 0), 0)
    if on_store is not None:
        on_store(processed, processed, synced)

    def store_payment_page(page_number: int, raw_payments: list[dict], total_records: int) -> None:
        nonlocal processed, synced
        if not raw_payments:
            if on_store is not None:
                on_store(processed, total_records, synced)
            return

        page_processed = 0
        page_synced = 0
        with get_connection() as connection:
            with connection.cursor() as cursor:
                for raw_payment in raw_payments:
                    page_processed += 1
                    payment = normalise_payment(raw_payment)
                    if not payment.get("xero_payment_id"):
                        continue
                    if payment.get("invoice_type") and payment.get("invoice_type") != "ACCREC":
                        continue
                    invoice_match = invoice_lookup.get(payment.get("xero_invoice_id"))
                    customer_id = customer_lookup.get(payment.get("xero_contact_id")) or (invoice_match or {}).get("customer_id")
                    if customer_id is None:
                        continue
                    cursor.execute(
                        """
                        INSERT INTO payments (
                            tenant_id, customer_id, invoice_id, xero_payment_id, xero_invoice_id,
                            invoice_number, payment_date, amount, currency_code, reference,
                            status, account_name, raw, synced_at, updated_at
                        )
                        VALUES (
                            %(tenant_id)s, %(customer_id)s, %(invoice_id)s, %(xero_payment_id)s, %(xero_invoice_id)s,
                            %(invoice_number)s, %(payment_date)s, %(amount)s, %(currency_code)s, %(reference)s,
                            %(status)s, %(account_name)s, %(raw_json)s::jsonb, %(synced_at)s, %(updated_at)s
                        )
                        ON CONFLICT (xero_payment_id) DO UPDATE
                        SET customer_id = EXCLUDED.customer_id,
                            invoice_id = EXCLUDED.invoice_id,
                            xero_invoice_id = EXCLUDED.xero_invoice_id,
                            invoice_number = EXCLUDED.invoice_number,
                            payment_date = EXCLUDED.payment_date,
                            amount = EXCLUDED.amount,
                            currency_code = EXCLUDED.currency_code,
                            reference = EXCLUDED.reference,
                            status = EXCLUDED.status,
                            account_name = EXCLUDED.account_name,
                            raw = EXCLUDED.raw,
                            synced_at = EXCLUDED.synced_at,
                            updated_at = EXCLUDED.updated_at
                        """,
                        {
                            **payment,
                            "tenant_id": connection_row["tenant_id"],
                            "customer_id": customer_id,
                            "invoice_id": (invoice_match or {}).get("id"),
                            "raw_json": json.dumps(payment.get("raw") or {}, default=_json_default),
                            "synced_at": now,
                            "updated_at": now,
                        },
                    )
                    page_synced += 1
            connection.commit()

        processed += page_processed
        synced += page_synced
        if on_store is not None:
            on_store(processed, total_records, synced)
        if on_checkpoint is not None:
            on_checkpoint(page_number, processed, synced)

    await fetch_paginated_collection(
        connection_row,
        PAYMENTS_URL,
        "Payments",
        start_page=start_page,
        on_page=on_page,
        on_batch=store_payment_page,
        on_retry=on_retry,
        on_request=on_request,
        modified_since=modified_since,
        collect_records=False,
        initial_records=processed,
    )
    return synced


def _credit_source_storage_type(source_type: str) -> str:
    return {
        "creditNote": "credit_note",
        "credit_note": "credit_note",
        "credit-note": "credit_note",
        "overpayment": "overpayment",
    }.get(str(source_type or ""), "")


def _credit_source_is_active(source_type: str, source: dict) -> bool:
    status_value = str(source.get("status") or "").upper()
    transaction_type = str(source.get("type") or "").upper()
    if status_value != AUTHORISED_CREDIT_STATUS:
        return False
    if source_type == "credit_note" and transaction_type and transaction_type != "ACCRECCREDIT":
        return False
    if source_type == "overpayment" and transaction_type and "RECEIVE" not in transaction_type:
        return False
    return _money(source.get("remainingCredit")) > 0


def _upsert_customer_credit_source(cursor, tenant_id: str, customer_id: str, source_type: str, source: dict, now: datetime) -> bool:
    storage_type = _credit_source_storage_type(source_type)
    source_id = str(source.get("id") or "").strip()
    if not storage_type or not source_id:
        return False

    if not _credit_source_is_active(storage_type, source):
        cursor.execute(
            """
            DELETE FROM customer_credits
            WHERE tenant_id = %s
              AND source_type = %s
              AND xero_credit_id = %s
            """,
            (tenant_id, storage_type, source_id),
        )
        return False

    cursor.execute(
        """
        INSERT INTO customer_credits (
            tenant_id, customer_id, source_type, xero_credit_id, number, reference,
            status, transaction_type, credit_date, currency_code, total,
            remaining_credit, applied_amount, line_items, allocations, synced_at, updated_at
        )
        VALUES (
            %(tenant_id)s, %(customer_id)s, %(source_type)s, %(xero_credit_id)s, %(number)s, %(reference)s,
            %(status)s, %(transaction_type)s, %(credit_date)s, %(currency_code)s, %(total)s,
            %(remaining_credit)s, %(applied_amount)s, %(line_items_json)s::jsonb, %(allocations_json)s::jsonb, %(synced_at)s, %(updated_at)s
        )
        ON CONFLICT (source_type, xero_credit_id) DO UPDATE
        SET tenant_id = EXCLUDED.tenant_id,
            customer_id = EXCLUDED.customer_id,
            number = EXCLUDED.number,
            reference = EXCLUDED.reference,
            status = EXCLUDED.status,
            transaction_type = EXCLUDED.transaction_type,
            credit_date = EXCLUDED.credit_date,
            currency_code = EXCLUDED.currency_code,
            total = EXCLUDED.total,
            remaining_credit = EXCLUDED.remaining_credit,
            applied_amount = EXCLUDED.applied_amount,
            line_items = EXCLUDED.line_items,
            allocations = EXCLUDED.allocations,
            synced_at = EXCLUDED.synced_at,
            updated_at = EXCLUDED.updated_at
        """,
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "source_type": storage_type,
            "xero_credit_id": source_id,
            "number": source.get("number") or "",
            "reference": source.get("reference") or "",
            "status": source.get("status") or "",
            "transaction_type": source.get("type") or "",
            "credit_date": _parse_optional_iso_date(source.get("date")),
            "currency_code": source.get("currencyCode") or "GBP",
            "total": _money(source.get("total")),
            "remaining_credit": _money(source.get("remainingCredit")),
            "applied_amount": _money(source.get("appliedAmount")),
            "line_items_json": json.dumps(source.get("lineItems") or [], default=_json_default),
            "allocations_json": json.dumps(source.get("allocations") or [], default=_json_default),
            "synced_at": now,
            "updated_at": now,
        },
    )
    return True


def _replace_customer_credit_sources(cursor, tenant_id: str, customer_id: str, credit_sources: list[dict], now: datetime) -> int:
    cursor.execute(
        """
        DELETE FROM customer_credits
        WHERE tenant_id = %s
          AND customer_id = %s
        """,
        (tenant_id, customer_id),
    )
    stored = 0
    for source in credit_sources:
        stored += int(_upsert_customer_credit_source(cursor, tenant_id, customer_id, source.get("sourceType"), source, now))
    _refresh_customer_totals(cursor, tenant_id, now)
    return stored


async def _sync_xero_customer_credits(
    connection_row: dict,
    now: datetime,
    modified_since: datetime | None = None,
    on_credit_note_page=None,
    on_credit_note_retry=None,
    on_overpayment_page=None,
    on_overpayment_retry=None,
    on_request=None,
) -> int:
    credit_note_where = RECEIVABLE_CREDIT_NOTE_INCREMENTAL_WHERE if modified_since else RECEIVABLE_CREDIT_NOTE_WHERE
    raw_credit_notes = await fetch_paginated_collection(
        connection_row,
        CREDIT_NOTES_URL,
        "CreditNotes",
        params={"where": credit_note_where},
        modified_since=modified_since,
        on_page=on_credit_note_page,
        on_retry=on_credit_note_retry,
        on_request=on_request,
    )
    raw_overpayments = await fetch_paginated_collection(
        connection_row,
        OVERPAYMENTS_URL,
        "Overpayments",
        params=None if modified_since else {"where": f'Status=="{AUTHORISED_CREDIT_STATUS}"'},
        modified_since=modified_since,
        on_page=on_overpayment_page,
        on_retry=on_overpayment_retry,
        on_request=on_request,
    )

    if modified_since is not None and not raw_credit_notes and not raw_overpayments:
        return 0

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, xero_contact_id FROM customers WHERE tenant_id = %s",
                (connection_row["tenant_id"],),
            )
            customer_lookup = {
                str(row["xero_contact_id"]).lower(): row["id"]
                for row in cursor.fetchall()
                if row.get("xero_contact_id")
            }
            if modified_since is None:
                cursor.execute("DELETE FROM customer_credits WHERE tenant_id = %s", (connection_row["tenant_id"],))

            stored = 0
            for raw_credit_note in raw_credit_notes:
                source = _serialize_credit_note_transaction(raw_credit_note)
                customer_id = customer_lookup.get(source.get("contactId"))
                if customer_id is None:
                    continue
                stored += int(_upsert_customer_credit_source(cursor, connection_row["tenant_id"], customer_id, "credit_note", source, now))

            for raw_overpayment in raw_overpayments:
                source = _serialize_overpayment_transaction(raw_overpayment)
                customer_id = customer_lookup.get(source.get("contactId"))
                if customer_id is None:
                    continue
                stored += int(_upsert_customer_credit_source(cursor, connection_row["tenant_id"], customer_id, "overpayment", source, now))

            _refresh_customer_totals(cursor, connection_row["tenant_id"], now)
        connection.commit()
    return stored


async def _sync_xero_paid_invoice_backfill(
    connection_row: dict,
    user: dict,
    now: datetime,
    invoice_years: list[int],
    paid_page_limit: int | None,
    base_contact_count: int,
    base_invoice_count: int,
    start_page: int = 1,
    already_seen: int = 0,
    already_synced: int = 0,
    already_contacts_synced: int = 0,
    on_page=None,
    on_retry=None,
    on_store=None,
    on_checkpoint=None,
    on_request=None,
) -> dict:
    paid_synced = max(int(already_synced or 0), 0)
    paid_contacts_synced = max(int(already_contacts_synced or 0), 0)
    synced_invoices = base_invoice_count + paid_synced

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, xero_contact_id FROM customers WHERE tenant_id = %s",
                (connection_row["tenant_id"],),
            )
            customer_rows = cursor.fetchall()
        connection.commit()

    existing_contact_ids = {
        row["xero_contact_id"]
        for row in customer_rows
        if row.get("xero_contact_id")
    }
    customer_lookup = {
        row["xero_contact_id"]: row["id"]
        for row in customer_rows
        if row.get("xero_contact_id")
    }

    def store_paid_invoice_page(page_number: int, raw_invoices: list[dict], total_records: int) -> None:
        nonlocal paid_synced, paid_contacts_synced, synced_invoices, customer_lookup
        page_contacts_synced = 0
        page_paid_synced = 0

        with get_connection() as connection:
            with connection.cursor() as cursor:
                for raw_contact in _contacts_from_invoices(raw_invoices):
                    contact_id = raw_contact.get("ContactID")
                    if not contact_id or contact_id in existing_contact_ids:
                        continue
                    _upsert_xero_customer(cursor, raw_contact, connection_row["tenant_id"], now)
                    existing_contact_ids.add(contact_id)
                    page_contacts_synced += 1

                if page_contacts_synced:
                    cursor.execute(
                        "SELECT id, xero_contact_id FROM customers WHERE tenant_id = %s",
                        (connection_row["tenant_id"],),
                    )
                    customer_lookup = {
                        row["xero_contact_id"]: row["id"]
                        for row in cursor.fetchall()
                        if row.get("xero_contact_id")
                    }

                for raw_invoice in raw_invoices:
                    invoice = normalise_invoice(raw_invoice)
                    if not invoice["xero_contact_id"]:
                        continue

                    customer_id = customer_lookup.get(invoice["xero_contact_id"])
                    if customer_id is None:
                        continue

                    cursor.execute(
                        """
                        INSERT INTO invoices (
                            customer_id, xero_invoice_id, invoice_number, status, due_date, invoice_date,
                            description, line_items, currency_code, total, amount_due, amount_paid, xero_updated_at, synced_at, updated_at
                        )
                        VALUES (
                            %(customer_id)s, %(xero_invoice_id)s, %(invoice_number)s, %(status)s, %(due_date)s, %(invoice_date)s,
                            %(description)s, %(line_items_json)s::jsonb, %(currency_code)s, %(total)s, %(amount_due)s, %(amount_paid)s, %(xero_updated_at)s, %(synced_at)s, %(updated_at)s
                        )
                        ON CONFLICT (xero_invoice_id) DO UPDATE
                        SET customer_id = EXCLUDED.customer_id,
                            invoice_number = EXCLUDED.invoice_number,
                            status = EXCLUDED.status,
                            due_date = EXCLUDED.due_date,
                            invoice_date = EXCLUDED.invoice_date,
                            description = EXCLUDED.description,
                            line_items = EXCLUDED.line_items,
                            currency_code = EXCLUDED.currency_code,
                            total = EXCLUDED.total,
                            amount_due = EXCLUDED.amount_due,
                            amount_paid = EXCLUDED.amount_paid,
                            xero_updated_at = EXCLUDED.xero_updated_at,
                            synced_at = EXCLUDED.synced_at,
                            updated_at = EXCLUDED.updated_at
                        RETURNING id
                        """,
                        {
                            **invoice,
                            "line_items_json": json.dumps(invoice.get("line_items") or [], default=_json_default),
                            "customer_id": customer_id,
                            "synced_at": now,
                            "updated_at": now,
                        },
                    )
                    stored = cursor.fetchone()
                    synced_invoices += 1
                    page_paid_synced += 1

                    cursor.execute(
                        """
                        INSERT INTO invoice_status_history (invoice_id, status, note, changed_by_user_id)
                        SELECT %s, %s, %s, %s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM invoice_status_history
                            WHERE invoice_id = %s AND status = %s
                        )
                        """,
                        (
                            stored["id"],
                            invoice["status"],
                            "Imported from Xero paid invoice backfill",
                            user["id"],
                            stored["id"],
                            invoice["status"],
                        ),
                    )

                paid_synced += page_paid_synced
                paid_contacts_synced += page_contacts_synced
                _refresh_customer_totals(cursor, connection_row["tenant_id"], now)
            connection.commit()

        if on_store is not None:
            on_store(
                page_number,
                total_records,
                paid_synced,
                paid_contacts_synced,
                synced_invoices,
            )
        if on_checkpoint is not None:
            on_checkpoint(
                page_number,
                total_records,
                paid_synced,
                paid_contacts_synced,
                synced_invoices,
            )

    await fetch_paginated_collection(
        connection_row,
        INVOICES_URL,
        "Invoices",
        params={"where": _with_invoice_year_filter(PAID_INVOICE_WHERE, invoice_years)},
        max_pages=paid_page_limit,
        start_page=start_page,
        on_page=on_page,
        on_batch=store_paid_invoice_page,
        on_retry=on_retry,
        on_request=on_request,
        collect_records=False,
        initial_records=already_seen,
    )

    return {
        "paid_synced": paid_synced,
        "paid_contacts_synced": paid_contacts_synced,
        "synced_invoices": synced_invoices,
        "total_contacts_synced": base_contact_count + paid_contacts_synced,
    }


def record_sync_start_failure(user: dict, exc: Exception) -> None:
    message = _sync_error_message(exc)
    try:
        record_audit_event(
            "sync_run",
            str(user.get("id") or "unknown"),
            "sync.start.failed",
            {"error": message, "detail": _sync_error_payload(exc)},
            user.get("id"),
        )
    except Exception:
        logger.exception("Unable to record sync start failure audit event")


def _mark_stale_sync_runs(user_id: str) -> None:
    now = utcnow()
    stale_before = now - SYNC_STALE_AFTER
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, current_step, summary, customers_synced, invoices_synced,
                       contacts_total, invoices_total, fetched_count, processed_count,
                       heartbeat_at, started_at, created_at
                FROM sync_runs
                WHERE provider = %s
                  AND initiated_by_user_id = %s
                  AND status IN ('queued', 'running')
                  AND COALESCE(heartbeat_at, started_at, created_at) < %s
                """,
                ("xero", user_id, stale_before),
            )
            stalled_rows = cursor.fetchall()

            if stalled_rows:
                cursor.execute(
                    """
                    UPDATE sync_runs
                    SET status = %s,
                        current_step = %s,
                        summary = %s,
                        error_message = %s,
                        failed_count = GREATEST(failed_count, 1),
                        completed_at = %s
                    WHERE id = ANY(%s)
                    """,
                    (
                        "failed",
                        "Sync timed out",
                        "A previous Xero sync stopped responding.",
                        "The previous Xero sync stopped responding. Start a fresh sync.",
                        now,
                        [row["id"] for row in stalled_rows],
                    ),
                )
        connection.commit()

    for row in stalled_rows or []:
        last_heartbeat = row.get("heartbeat_at") or row.get("started_at") or row.get("created_at")
        elapsed_seconds = None
        if last_heartbeat is not None:
            elapsed_seconds = max(0, int((now - last_heartbeat).total_seconds()))
        try:
            record_audit_event(
                "sync_run",
                str(row["id"]),
                "sync.stalled",
                {
                    "message": (
                        f"Sync marked stale after {elapsed_seconds} seconds without a heartbeat"
                        if elapsed_seconds is not None
                        else "Sync marked stale (no heartbeat timestamp recorded)."
                    ),
                    "last_step": row.get("current_step") or "",
                    "last_summary": row.get("summary") or "",
                    "last_heartbeat_at": _iso(last_heartbeat),
                    "elapsed_seconds_since_heartbeat": elapsed_seconds,
                    "stale_threshold_seconds": int(SYNC_STALE_AFTER.total_seconds()),
                    "customers_synced": int(row.get("customers_synced") or 0),
                    "invoices_synced": int(row.get("invoices_synced") or 0),
                    "contacts_total": int(row.get("contacts_total") or 0),
                    "invoices_total": int(row.get("invoices_total") or 0),
                    "fetched_count": int(row.get("fetched_count") or 0),
                    "processed_count": int(row.get("processed_count") or 0),
                },
                user_id,
            )
        except Exception:
            logger.exception("Unable to record sync stall audit event")


def record_interrupted_sync_runs(signal_name: str = "SIGTERM") -> int:
    now = utcnow()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, initiated_by_user_id, current_step, summary,
                       customers_synced, invoices_synced, contacts_total, invoices_total,
                       fetched_count, processed_count, heartbeat_at, started_at, created_at
                FROM sync_runs
                WHERE provider = %s
                  AND status IN ('queued', 'running')
                """,
                ("xero",),
            )
            interrupted_rows = cursor.fetchall()

            if interrupted_rows:
                cursor.execute(
                    """
                    UPDATE sync_runs
                    SET status = %s,
                        current_step = %s,
                        summary = %s,
                        error_message = %s,
                        failed_count = GREATEST(failed_count, 1),
                        completed_at = %s
                    WHERE id = ANY(%s)
                    """,
                    (
                        "failed",
                        "Sync interrupted",
                        "Xero sync interrupted before it could complete.",
                        f"Process received {signal_name} while a Xero sync was active.",
                        now,
                        [row["id"] for row in interrupted_rows],
                    ),
                )
        connection.commit()

    for row in interrupted_rows or []:
        user_id = row.get("initiated_by_user_id")
        last_heartbeat = row.get("heartbeat_at") or row.get("started_at") or row.get("created_at")
        elapsed_seconds = None
        if last_heartbeat is not None:
            elapsed_seconds = max(0, int((now - last_heartbeat).total_seconds()))
        try:
            record_audit_event(
                "sync_run",
                str(row["id"]),
                "sync.interrupted",
                {
                    "message": f"Sync interrupted after process received {signal_name}.",
                    "signal": signal_name,
                    "last_step": row.get("current_step") or "",
                    "last_summary": row.get("summary") or "",
                    "last_heartbeat_at": _iso(last_heartbeat),
                    "elapsed_seconds_since_heartbeat": elapsed_seconds,
                    "customers_synced": int(row.get("customers_synced") or 0),
                    "invoices_synced": int(row.get("invoices_synced") or 0),
                    "contacts_total": int(row.get("contacts_total") or 0),
                    "invoices_total": int(row.get("invoices_total") or 0),
                    "fetched_count": int(row.get("fetched_count") or 0),
                    "processed_count": int(row.get("processed_count") or 0),
                },
                str(user_id) if user_id else None,
            )
        except Exception:
            logger.exception("Unable to record sync interruption audit event")

    return len(interrupted_rows or [])


def _sigterm_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except Exception:
        return f"signal {signum}"


def _handle_sync_sigterm(signum, frame) -> None:
    signal_name = _sigterm_name(signum)
    try:
        interrupted_count = record_interrupted_sync_runs(signal_name)
        if interrupted_count:
            logger.warning("Marked %s active sync run(s) interrupted after %s", interrupted_count, signal_name)
    except Exception:
        logger.exception("Unable to mark active sync runs interrupted after %s", signal_name)

    previous_handler = _PREVIOUS_SIGTERM_HANDLER
    if callable(previous_handler) and previous_handler is not _handle_sync_sigterm:
        previous_handler(signum, frame)
        return
    if previous_handler == signal.SIG_IGN:
        return
    raise SystemExit(128 + int(signum))


def install_sync_signal_handlers() -> None:
    global _PREVIOUS_SIGTERM_HANDLER, _SYNC_SIGNAL_HANDLERS_INSTALLED
    if _SYNC_SIGNAL_HANDLERS_INSTALLED or not hasattr(signal, "SIGTERM"):
        return
    try:
        _PREVIOUS_SIGTERM_HANDLER = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _handle_sync_sigterm)
        _SYNC_SIGNAL_HANDLERS_INSTALLED = True
    except ValueError:
        logger.warning("Unable to install sync SIGTERM handler outside the main thread")


def _update_sync_run(sync_run_id: str, **fields) -> dict | None:
    if not fields:
        return None
    fields.setdefault("heartbeat_at", utcnow())

    assignments = ", ".join(f"{field} = %s" for field in fields)
    values = [*fields.values(), sync_run_id]
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE sync_runs
                SET {assignments}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def request_sync_run(user: dict, sync_options: dict | None = None) -> tuple[dict, bool]:
    sync_options = normalise_sync_options(sync_options)
    get_xero_connection_for_user(user["id"])
    _mark_stale_sync_runs(user["id"])
    rate_limit = _active_xero_rate_limit(user["id"])
    if rate_limit is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": rate_limit["message"],
                "retry_after_seconds": rate_limit["retry_after_seconds"],
                "rate_limit_until": _iso(rate_limit["rate_limit_until"]),
                "previous_error": rate_limit["previous_error"],
            },
        )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM sync_runs
                WHERE provider = %s
                  AND initiated_by_user_id = %s
                  AND status IN ('queued', 'running')
                  AND created_at > NOW() - INTERVAL '2 hours'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                ("xero", user["id"]),
            )
            active = cursor.fetchone()
            if active is not None:
                connection.commit()
                return active, False

            cursor.execute(
                """
                INSERT INTO sync_runs (
                    provider,
                    initiated_by_user_id,
                    status,
                    current_step,
                    summary,
                    customers_synced,
                    invoices_synced,
                    fetched_count,
                    processed_count,
                    failed_count,
                    contacts_total,
                    invoices_total,
                    heartbeat_at,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    "xero",
                    user["id"],
                    "queued",
                    "Queued",
                    f"Xero sync queued. {sync_options['summary']}",
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    utcnow(),
                    utcnow(),
                ),
            )
            sync_run = cursor.fetchone()
        connection.commit()

    return sync_run, True


def get_sync_run(user: dict, sync_run_id: str) -> dict:
    _mark_stale_sync_runs(user["id"])
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM sync_runs
                WHERE id = %s
                  AND initiated_by_user_id = %s
                  AND provider = %s
                """,
                (sync_run_id, user["id"], "xero"),
            )
            row = cursor.fetchone()
        connection.commit()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sync run not found.")
    return row


def serialize_sync_run(sync_run: dict) -> dict:
    progress = 0
    if sync_run.get("status") == "completed":
        progress = 100
    elif sync_run.get("status") == "failed":
        progress = 100
    elif sync_run.get("status") == "queued":
        progress = 4
    else:
        contacts_total = int(sync_run.get("contacts_total") or 0)
        invoices_total = int(sync_run.get("invoices_total") or 0)
        customers_synced = int(sync_run.get("customers_synced") or 0)
        invoices_synced = int(sync_run.get("invoices_synced") or 0)
        if contacts_total or invoices_total:
            total = max(contacts_total + invoices_total, 1)
            progress = min(95, max(8, round(((customers_synced + invoices_synced) / total) * 100)))
        else:
            progress = 12

    return {
        "id": str(sync_run["id"]),
        "status": sync_run.get("status") or "",
        "currentStep": sync_run.get("current_step") or "",
        "summary": sync_run.get("summary") or "",
        "errorMessage": sync_run.get("error_message") or "",
        "customersSynced": int(sync_run.get("customers_synced") or 0),
        "invoicesSynced": int(sync_run.get("invoices_synced") or 0),
        "contactsTotal": int(sync_run.get("contacts_total") or 0),
        "invoicesTotal": int(sync_run.get("invoices_total") or 0),
        "progress": progress,
        "createdAt": _iso(sync_run.get("created_at")) or "",
        "startedAt": _iso(sync_run.get("started_at")) or "",
        "heartbeatAt": _iso(sync_run.get("heartbeat_at")) or "",
        "completedAt": _iso(sync_run.get("completed_at")) or "",
        "rateLimitUntil": _iso(sync_run.get("rate_limit_until")) or "",
        "retryAfterSeconds": int(sync_run.get("retry_after_seconds") or 0),
        "isActive": sync_run.get("status") in ACTIVE_SYNC_STATUSES,
    }


OPERATION_LABELS = {
    "late_payment_charges": "Late payment charges",
    "bad_debt_write_offs": "Bad debt write-offs",
}


def _operation_payload(invoice_ids: list[str], options: dict | None = None) -> dict:
    return {
        "invoiceIds": [str(invoice_id) for invoice_id in invoice_ids],
        "options": options or {},
    }


def request_operation_run(user: dict, operation_type: str, invoice_ids: list[str], options: dict | None = None) -> dict:
    if operation_type not in OPERATION_LABELS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported operation type.")
    clean_invoice_ids = [str(invoice_id).strip() for invoice_id in invoice_ids if str(invoice_id).strip()]
    if not clean_invoice_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one invoice.")

    label = OPERATION_LABELS[operation_type]
    now = utcnow()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO operation_runs (
                    operation_type, initiated_by_user_id, status, total_count,
                    processed_count, succeeded_count, failed_count, current_step,
                    summary, payload, created_at, heartbeat_at
                )
                VALUES (%s, %s, %s, %s, 0, 0, 0, %s, %s, %s::jsonb, %s, %s)
                RETURNING *
                """,
                (
                    operation_type,
                    user["id"],
                    "queued",
                    len(clean_invoice_ids),
                    "Queued",
                    f"{label} queued for {len(clean_invoice_ids)} invoice{'s' if len(clean_invoice_ids) != 1 else ''}.",
                    json.dumps(_operation_payload(clean_invoice_ids, options), default=_json_default),
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def get_operation_run(user: dict, operation_run_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM operation_runs
                WHERE id = %s
                  AND initiated_by_user_id = %s
                """,
                (operation_run_id, user["id"]),
            )
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation run not found.")
    return row


def serialize_operation_run(operation_run: dict) -> dict:
    total = int(operation_run.get("total_count") or 0)
    processed = int(operation_run.get("processed_count") or 0)
    if operation_run.get("status") in ("completed", "failed"):
        progress = 100
    elif operation_run.get("status") == "queued":
        progress = 4
    elif total:
        progress = min(98, max(8, round((processed / total) * 100)))
    else:
        progress = 12
    return {
        "id": str(operation_run["id"]),
        "operationType": operation_run.get("operation_type") or "",
        "label": OPERATION_LABELS.get(operation_run.get("operation_type"), "Xero operation"),
        "status": operation_run.get("status") or "",
        "currentStep": operation_run.get("current_step") or "",
        "summary": operation_run.get("summary") or "",
        "errorMessage": operation_run.get("error_message") or "",
        "totalCount": total,
        "processedCount": processed,
        "succeededCount": int(operation_run.get("succeeded_count") or 0),
        "failedCount": int(operation_run.get("failed_count") or 0),
        "progress": progress,
        "result": operation_run.get("result") or {},
        "createdAt": _iso(operation_run.get("created_at")) or "",
        "startedAt": _iso(operation_run.get("started_at")) or "",
        "heartbeatAt": _iso(operation_run.get("heartbeat_at")) or "",
        "completedAt": _iso(operation_run.get("completed_at")) or "",
        "isActive": operation_run.get("status") in ("queued", "running"),
    }


def _update_operation_run(operation_run_id: str, **fields) -> dict:
    if not fields:
        raise ValueError("No operation fields supplied.")
    fields.setdefault("heartbeat_at", utcnow())
    assignments = ", ".join(f"{key} = %s::jsonb" if key in {"payload", "result"} else f"{key} = %s" for key in fields)
    values = list(fields.values())
    values.append(operation_run_id)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE operation_runs
                SET {assignments}
                WHERE id = %s
                RETURNING *
                """,
                values,
            )
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation run not found.")
    return row


def _invoice_operation_label(invoice_id: str) -> str:
    try:
        invoice_uuid = UUID(str(invoice_id))
    except ValueError:
        return str(invoice_id)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT invoices.invoice_number, customers.name AS customer_name
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE invoices.id = %s
                """,
                (invoice_uuid,),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        return str(invoice_id)
    invoice_number = row.get("invoice_number") or str(invoice_id)
    customer_name = row.get("customer_name") or "client"
    return f"{customer_name} · {invoice_number}"


async def run_invoice_operation_job(
    user: dict,
    operation_run_id: str,
    operation_type: str,
    invoice_ids: list[str],
    options: dict | None = None,
) -> None:
    label = OPERATION_LABELS.get(operation_type, "Xero operation")
    created: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    processed_count = 0
    succeeded_count = 0
    failed_count = 0
    clean_invoice_ids = [str(invoice_id).strip() for invoice_id in invoice_ids if str(invoice_id).strip()]
    _update_operation_run(
        operation_run_id,
        status="running",
        started_at=utcnow(),
        current_step=f"Starting {label.lower()}",
        summary=f"Starting {label.lower()} for {len(clean_invoice_ids)} invoice{'s' if len(clean_invoice_ids) != 1 else ''}.",
    )
    charge_selections = (options or {}).get("chargeSelections") or []
    charge_selection_by_id = {
        str(item.get("invoiceId") or "").strip(): item
        for item in charge_selections
        if str(item.get("invoiceId") or "").strip()
    }

    try:
        for invoice_id in clean_invoice_ids:
            invoice_label = _invoice_operation_label(invoice_id)
            action_text = "Raising late payment charge" if operation_type == "late_payment_charges" else "Raising and allocating bad debt credit note"
            _update_operation_run(
                operation_run_id,
                current_step=f"{action_text}: {invoice_label}",
                summary=f"Processed {processed_count} of {len(clean_invoice_ids)}. Working on {invoice_label}.",
            )
            try:
                if operation_type == "late_payment_charges":
                    selection = charge_selection_by_id.get(invoice_id) or {"invoiceId": invoice_id}
                    result = await create_late_payment_charges(user, [invoice_id], [selection])
                elif operation_type == "bad_debt_write_offs":
                    result = await create_bad_debt_write_offs(user, [invoice_id])
                else:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported operation type.")
                created_items = result.get("created") or []
                skipped_items = result.get("skipped") or []
                created.extend(created_items)
                skipped.extend(skipped_items)
                if created_items:
                    succeeded_count += len(created_items)
                if skipped_items:
                    failed_count += len(skipped_items)
            except Exception as exc:
                error = _sync_error_message(exc)
                failed_count += 1
                failed.append({"invoiceId": invoice_id, "reason": error})
                logger.exception("Unable to run %s for invoice %s", operation_type, invoice_id)

            processed_count += 1
            _update_operation_run(
                operation_run_id,
                processed_count=processed_count,
                succeeded_count=succeeded_count,
                failed_count=failed_count,
                current_step=f"{label}: {processed_count} of {len(clean_invoice_ids)} processed",
                summary=(
                    f"Processed {processed_count} of {len(clean_invoice_ids)} invoice"
                    f"{'' if len(clean_invoice_ids) == 1 else 's'}; "
                    f"{succeeded_count} succeeded, {failed_count} skipped or failed."
                ),
            )

        result_payload = {"created": created, "skipped": [*skipped, *failed]}
        _update_operation_run(
            operation_run_id,
            status="completed",
            completed_at=utcnow(),
            current_step=f"{label} complete",
            summary=(
                f"{label} complete: {succeeded_count} succeeded, "
                f"{failed_count} skipped or failed."
            ),
            result=json.dumps(result_payload, default=_json_default),
        )
    except Exception as exc:
        _update_operation_run(
            operation_run_id,
            status="failed",
            completed_at=utcnow(),
            current_step=f"{label} failed",
            summary=f"{label} stopped before completion.",
            error_message=_sync_error_message(exc),
            result=json.dumps({"created": created, "skipped": [*skipped, *failed]}, default=_json_default),
        )
        logger.exception("Operation run %s failed", operation_run_id)


def sync_run_has_working_data(sync_run: dict) -> bool:
    customers_synced = int(sync_run.get("customers_synced") or 0)
    invoices_synced = int(sync_run.get("invoices_synced") or 0)
    processed_count = int(sync_run.get("processed_count") or 0)
    imported_invoice_rows_are_ready = (
        invoices_synced > 0
        and processed_count >= customers_synced + invoices_synced
    )
    return (
        sync_run.get("status") in ACTIVE_SYNC_STATUSES
        and invoices_synced > 0
        and (
            sync_run.get("current_step") in WORKING_DATA_STEPS
            or imported_invoice_rows_are_ready
        )
    )


def active_sync_run_for_user(user: dict | None) -> dict | None:
    if not user or not user.get("id"):
        return None

    _mark_stale_sync_runs(user["id"])
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM sync_runs
                WHERE provider = %s
                  AND initiated_by_user_id = %s
                  AND status IN ('queued', 'running')
                ORDER BY COALESCE(heartbeat_at, started_at, created_at) DESC, created_at DESC
                LIMIT 1
                """,
                ("xero", user["id"]),
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def list_developer_logs(user: dict, limit: int = 120) -> list[dict]:
    bounded_limit = max(1, min(int(limit or 120), 300))
    logs: list[dict] = []
    try:
        logs.extend(_list_audit_developer_logs(user, bounded_limit))
    except Exception as exc:
        logger.exception("Unable to load audit developer logs")
        logs.append(_developer_log_error_entry("developer.log.query.failed", exc))
    try:
        logs.extend(_list_sync_run_developer_logs(user, bounded_limit))
    except Exception as exc:
        logger.exception("Unable to load sync run developer logs")
        logs.append(_developer_log_error_entry("developer.log.sync_runs.failed", exc))
    try:
        logs.extend(_list_ignition_sync_run_developer_logs(user, bounded_limit))
    except Exception as exc:
        logger.exception("Unable to load Ignition sync run developer logs")
        logs.append(_developer_log_error_entry("developer.log.ignition_sync_runs.failed", exc))

    logs.sort(key=lambda entry: entry.get("createdAt") or "", reverse=True)
    return logs[:bounded_limit]


def _list_audit_developer_logs(user: dict, limit: int) -> list[dict]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT audit_events.*
                FROM audit_events
                WHERE (audit_events.user_id = %s OR audit_events.user_id IS NULL)
                  AND (
                      audit_events.entity_type = 'sync_run'
                      OR audit_events.entity_type = 'ignition_sync_run'
                      OR audit_events.entity_type = 'xero_connection'
                      OR audit_events.event_type LIKE 'sync.%%'
                      OR audit_events.event_type LIKE 'xero.%%'
                      OR audit_events.event_type LIKE 'ignition.%%'
                  )
                ORDER BY audit_events.created_at DESC
                LIMIT %s
                """,
                (user["id"], limit),
            )
            rows = cursor.fetchall()
        connection.commit()

    return [
        {
            "id": str(row["id"]),
            "level": "error" if str(row.get("event_type") or "").endswith("failed") else "info",
            "source": "audit",
            "eventType": row.get("event_type") or "",
            "message": _developer_log_message(row),
            "payload": _safe_json(row.get("payload") or {}),
            "createdAt": _iso(row.get("created_at")) or "",
            "syncRunId": row.get("entity_id") if row.get("entity_type") in ("sync_run", "ignition_sync_run") else "",
            "syncStatus": "",
        }
        for row in rows
    ]


def _list_sync_run_developer_logs(user: dict, limit: int) -> list[dict]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM sync_runs
                WHERE provider = %s
                  AND initiated_by_user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                ("xero", user["id"], limit),
            )
            rows = cursor.fetchall()
        connection.commit()

    logs = []
    for row in rows:
        status_value = row.get("status") or ""
        logs.append(
            {
                "id": str(row["id"]),
                "level": "error" if status_value == "failed" else "info",
                "source": "sync_runs",
                "eventType": f"sync.{status_value or 'unknown'}",
                "message": row.get("error_message") or row.get("summary") or row.get("current_step") or "Sync run",
                "payload": _safe_json(serialize_sync_run(row)),
                "createdAt": _iso(row.get("created_at")) or "",
                "syncRunId": str(row["id"]),
                "syncStatus": status_value,
            }
        )
    return logs


def _list_ignition_sync_run_developer_logs(user: dict, limit: int) -> list[dict]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM ignition_sync_runs
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user["id"], limit),
            )
            rows = cursor.fetchall()
        connection.commit()

    logs = []
    for row in rows:
        status_value = row.get("status") or ""
        logs.append(
            {
                "id": str(row["id"]),
                "level": "error" if status_value == "failed" else "info",
                "source": "ignition_sync_runs",
                "eventType": f"ignition.sync.{status_value or 'unknown'}",
                "message": row.get("error_message") or row.get("summary") or row.get("current_step") or "Ignition sync run",
                "payload": _safe_json(serialize_ignition_sync_run(row)),
                "createdAt": _iso(row.get("created_at")) or "",
                "syncRunId": str(row["id"]),
                "syncStatus": status_value,
            }
        )
    return logs


def _developer_log_error_entry(event_type: str, exc: Exception) -> dict:
    return {
        "id": event_type,
        "level": "error",
        "source": "backend",
        "eventType": event_type,
        "message": str(exc) or exc.__class__.__name__,
        "payload": {"type": exc.__class__.__name__},
        "createdAt": utcnow().isoformat(),
        "syncRunId": "",
        "syncStatus": "",
    }


def _developer_log_message(row: dict) -> str:
    payload = row.get("payload") or {}
    if isinstance(payload, dict):
        return str(
            payload.get("summary")
            or payload.get("error")
            or payload.get("message")
            or row.get("event_type")
            or "Log event"
        )
    return str(payload or row.get("event_type") or "Log event")


def run_sync_job(user: dict, sync_run_id: str, sync_options: dict | None = None) -> None:
    try:
        asyncio.run(run_sync(user, sync_run_id, sync_options))
    except Exception as exc:
        logger.exception("Background Xero sync failed")
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT status FROM sync_runs WHERE id = %s", (sync_run_id,))
                row = cursor.fetchone()
            connection.commit()
        if row and row.get("status") in ACTIVE_SYNC_STATUSES:
            message = _sync_error_message(exc)
            _update_sync_run(
                sync_run_id,
                status="failed",
                current_step="Sync failed",
                summary="Xero sync failed before it could complete.",
                error_message=message,
                failed_count=1,
                completed_at=utcnow(),
            )


def _contacts_from_invoices(invoices: list[dict]) -> list[dict]:
    contacts_by_id: dict[str, dict] = {}
    for invoice in invoices:
        contact = invoice.get("Contact") or {}
        contact_id = contact.get("ContactID")
        if not contact_id:
            continue
        existing = contacts_by_id.setdefault(contact_id, {"ContactID": contact_id})
        for key, value in contact.items():
            if value not in (None, "", [], {}) and not existing.get(key):
                existing[key] = value
    return list(contacts_by_id.values())


def _contact_receivable_amount(contact: dict, key: str) -> Decimal:
    receivable = ((contact.get("Balances") or {}).get("AccountsReceivable") or {})
    try:
        return Decimal(str(receivable.get(key) if receivable.get(key) is not None else 0))
    except Exception:
        return Decimal("0")


def _is_customer_contact(contact: dict, invoice_contact_ids: set[str]) -> bool:
    contact_id = contact.get("ContactID")
    if contact_id in invoice_contact_ids:
        return True
    if contact.get("IsCustomer") is True:
        return True
    return (
        _contact_receivable_amount(contact, "Outstanding") != 0
        or _contact_receivable_amount(contact, "Overdue") != 0
    )


def _customer_contacts_from_xero(raw_contacts: list[dict], invoices: list[dict]) -> list[dict]:
    invoice_contacts = _contacts_from_invoices(invoices)
    invoice_contact_ids = {
        contact.get("ContactID")
        for contact in invoice_contacts
        if contact.get("ContactID")
    }
    contacts_by_id = {
        contact["ContactID"]: contact
        for contact in raw_contacts
        if contact.get("ContactID") and _is_customer_contact(contact, invoice_contact_ids)
    }
    for contact in invoice_contacts:
        contact_id = contact.get("ContactID")
        if contact_id:
            contacts_by_id.setdefault(contact_id, contact)
    return list(contacts_by_id.values())


def _upsert_xero_customer(cursor, raw_contact: dict, tenant_id: str, synced_at: datetime) -> None:
    contact = normalise_contact(raw_contact, tenant_id)
    cursor.execute(
        """
        INSERT INTO customers (
            tenant_id, xero_contact_id, name, email, phone, account_number,
            primary_person, contact_people, addresses, total_due, overdue_amount, last_sync_at, updated_at
        )
        VALUES (
            %(tenant_id)s, %(xero_contact_id)s, %(name)s, %(email)s, %(phone)s, %(account_number)s,
            %(primary_person)s, %(contact_people_json)s::jsonb, %(addresses_json)s::jsonb, %(total_due)s, %(overdue_amount)s, %(last_sync_at)s, %(updated_at)s
        )
        ON CONFLICT (xero_contact_id) DO UPDATE
        SET tenant_id = EXCLUDED.tenant_id,
            name = EXCLUDED.name,
            email = EXCLUDED.email,
            phone = EXCLUDED.phone,
            account_number = EXCLUDED.account_number,
            primary_person = EXCLUDED.primary_person,
            contact_people = EXCLUDED.contact_people,
            addresses = EXCLUDED.addresses,
            total_due = EXCLUDED.total_due,
            overdue_amount = EXCLUDED.overdue_amount,
            last_sync_at = EXCLUDED.last_sync_at,
            updated_at = EXCLUDED.updated_at
        """,
        {
            **contact,
            "contact_people_json": json.dumps(contact.get("contact_people") or [], default=_json_default),
            "addresses_json": json.dumps(contact.get("addresses") or [], default=_json_default),
            "last_sync_at": synced_at,
            "updated_at": synced_at,
        },
    )


def _make_xero_request_tracer(sync_run_id: str, user_id: str, label: str):
    def trace(info: dict) -> None:
        try:
            payload = {"label": label, **(info or {})}
            outcome = str(payload.get("outcome") or "ok")
            event_type = {
                "ok": "sync.xero_request.ok",
                "timeout": "sync.xero_request.timeout",
                "rate_limited": "sync.xero_request.rate_limited",
                "error": "sync.xero_request.error",
            }.get(outcome, "sync.xero_request")
            record_audit_event(
                "sync_run",
                str(sync_run_id),
                event_type,
                payload,
                user_id,
            )
        except Exception:
            logger.exception("Unable to record Xero request audit event")
    return trace


async def run_sync(user: dict, sync_run_id: str, sync_options: dict | None = None) -> dict:
    sync_options = normalise_sync_options(sync_options)
    connection_row = get_xero_connection_for_user(user["id"])
    sync_signature = _sync_options_signature(sync_options)
    now = utcnow()
    candidate_modified_since = _incremental_modified_since(user["id"])
    scope_already_imported = (
        _completed_sync_covers_scope(user["id"], sync_options["invoice_scope"])
        if candidate_modified_since is not None
        else False
    )
    years_are_already_imported = (
        True
        if not sync_options["invoice_years"] and scope_already_imported
        else (
            _local_invoice_years_cover(connection_row["tenant_id"], sync_options["invoice_years"])
            if candidate_modified_since is not None
            else False
        )
    )
    modified_since = candidate_modified_since if scope_already_imported and years_are_already_imported else None
    is_incremental_sync = modified_since is not None
    needs_paid_backfill = sync_options["paid_page_limit"] != 0 and not scope_already_imported
    resume_state = None if is_incremental_sync else _latest_resumable_sync_state(user["id"], connection_row["tenant_id"], sync_signature)
    resume_checkpoints = (resume_state or {}).get("checkpoints") or {}
    resume_source_run = (resume_state or {}).get("sync_run") or {}
    resume_started_at = resume_source_run.get("started_at") or resume_source_run.get("created_at")
    contact_fetch_label = "changed customer records" if is_incremental_sync else "customer records"
    invoice_fetch_label = "changed invoices" if is_incremental_sync else "outstanding invoices"
    contact_fetch_step = f"Fetching {contact_fetch_label} from Xero"
    invoice_fetch_step = f"Fetching {invoice_fetch_label} from Xero"
    invoice_where = _with_invoice_year_filter(
        ACCREC_INVOICE_WHERE if is_incremental_sync else OUTSTANDING_INVOICE_WHERE,
        sync_options["invoice_years"],
    )
    if is_incremental_sync:
        sync_mode_summary = f"Incremental sync from {modified_since.isoformat()}."
    elif candidate_modified_since is not None:
        sync_mode_summary = "Full sync for a newly selected import scope or invoice year range."
    else:
        sync_mode_summary = "First full sync for the selected scope."
    if resume_state is not None:
        sync_mode_summary = f"Resuming interrupted sync {resume_source_run.get('id')}. {sync_mode_summary}"
    _update_sync_run(
        sync_run_id,
        status="running",
        current_step="Starting Xero sync",
        summary=f"Connecting to Xero. {sync_mode_summary}",
        started_at=resume_started_at or now,
        error_message=None,
    )
    if resume_state is not None:
        record_audit_event(
            "sync_run",
            str(sync_run_id),
            "sync.resumed",
            {
                "source_sync_run_id": str(resume_source_run.get("id") or ""),
                "source_started_at": _iso(resume_started_at),
                "phases": sorted(resume_checkpoints.keys()),
            },
            user["id"],
        )

    try:
        def rate_limit_progress(label: str):
            def progress(page: int, total_records: int, delay_seconds: int, retry_number: int) -> None:
                _update_sync_run(
                    sync_run_id,
                    current_step="Waiting for Xero rate limit",
                    summary=(
                        f"Xero rate-limited {label} page {page}. Retrying in {delay_seconds} seconds "
                        f"({retry_number} of {XERO_RATE_LIMIT_RETRIES}). Fetched {total_records} records so far."
                    ),
                )

            return progress

        def contact_progress(_, total_records: int, __) -> None:
            _update_sync_run(
                sync_run_id,
                current_step=contact_fetch_step,
                summary=f"Fetched {total_records} {contact_fetch_label} from Xero.",
                contacts_total=total_records,
            )

        def outstanding_invoice_progress(_, total_records: int, __) -> None:
            _update_sync_run(
                sync_run_id,
                current_step=invoice_fetch_step,
                summary=f"Fetched {total_records} {invoice_fetch_label} from Xero.",
                invoices_synced=total_records,
                invoices_total=total_records,
            )

        def paid_invoice_progress(_, total_records: int, __) -> None:
            _update_sync_run(
                sync_run_id,
                current_step="Fetching paid invoices from Xero",
                summary=f"Fetched {total_records} paid invoices from Xero.",
                invoices_total=len(outstanding_invoices) + total_records,
            )

        def payment_progress(_, total_records: int, __) -> None:
            _update_sync_run(
                sync_run_id,
                current_step="Fetching payments from Xero",
                summary=f"Fetched {total_records} Xero payments.",
            )

        def payment_store_progress(processed_records: int, total_records: int, stored_records: int) -> None:
            _update_sync_run(
                sync_run_id,
                current_step="Importing payments from Xero",
                summary=(
                    f"Processed {processed_records} Xero payments so far; "
                    f"stored {stored_records} matching customer payments."
                ),
                fetched_count=len(contacts) + len(outstanding_invoices) + total_records,
                processed_count=len(contacts) + synced_invoices + processed_records,
            )

        def credit_note_progress(_, total_records: int, __) -> None:
            _update_sync_run(
                sync_run_id,
                current_step="Fetching credit notes from Xero",
                summary=f"Fetched {total_records} Xero credit notes.",
            )

        def overpayment_progress(_, total_records: int, __) -> None:
            _update_sync_run(
                sync_run_id,
                current_step="Fetching overpayments from Xero",
                summary=f"Fetched {total_records} Xero overpayments.",
            )

        async def sync_payments_step() -> int:
            payment_checkpoint = resume_checkpoints.get(SYNC_PHASE_PAYMENTS)
            if _checkpoint_completed(resume_checkpoints, SYNC_PHASE_PAYMENTS):
                payments_synced = int(payment_checkpoint.get("records_stored") or 0)
                _upsert_sync_checkpoint(
                    sync_run_id,
                    user["id"],
                    connection_row["tenant_id"],
                    sync_signature,
                    SYNC_PHASE_PAYMENTS,
                    "completed",
                    page_number=int(payment_checkpoint.get("page_number") or 0),
                    records_seen=int(payment_checkpoint.get("records_seen") or 0),
                    records_stored=payments_synced,
                    payload={**(payment_checkpoint.get("payload") or {}), "resumed_from": str(resume_source_run.get("id") or "")},
                )
                _update_sync_run(
                    sync_run_id,
                    current_step="Payments imported from Xero",
                    summary=f"Reused {payments_synced} Xero payments from the interrupted sync. Checking customer credits.",
                )
                return payments_synced

            start_page = int((payment_checkpoint or {}).get("page_number") or 0) + 1
            already_processed = int((payment_checkpoint or {}).get("records_seen") or 0)
            already_synced = int((payment_checkpoint or {}).get("records_stored") or 0)
            _update_sync_run(
                sync_run_id,
                current_step="Fetching payments from Xero",
                summary=(
                    f"Resuming payments from Xero page {start_page}."
                    if payment_checkpoint
                    else "Pulling payments made against Xero invoices."
                ),
            )
            _upsert_sync_checkpoint(
                sync_run_id,
                user["id"],
                connection_row["tenant_id"],
                sync_signature,
                SYNC_PHASE_PAYMENTS,
                "running",
                page_number=max(start_page - 1, 0),
                records_seen=already_processed,
                records_stored=already_synced,
                payload={"resumed_from": str(resume_source_run.get("id") or "")} if payment_checkpoint else {},
            )
            try:
                last_payment_page = max(start_page - 1, int((payment_checkpoint or {}).get("page_number") or 0))
                last_payment_seen = already_processed

                def payment_checkpoint_progress(page_number: int, processed_records: int, stored_records: int) -> None:
                    nonlocal last_payment_page, last_payment_seen
                    last_payment_page = page_number
                    last_payment_seen = processed_records
                    _upsert_sync_checkpoint(
                        sync_run_id,
                        user["id"],
                        connection_row["tenant_id"],
                        sync_signature,
                        SYNC_PHASE_PAYMENTS,
                        "running",
                        page_number=page_number,
                        records_seen=processed_records,
                        records_stored=stored_records,
                        payload={"resumed_from": str(resume_source_run.get("id") or "")} if payment_checkpoint else {},
                    )

                payments_synced = await _sync_xero_payments(
                    connection_row,
                    utcnow(),
                    modified_since=modified_since if is_incremental_sync else None,
                    start_page=start_page,
                    already_processed=already_processed,
                    already_synced=already_synced,
                    on_page=payment_progress,
                    on_retry=rate_limit_progress("payments"),
                    on_store=payment_store_progress,
                    on_checkpoint=payment_checkpoint_progress,
                    on_request=_make_xero_request_tracer(sync_run_id, user["id"], "payments"),
                )
                _upsert_sync_checkpoint(
                    sync_run_id,
                    user["id"],
                    connection_row["tenant_id"],
                    sync_signature,
                    SYNC_PHASE_PAYMENTS,
                    "completed",
                    page_number=last_payment_page,
                    records_seen=last_payment_seen,
                    records_stored=payments_synced,
                    payload={"resumed_from": str(resume_source_run.get("id") or "")} if payment_checkpoint else {},
                )
                _update_sync_run(
                    sync_run_id,
                    current_step="Payments imported from Xero",
                    summary=f"Stored {payments_synced} Xero payments. Checking customer credits.",
                )
                return payments_synced
            except Exception as exc:
                logger.exception("Unable to sync Xero payments")
                record_audit_event(
                    "sync_run",
                    str(sync_run_id),
                    "sync.payments.failed",
                    {"error": _sync_error_message(exc), "detail": _sync_error_payload(exc)},
                    user["id"],
                )
                return 0

        async def sync_credit_sources_step() -> int:
            credit_checkpoint = resume_checkpoints.get(SYNC_PHASE_CREDITS)
            if _checkpoint_completed(resume_checkpoints, SYNC_PHASE_CREDITS):
                credit_sources_synced = int(credit_checkpoint.get("records_stored") or 0)
                _upsert_sync_checkpoint(
                    sync_run_id,
                    user["id"],
                    connection_row["tenant_id"],
                    sync_signature,
                    SYNC_PHASE_CREDITS,
                    "completed",
                    records_seen=int(credit_checkpoint.get("records_seen") or 0),
                    records_stored=credit_sources_synced,
                    payload={**(credit_checkpoint.get("payload") or {}), "resumed_from": str(resume_source_run.get("id") or "")},
                )
                _update_sync_run(
                    sync_run_id,
                    current_step="Customer credits imported from Xero",
                    summary=f"Reused {credit_sources_synced} customer credits from the interrupted sync.",
                )
                return credit_sources_synced

            _update_sync_run(
                sync_run_id,
                current_step="Fetching customer credits from Xero",
                summary="Pulling credit notes and overpayments that reduce debtor balances.",
            )
            _upsert_sync_checkpoint(
                sync_run_id,
                user["id"],
                connection_row["tenant_id"],
                sync_signature,
                SYNC_PHASE_CREDITS,
                "running",
            )
            credit_sources_synced = await _sync_xero_customer_credits(
                connection_row,
                utcnow(),
                modified_since=modified_since if is_incremental_sync else None,
                on_credit_note_page=credit_note_progress,
                on_credit_note_retry=rate_limit_progress("credit notes"),
                on_overpayment_page=overpayment_progress,
                on_overpayment_retry=rate_limit_progress("overpayments"),
                on_request=_make_xero_request_tracer(sync_run_id, user["id"], "credit_sources"),
            )
            _upsert_sync_checkpoint(
                sync_run_id,
                user["id"],
                connection_row["tenant_id"],
                sync_signature,
                SYNC_PHASE_CREDITS,
                "completed",
                records_seen=credit_sources_synced,
                records_stored=credit_sources_synced,
            )
            return credit_sources_synced

        outstanding_checkpoint = resume_checkpoints.get(SYNC_PHASE_OUTSTANDING)
        if _checkpoint_completed(resume_checkpoints, SYNC_PHASE_OUTSTANDING):
            local_counts = _local_resume_counts(connection_row["tenant_id"], sync_options["invoice_years"])
            imported_contacts = _checkpoint_payload_count(outstanding_checkpoint, "contacts_count", local_counts["customers"])
            outstanding_synced = _checkpoint_payload_count(outstanding_checkpoint, "outstanding_count", local_counts["outstanding_invoices"])
            synced_invoices = _checkpoint_payload_count(outstanding_checkpoint, "synced_invoices", outstanding_synced)
            contacts = [{}] * imported_contacts
            outstanding_invoices = [{}] * outstanding_synced
            ready_step = "Finalising incremental sync" if is_incremental_sync and not needs_paid_backfill else OUTSTANDING_READY_STEP
            ready_summary = (
                f"Reused {imported_contacts} customers and {outstanding_synced} outstanding invoices from interrupted sync."
            )
            outstanding_ready = _update_sync_run(
                sync_run_id,
                status="running",
                current_step=ready_step,
                customers_synced=imported_contacts,
                invoices_synced=synced_invoices,
                fetched_count=imported_contacts + outstanding_synced,
                processed_count=imported_contacts + synced_invoices,
                failed_count=0,
                contacts_total=imported_contacts,
                invoices_total=outstanding_synced,
                summary=ready_summary,
                completed_at=None,
            )
            _upsert_sync_checkpoint(
                sync_run_id,
                user["id"],
                connection_row["tenant_id"],
                sync_signature,
                SYNC_PHASE_OUTSTANDING,
                "completed",
                records_seen=imported_contacts + outstanding_synced,
                records_stored=imported_contacts + outstanding_synced,
                payload={
                    "contacts_count": imported_contacts,
                    "outstanding_count": outstanding_synced,
                    "synced_invoices": synced_invoices,
                    "invoice_years": sync_options["invoice_years"],
                    "resumed_from": str(resume_source_run.get("id") or ""),
                },
            )
        else:
            _update_sync_run(
                sync_run_id,
                current_step=contact_fetch_step,
                summary=f"Fetching {contact_fetch_label} from Xero.",
            )
            raw_contacts: list[dict] = []
            contact_fetch_summary = ""
            try:
                raw_contacts = await fetch_paginated_collection(
                    connection_row,
                    CONTACTS_URL,
                    "Contacts",
                    on_page=contact_progress,
                    on_retry=rate_limit_progress(contact_fetch_label),
                    on_request=_make_xero_request_tracer(sync_run_id, user["id"], "contacts"),
                    modified_since=modified_since if is_incremental_sync else None,
                )
                contact_fetch_summary = f"Fetched {len(raw_contacts)} {contact_fetch_label}."
            except HTTPException as exc:
                retry_after_seconds = _xero_rate_limit_retry_seconds(exc)
                if retry_after_seconds is None:
                    raise
                contact_fetch_summary = (
                    f"Xero Contacts is rate-limited for about {_format_wait_seconds(retry_after_seconds)}. "
                    "Continuing with customer details embedded in invoices."
                )
                record_audit_event(
                    "sync_run",
                    str(sync_run_id),
                    "sync.contacts.rate_limited",
                    {
                        "summary": contact_fetch_summary,
                        "retry_after_seconds": retry_after_seconds,
                        "detail": _sync_error_payload(exc),
                    },
                    user["id"],
                )
            _update_sync_run(
                sync_run_id,
                current_step=invoice_fetch_step,
                summary=f"{contact_fetch_summary} Fetching {invoice_fetch_label}.",
                contacts_total=len(raw_contacts),
            )
            outstanding_invoices = await fetch_paginated_collection(
                connection_row,
                INVOICES_URL,
                "Invoices",
                params={"where": invoice_where},
                on_page=outstanding_invoice_progress,
                on_retry=rate_limit_progress(invoice_fetch_label),
                on_request=_make_xero_request_tracer(sync_run_id, user["id"], "outstanding_invoices"),
                modified_since=modified_since,
            )
            contacts = _customer_contacts_from_xero(raw_contacts, outstanding_invoices)
            _update_sync_run(
                sync_run_id,
                current_step=f"Importing {invoice_fetch_label}",
                summary=(
                    f"Fetched {len(outstanding_invoices)} {invoice_fetch_label}. "
                    f"Prepared {len(contacts)} customer records from Xero contacts and invoice contacts."
                ),
                contacts_total=len(contacts),
                invoices_total=len(outstanding_invoices),
                customers_synced=0,
                invoices_synced=0,
                fetched_count=len(contacts) + len(outstanding_invoices),
                processed_count=0,
                failed_count=0,
            )

            with get_connection() as connection:
                with connection.cursor() as cursor:
                    imported_contacts = 0

                    for raw_contact in contacts:
                        _upsert_xero_customer(cursor, raw_contact, connection_row["tenant_id"], now)
                        imported_contacts += 1
                        if imported_contacts % 100 == 0:
                            _update_sync_run(
                                sync_run_id,
                                current_step="Importing contacts",
                                summary=f"Imported {imported_contacts} of {len(contacts)} contacts.",
                                customers_synced=imported_contacts,
                                processed_count=imported_contacts,
                            )

                    cursor.execute(
                        "SELECT id, xero_contact_id FROM customers WHERE tenant_id = %s",
                        (connection_row["tenant_id"],),
                    )
                    customer_lookup = {
                        row["xero_contact_id"]: row["id"]
                        for row in cursor.fetchall()
                    }

                    customer_totals: dict[str, dict[str, Decimal]] = {}
                    synced_invoices = 0
                    _update_sync_run(
                        sync_run_id,
                        current_step=f"Importing {invoice_fetch_label}",
                        summary=f"Imported {imported_contacts} {contact_fetch_label}. Importing {invoice_fetch_label}.",
                        customers_synced=imported_contacts,
                        invoices_synced=0,
                        processed_count=imported_contacts,
                    )

                    def import_invoice_batch(raw_invoices: list[dict], phase_label: str, update_customer_totals: bool) -> int:
                        nonlocal synced_invoices
                        imported_batch = 0
                        for raw_invoice in raw_invoices:
                            invoice = normalise_invoice(raw_invoice)
                            if not invoice["xero_contact_id"]:
                                continue

                            customer_id = customer_lookup.get(invoice["xero_contact_id"])
                            if customer_id is None:
                                continue

                            cursor.execute(
                                """
                                INSERT INTO invoices (
                                    customer_id, xero_invoice_id, invoice_number, status, due_date, invoice_date,
                                    description, line_items, currency_code, total, amount_due, amount_paid, xero_updated_at, synced_at, updated_at
                                )
                                VALUES (
                                    %(customer_id)s, %(xero_invoice_id)s, %(invoice_number)s, %(status)s, %(due_date)s, %(invoice_date)s,
                                    %(description)s, %(line_items_json)s::jsonb, %(currency_code)s, %(total)s, %(amount_due)s, %(amount_paid)s, %(xero_updated_at)s, %(synced_at)s, %(updated_at)s
                                )
                                ON CONFLICT (xero_invoice_id) DO UPDATE
                                SET customer_id = EXCLUDED.customer_id,
                                    invoice_number = EXCLUDED.invoice_number,
                                    status = EXCLUDED.status,
                                    due_date = EXCLUDED.due_date,
                                    invoice_date = EXCLUDED.invoice_date,
                                    description = EXCLUDED.description,
                                    line_items = EXCLUDED.line_items,
                                    currency_code = EXCLUDED.currency_code,
                                    total = EXCLUDED.total,
                                    amount_due = EXCLUDED.amount_due,
                                    amount_paid = EXCLUDED.amount_paid,
                                    xero_updated_at = EXCLUDED.xero_updated_at,
                                    synced_at = EXCLUDED.synced_at,
                                    updated_at = EXCLUDED.updated_at
                                RETURNING id, control_status
                                """,
                                {
                                    **invoice,
                                    "line_items_json": json.dumps(invoice.get("line_items") or [], default=_json_default),
                                    "customer_id": customer_id,
                                    "synced_at": now,
                                    "updated_at": now,
                                },
                            )
                            stored = cursor.fetchone()
                            synced_invoices += 1
                            imported_batch += 1

                            cursor.execute(
                                """
                                INSERT INTO invoice_status_history (invoice_id, status, note, changed_by_user_id)
                                SELECT %s, %s, %s, %s
                                WHERE NOT EXISTS (
                                    SELECT 1 FROM invoice_status_history
                                    WHERE invoice_id = %s AND status = %s
                                )
                                """,
                                (
                                    stored["id"],
                                    invoice["status"],
                                    "Imported from Xero sync",
                                    user["id"],
                                    stored["id"],
                                    invoice["status"],
                                ),
                            )

                            if update_customer_totals:
                                totals = customer_totals.setdefault(
                                    customer_id,
                                    {"total_due": Decimal("0"), "overdue_amount": Decimal("0")},
                                )
                                amount_due = Decimal(str(invoice["amount_due"]))
                                totals["total_due"] += amount_due
                                if invoice["due_date"] and invoice["due_date"] < now.date() and amount_due > 0:
                                    totals["overdue_amount"] += amount_due

                            if synced_invoices % 100 == 0:
                                _update_sync_run(
                                    sync_run_id,
                                    current_step=f"Importing {phase_label} invoices",
                                    summary=f"Imported {synced_invoices} invoices from Xero.",
                                    customers_synced=imported_contacts,
                                    invoices_synced=synced_invoices,
                                    processed_count=imported_contacts + synced_invoices,
                                )
                        return imported_batch

                    outstanding_synced = import_invoice_batch(outstanding_invoices, invoice_fetch_label, True)

                    for customer_id, totals in customer_totals.items():
                        cursor.execute(
                            """
                            UPDATE customers
                            SET total_due = %s,
                                overdue_amount = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (totals["total_due"], totals["overdue_amount"], now, customer_id),
                        )

                    _refresh_customer_totals(cursor, connection_row["tenant_id"], now)
                    ready_step = "Finalising incremental sync" if is_incremental_sync and not needs_paid_backfill else OUTSTANDING_READY_STEP
                    if is_incremental_sync and needs_paid_backfill:
                        ready_summary = (
                            f"Incremental changes ready: synced {imported_contacts} changed contacts and "
                            f"{outstanding_synced} changed invoices. Backfilling {sync_options['label'].lower()}."
                        )
                    elif is_incremental_sync:
                        ready_summary = (
                            f"Incremental changes ready: synced {imported_contacts} changed contacts and "
                            f"{outstanding_synced} changed invoices."
                        )
                    else:
                        ready_summary = (
                            f"Outstanding invoices ready: synced {outstanding_synced} outstanding invoices. "
                            "Backfilling paid invoices."
                        )
                    cursor.execute(
                        """
                        UPDATE sync_runs
                        SET status = %s,
                            current_step = %s,
                            customers_synced = %s,
                            invoices_synced = %s,
                            fetched_count = %s,
                            processed_count = %s,
                            failed_count = %s,
                            contacts_total = %s,
                            invoices_total = %s,
                            summary = %s,
                            completed_at = %s
                        WHERE id = %s
                        RETURNING *
                        """,
                        (
                            "running",
                            ready_step,
                            len(contacts),
                            synced_invoices,
                            len(contacts) + len(outstanding_invoices),
                            len(contacts) + synced_invoices,
                            0,
                            len(contacts),
                            len(outstanding_invoices),
                            ready_summary,
                            None,
                            sync_run_id,
                        ),
                    )
                    outstanding_ready = cursor.fetchone()
                connection.commit()

            _upsert_sync_checkpoint(
                sync_run_id,
                user["id"],
                connection_row["tenant_id"],
                sync_signature,
                SYNC_PHASE_OUTSTANDING,
                "completed",
                records_seen=len(contacts) + len(outstanding_invoices),
                records_stored=len(contacts) + outstanding_synced,
                payload={
                    "contacts_count": len(contacts),
                    "outstanding_count": outstanding_synced,
                    "synced_invoices": synced_invoices,
                    "invoice_years": sync_options["invoice_years"],
                },
            )

        record_audit_event(
            "sync_run",
            str(outstanding_ready["id"]),
            "sync.incremental_ready" if is_incremental_sync else "sync.outstanding_ready",
            {"summary": outstanding_ready["summary"]},
            user["id"],
        )
        payments_synced = await sync_payments_step()
        credit_sources_synced = await sync_credit_sources_step()

        if is_incremental_sync and not needs_paid_backfill:
            completed = _update_sync_run(
                sync_run_id,
                status="completed",
                current_step="Incremental sync complete",
                summary=(
                    f"Incremental sync complete: refreshed changes since {modified_since.isoformat()}. "
                    f"Synced {imported_contacts} changed contacts and {outstanding_synced} changed invoices. "
                    f"Pulled through {payments_synced} payments and {credit_sources_synced} customer credits. Scope: {sync_options['label']}."
                ),
                customers_synced=len(contacts),
                invoices_synced=synced_invoices,
                fetched_count=len(contacts) + len(outstanding_invoices) + payments_synced + credit_sources_synced,
                processed_count=len(contacts) + synced_invoices + payments_synced + credit_sources_synced,
                failed_count=0,
                contacts_total=len(contacts),
                invoices_total=len(outstanding_invoices),
                completed_at=utcnow(),
            )
            record_audit_event("sync_run", str(completed["id"]), "sync.completed", {"summary": completed["summary"]}, user["id"])
            return completed

        paid_page_limit = sync_options["paid_page_limit"]
        if paid_page_limit == 0:
            completed = _update_sync_run(
                sync_run_id,
                status="completed",
                current_step="Outstanding sync complete",
                summary=(
                    f"Synced {len(contacts)} customers and {outstanding_synced} outstanding invoices. "
                    f"Pulled through {payments_synced} payments and {credit_sources_synced} customer credits. Paid invoice history was left for a later staged sync."
                ),
                customers_synced=len(contacts),
                invoices_synced=synced_invoices,
                fetched_count=len(contacts) + len(outstanding_invoices) + payments_synced + credit_sources_synced,
                processed_count=len(contacts) + synced_invoices + payments_synced + credit_sources_synced,
                failed_count=0,
                contacts_total=len(contacts),
                invoices_total=len(outstanding_invoices),
                completed_at=utcnow(),
            )
            record_audit_event("sync_run", str(completed["id"]), "sync.completed", {"summary": completed["summary"]}, user["id"])
            return completed

        paid_checkpoint = resume_checkpoints.get(SYNC_PHASE_PAID_INVOICES)
        paid_synced = 0
        paid_contacts_synced = 0
        paid_records_seen = 0
        if _checkpoint_completed(resume_checkpoints, SYNC_PHASE_PAID_INVOICES):
            paid_synced = int(paid_checkpoint.get("records_stored") or 0)
            paid_records_seen = int(paid_checkpoint.get("records_seen") or paid_synced)
            paid_contacts_synced = _checkpoint_payload_count(paid_checkpoint, "paid_contacts_synced", 0)
            synced_invoices += paid_synced
            _upsert_sync_checkpoint(
                sync_run_id,
                user["id"],
                connection_row["tenant_id"],
                sync_signature,
                SYNC_PHASE_PAID_INVOICES,
                "completed",
                page_number=int(paid_checkpoint.get("page_number") or 0),
                records_seen=paid_records_seen,
                records_stored=paid_synced,
                payload={**(paid_checkpoint.get("payload") or {}), "resumed_from": str(resume_source_run.get("id") or "")},
            )
            _update_sync_run(
                sync_run_id,
                current_step="Paid invoices backfilled",
                summary=f"Reused {paid_synced} paid invoices from the interrupted sync.",
                invoices_synced=synced_invoices,
            )
        else:
            start_page = int((paid_checkpoint or {}).get("page_number") or 0) + 1
            already_seen = int((paid_checkpoint or {}).get("records_seen") or 0)
            already_synced = int((paid_checkpoint or {}).get("records_stored") or 0)
            already_paid_contacts = _checkpoint_payload_count(paid_checkpoint, "paid_contacts_synced", 0)
            _update_sync_run(
                sync_run_id,
                current_step="Fetching paid invoices from Xero",
                summary=(
                    f"Resuming paid invoice backfill from Xero page {start_page}."
                    if paid_checkpoint
                    else (
                        f"Changed invoices are ready. Backfilling {sync_options['label'].lower()}."
                        if is_incremental_sync
                        else f"Outstanding invoices are ready. {sync_options['summary']}"
                    )
                ),
            )
            _upsert_sync_checkpoint(
                sync_run_id,
                user["id"],
                connection_row["tenant_id"],
                sync_signature,
                SYNC_PHASE_PAID_INVOICES,
                "running",
                page_number=max(start_page - 1, 0),
                records_seen=already_seen,
                records_stored=already_synced,
                payload={
                    "paid_contacts_synced": already_paid_contacts,
                    "resumed_from": str(resume_source_run.get("id") or "") if paid_checkpoint else "",
                },
            )
            last_paid_page = max(start_page - 1, int((paid_checkpoint or {}).get("page_number") or 0))
            last_paid_seen = already_seen

            def paid_store_progress(
                page_number: int,
                total_records: int,
                stored_records: int,
                contact_records: int,
                invoice_records: int,
            ) -> None:
                _update_sync_run(
                    sync_run_id,
                    current_step="Backfilling paid invoices",
                    summary=f"Backfilled {stored_records} of {total_records} paid invoices.",
                    customers_synced=len(contacts) + contact_records,
                    contacts_total=len(contacts) + contact_records,
                    invoices_synced=invoice_records,
                    invoices_total=len(outstanding_invoices) + total_records,
                    processed_count=len(contacts) + contact_records + invoice_records,
                )

            def paid_checkpoint_progress(
                page_number: int,
                total_records: int,
                stored_records: int,
                contact_records: int,
                invoice_records: int,
            ) -> None:
                nonlocal last_paid_page, last_paid_seen
                last_paid_page = page_number
                last_paid_seen = total_records
                _upsert_sync_checkpoint(
                    sync_run_id,
                    user["id"],
                    connection_row["tenant_id"],
                    sync_signature,
                    SYNC_PHASE_PAID_INVOICES,
                    "running",
                    page_number=page_number,
                    records_seen=total_records,
                    records_stored=stored_records,
                    payload={
                        "paid_contacts_synced": contact_records,
                        "synced_invoices": invoice_records,
                        "resumed_from": str(resume_source_run.get("id") or "") if paid_checkpoint else "",
                    },
                )

            paid_result = await _sync_xero_paid_invoice_backfill(
                connection_row,
                user,
                utcnow(),
                sync_options["invoice_years"],
                paid_page_limit,
                base_contact_count=len(contacts),
                base_invoice_count=synced_invoices,
                start_page=start_page,
                already_seen=already_seen,
                already_synced=already_synced,
                already_contacts_synced=already_paid_contacts,
                on_page=paid_invoice_progress,
                on_retry=rate_limit_progress("paid invoices"),
                on_store=paid_store_progress,
                on_checkpoint=paid_checkpoint_progress,
                on_request=_make_xero_request_tracer(sync_run_id, user["id"], "paid_invoices"),
            )
            paid_synced = int(paid_result["paid_synced"])
            paid_contacts_synced = int(paid_result["paid_contacts_synced"])
            synced_invoices = int(paid_result["synced_invoices"])
            paid_records_seen = last_paid_seen
            _upsert_sync_checkpoint(
                sync_run_id,
                user["id"],
                connection_row["tenant_id"],
                sync_signature,
                SYNC_PHASE_PAID_INVOICES,
                "completed",
                page_number=last_paid_page,
                records_seen=paid_records_seen,
                records_stored=paid_synced,
                payload={
                    "paid_contacts_synced": paid_contacts_synced,
                    "synced_invoices": synced_invoices,
                    "resumed_from": str(resume_source_run.get("id") or "") if paid_checkpoint else "",
                },
            )

        total_contacts_synced = len(contacts) + paid_contacts_synced
        completion_summary = (
            f"Incremental sync complete: refreshed {total_contacts_synced} customer contacts, "
            f"{outstanding_synced} changed invoices, and backfilled {paid_synced} paid invoices. "
            f"Pulled through {payments_synced} payments and {credit_sources_synced} customer credits. Scope: {sync_options['label']}."
            if is_incremental_sync
            else f"Synced {total_contacts_synced} customers, {outstanding_synced} outstanding invoices, {paid_synced} paid invoices, {payments_synced} payments, and {credit_sources_synced} customer credits from Xero. Scope: {sync_options['label']}."
        )
        completed = _update_sync_run(
            sync_run_id,
            status="completed",
            current_step="Sync complete",
            customers_synced=total_contacts_synced,
            invoices_synced=synced_invoices,
            fetched_count=total_contacts_synced + len(outstanding_invoices) + paid_records_seen + payments_synced + credit_sources_synced,
            processed_count=total_contacts_synced + synced_invoices + payments_synced + credit_sources_synced,
            failed_count=0,
            contacts_total=total_contacts_synced,
            invoices_total=len(outstanding_invoices) + paid_records_seen,
            summary=completion_summary,
            completed_at=utcnow(),
        )

        record_audit_event("sync_run", str(completed["id"]), "sync.completed", {"summary": completed["summary"]}, user["id"])
        return completed
    except Exception as exc:
        message = _sync_error_message(exc)
        failure_fields = {
            "status": "failed",
            "current_step": "Sync failed",
            "summary": "Xero sync failed.",
            "error_message": message,
            "failed_count": 1,
            "completed_at": utcnow(),
        }
        retry_after_seconds = _xero_rate_limit_retry_seconds(exc)
        if retry_after_seconds is not None:
            failure_fields["rate_limit_until"] = utcnow() + timedelta(seconds=retry_after_seconds)
            failure_fields["retry_after_seconds"] = retry_after_seconds
        _update_sync_run(sync_run_id, **failure_fields)
        try:
            record_audit_event(
                "sync_run",
                str(sync_run_id),
                "sync.failed",
                {"error": message, "detail": _sync_error_payload(exc)},
                user["id"],
            )
        except Exception:
            logger.exception("Unable to record failed sync audit event")
        raise


def _late_payment_breakdown(amount_due: float, overdue_days: int) -> dict:
    rate = get_settings().statutory_interest_rate
    interest = round(amount_due * rate * max(overdue_days, 0) / 365, 2)

    if amount_due <= 300:
        court_cost = 35
    elif amount_due <= 500:
        court_cost = 50
    elif amount_due <= 1000:
        court_cost = 70
    elif amount_due <= 1500:
        court_cost = 80
    elif amount_due <= 3000:
        court_cost = 115
    elif amount_due <= 5000:
        court_cost = 205
    else:
        court_cost = 455

    return {"interest": interest, "court_cost": court_cost}


def dashboard_payload(tenant_id: str | None = None) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    MAX(synced_at) AS as_of,
                    COUNT(*) FILTER (WHERE amount_due > 0) AS invoice_count,
                    COALESCE(SUM(CASE WHEN due_date >= CURRENT_DATE - INTERVAL '30 days' AND due_date < CURRENT_DATE THEN amount_due ELSE 0 END), 0) AS overdue_1_30,
                    COALESCE(SUM(CASE WHEN due_date >= CURRENT_DATE - INTERVAL '60 days' AND due_date < CURRENT_DATE - INTERVAL '30 days' THEN amount_due ELSE 0 END), 0) AS overdue_31_60,
                    COALESCE(SUM(CASE WHEN due_date >= CURRENT_DATE - INTERVAL '90 days' AND due_date < CURRENT_DATE - INTERVAL '60 days' THEN amount_due ELSE 0 END), 0) AS overdue_61_90,
                    COALESCE(SUM(CASE WHEN due_date < CURRENT_DATE - INTERVAL '90 days' THEN amount_due ELSE 0 END), 0) AS overdue_90_plus
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE (%s IS NULL OR customers.tenant_id = %s)
                """,
                (tenant_id, tenant_id),
            )
            summary = cursor.fetchone()
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(total_due), 0) AS total_receivables,
                    COALESCE(SUM(overdue_amount), 0) AS total_overdue,
                    COALESCE(SUM(credits.credit_balance), 0) AS credit_balance,
                    COUNT(*) FILTER (WHERE overdue_amount > 0) AS accounts_needing_action
                FROM customers
                LEFT JOIN (
                    SELECT customer_id, COALESCE(SUM(remaining_credit), 0) AS credit_balance
                    FROM customer_credits
                    WHERE remaining_credit > 0
                    GROUP BY customer_id
                ) AS credits ON credits.customer_id = customers.id
                WHERE (%s IS NULL OR customers.tenant_id = %s)
                """,
                (tenant_id, tenant_id),
            )
            customer_summary = cursor.fetchone()
            cursor.execute(
                """
                SELECT customers.name,
                       customers.total_due AS amount_due,
                       MIN(invoices.due_date) FILTER (WHERE invoices.amount_due > 0) AS due_date
                FROM customers
                LEFT JOIN invoices ON invoices.customer_id = customers.id
                WHERE customers.total_due > 0
                  AND (%s IS NULL OR customers.tenant_id = %s)
                GROUP BY customers.id, customers.name, customers.total_due
                ORDER BY customers.total_due DESC, due_date ASC NULLS LAST
                LIMIT 5
                """,
                (tenant_id, tenant_id),
            )
            risks = cursor.fetchall()
            cursor.execute(
                """
                SELECT completed_at
                FROM sync_runs
                WHERE provider = %s
                  AND status = %s
                ORDER BY completed_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """,
                ("xero", "completed"),
            )
            latest_sync = cursor.fetchone()
        connection.commit()

    return {
        "as_of": summary["as_of"] or (latest_sync or {}).get("completed_at"),
        "invoice_count": summary["invoice_count"] or 0,
        "total_receivables": float(customer_summary["total_receivables"] or 0),
        "total_overdue": float(customer_summary["total_overdue"] or 0),
        "credit_balance": float(customer_summary["credit_balance"] or 0),
        "overdue_1_30": float(summary["overdue_1_30"] or 0),
        "overdue_31_60": float(summary["overdue_31_60"] or 0),
        "overdue_61_90": float(summary["overdue_61_90"] or 0),
        "overdue_90_plus": float(summary["overdue_90_plus"] or 0),
        "accounts_needing_action": customer_summary["accounts_needing_action"] or 0,
        "top_risk_accounts": [
            {
                "name": row["name"],
                "amount_due": float(row["amount_due"]),
                "due_date": row["due_date"],
            }
            for row in risks
        ],
    }


def _iso(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _float(value) -> float:
    return float(value or 0)


def _serialize_timeline_items(rows: list[dict], title_key: str, body_key: str, stamp_key: str = "created_at") -> list[dict]:
    items = []
    for row in rows:
        items.append(
            {
                "id": row.get("id"),
                "title": row.get(title_key) or "Update",
                "body": row.get(body_key) or "",
                "stamp": _iso(row.get(stamp_key)) or "",
            }
        )
    return items


def _serialize_invoice(invoice: dict, detail: dict | None = None) -> dict:
    payload = {
        "id": invoice["id"],
        "xeroInvoiceId": invoice.get("xero_invoice_id") or "",
        "invoiceNumber": invoice.get("invoice_number") or "",
        "status": invoice.get("status") or "",
        "controlStatus": invoice.get("control_status") or invoice.get("status") or "New",
        "dueDate": _iso(invoice.get("due_date")),
        "invoiceDate": _iso(invoice.get("invoice_date")),
        "description": invoice.get("description") or "",
        "lineItems": invoice.get("line_items") or [],
        "currencyCode": invoice.get("currency_code") or "GBP",
        "total": _float(invoice.get("total")),
        "amountDue": _float(invoice.get("amount_due")),
        "amountPaid": _float(invoice.get("amount_paid")),
        "promisedDate": _iso(invoice.get("promised_date")),
        "promiseStatus": invoice.get("promise_status") or "",
        "lastChasedAt": _iso(invoice.get("last_chased_at")),
        "overdueDays": invoice.get("overdue_days") or 0,
        "latePayment": invoice.get("late_payment") or {"interest": 0, "court_cost": 35},
        "latePaymentChargeRaisedAt": _iso(invoice.get("late_payment_charge_raised_at")),
        "latePaymentChargeInvoiceId": invoice.get("late_payment_charge_invoice_id") or "",
        "latePaymentChargeInvoiceNumber": invoice.get("late_payment_charge_invoice_number") or "",
        "latePaymentChargeAmount": _float(invoice.get("late_payment_charge_amount")),
        "badDebtWriteOffAt": _iso(invoice.get("bad_debt_write_off_at")),
        "badDebtCreditNoteId": invoice.get("bad_debt_credit_note_id") or "",
        "badDebtCreditNoteNumber": invoice.get("bad_debt_credit_note_number") or "",
        "badDebtCreditNoteAmount": _float(invoice.get("bad_debt_credit_note_amount")),
    }
    if detail:
        payload["notes"] = _serialize_timeline_items(detail["notes"], "full_name", "body")
        payload["promises"] = [
            {
                "id": row.get("id"),
                "title": f"Promise for £{_float(row.get('promised_amount')):,.2f}",
                "body": row.get("note") or "",
                "stamp": _iso(row.get("promised_date")) or "",
                "status": row.get("status") or "",
            }
            for row in detail["promises"]
        ]
        payload["statuses"] = [
            {
                "id": row.get("id"),
                "title": row.get("status") or "Status updated",
                "body": row.get("note") or "",
                "stamp": _iso(row.get("created_at")) or "",
            }
            for row in detail["statuses"]
        ]
        payload["audit"] = [
            {
                "id": row.get("id"),
                "entityType": row.get("entity_type") or "",
                "entityId": row.get("entity_id") or "",
                "title": row.get("event_type") or "Audit event",
                "body": row.get("payload") if isinstance(row.get("payload"), str) else __import__("json").dumps(row.get("payload") or {}),
                "stamp": _iso(row.get("created_at")) or "",
            }
            for row in detail["audit"]
        ]
    return payload


def _serialize_payment(payment: dict) -> dict:
    return {
        "id": payment.get("id"),
        "xeroPaymentId": payment.get("xero_payment_id") or "",
        "invoiceId": payment.get("invoice_id") or "",
        "xeroInvoiceId": payment.get("xero_invoice_id") or "",
        "invoiceNumber": payment.get("invoice_number") or "",
        "date": _iso(payment.get("payment_date")),
        "amount": _float(payment.get("amount")),
        "currencyCode": payment.get("currency_code") or "GBP",
        "reference": payment.get("reference") or "",
        "status": payment.get("status") or "",
        "accountName": payment.get("account_name") or "",
    }


def panel_payload(user: dict | None = None) -> dict:
    customers = []
    selected_invoice = None
    xero_connected = False
    xero_connection = None
    tenant_id = None
    if user and user.get("id"):
        try:
            xero_connection = get_xero_connection_for_user(user["id"])
            tenant_id = xero_connection.get("tenant_id")
            xero_connected = True
        except HTTPException:
            xero_connected = False

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM customers
                WHERE (%s IS NULL OR tenant_id = %s)
                ORDER BY overdue_amount DESC, total_due DESC, name ASC
                """,
                (tenant_id, tenant_id),
            )
            customer_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT invoices.*
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE (%s IS NULL OR customers.tenant_id = %s)
                ORDER BY due_date ASC NULLS LAST, invoice_number ASC
                """,
                (tenant_id, tenant_id),
            )
            invoice_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT *
                FROM audit_events
                ORDER BY created_at DESC
                LIMIT 30
                """
            )
            audit_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT customer_notes.*, users.full_name
                FROM customer_notes
                JOIN customers ON customers.id = customer_notes.customer_id
                LEFT JOIN users ON users.id = customer_notes.user_id
                WHERE (%s IS NULL OR customers.tenant_id = %s)
                ORDER BY customer_notes.created_at DESC
                """,
                (tenant_id, tenant_id),
            )
            customer_note_rows = cursor.fetchall()
            invoice_ids = [row["id"] for row in invoice_rows]
            note_rows = []
            promise_rows = []
            status_rows = []
            payment_rows = []
            credit_total_rows = []
            if invoice_ids:
                cursor.execute(
                    """
                    SELECT notes.*, users.full_name
                    FROM notes
                    LEFT JOIN users ON users.id = notes.user_id
                    WHERE notes.invoice_id = ANY(%s)
                    ORDER BY notes.created_at DESC
                    """,
                    (invoice_ids,),
                )
                note_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT payment_promises.*, users.full_name
                    FROM payment_promises
                    LEFT JOIN users ON users.id = payment_promises.created_by_user_id
                    WHERE payment_promises.invoice_id = ANY(%s)
                    ORDER BY payment_promises.created_at DESC
                    """,
                    (invoice_ids,),
                )
                promise_rows = cursor.fetchall()
                cursor.execute(
                    """
                    SELECT invoice_status_history.*, users.full_name
                    FROM invoice_status_history
                    LEFT JOIN users ON users.id = invoice_status_history.changed_by_user_id
                    WHERE invoice_status_history.invoice_id = ANY(%s)
                    ORDER BY invoice_status_history.created_at DESC
                    """,
                    (invoice_ids,),
                )
                status_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT *
                FROM payments
                WHERE (%s IS NULL OR customer_id IN (
                    SELECT id FROM customers WHERE tenant_id = %s
                ))
                ORDER BY payment_date DESC NULLS LAST, created_at DESC
                LIMIT %s
                """,
                (tenant_id, tenant_id, PANEL_PAYMENT_LIMIT),
            )
            payment_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT customer_id,
                       COALESCE(SUM(remaining_credit), 0) AS credit_balance,
                       COUNT(*) AS credit_count
                FROM customer_credits
                WHERE remaining_credit > 0
                  AND (%s IS NULL OR customer_id IN (
                      SELECT id FROM customers WHERE tenant_id = %s
                  ))
                GROUP BY customer_id
                """,
                (tenant_id, tenant_id),
            )
            credit_total_rows = cursor.fetchall()
        connection.commit()

    invoices_by_customer: dict[str, list[dict]] = {}
    notes_by_customer: dict[str, list[dict]] = {}
    notes_by_invoice: dict[str, list[dict]] = defaultdict(list)
    promises_by_invoice: dict[str, list[dict]] = defaultdict(list)
    statuses_by_invoice: dict[str, list[dict]] = defaultdict(list)
    payments_by_customer: dict[str, list[dict]] = defaultdict(list)
    credit_totals_by_customer = {row["customer_id"]: row for row in credit_total_rows}
    for note in customer_note_rows:
        notes_by_customer.setdefault(note["customer_id"], []).append(note)
    for note in note_rows:
        notes_by_invoice[note["invoice_id"]].append(note)
    for promise in promise_rows:
        promises_by_invoice[promise["invoice_id"]].append(promise)
    for status_row in status_rows:
        statuses_by_invoice[status_row["invoice_id"]].append(status_row)
    for payment in payment_rows:
        if payment.get("customer_id"):
            payments_by_customer[payment["customer_id"]].append(payment)

    today = utcnow().date()
    for invoice in invoice_rows:
        due_date = invoice["due_date"]
        overdue_days = 0 if due_date is None else max((today - due_date).days, 0)
        invoice["late_payment"] = _late_payment_breakdown(float(invoice["amount_due"]), overdue_days)
        invoice["overdue_days"] = overdue_days
        invoices_by_customer.setdefault(invoice["customer_id"], []).append(invoice)

    for customer_row in customer_rows:
        detail_invoices = invoices_by_customer.get(customer_row["id"], [])
        invoices = []
        for invoice in detail_invoices:
            invoice_payload = _serialize_invoice(
                invoice,
                {
                    "notes": notes_by_invoice.get(invoice["id"], []),
                    "promises": promises_by_invoice.get(invoice["id"], []),
                    "statuses": statuses_by_invoice.get(invoice["id"], []),
                    "audit": [],
                },
            )
            invoices.append(invoice_payload)
            if selected_invoice is None:
                selected_invoice = invoice_payload

        open_invoices = sum(1 for invoice in detail_invoices if _float(invoice.get("amount_due")) > 0)
        gross_total_due = sum(_float(invoice.get("amount_due")) for invoice in detail_invoices)
        credit_totals = credit_totals_by_customer.get(customer_row["id"], {})
        credit_balance = _float(credit_totals.get("credit_balance"))
        customers.append(
            {
                "id": customer_row["id"],
                "xeroContactId": customer_row.get("xero_contact_id") or "",
                "name": customer_row.get("name") or "",
                "email": customer_row.get("email") or "",
                "phone": customer_row.get("phone") or "",
                "accountNumber": customer_row.get("account_number") or "",
                "latePaymentChargeBaseAmount": (
                    float(customer_row["late_payment_charge_base_amount"])
                    if customer_row.get("late_payment_charge_base_amount") is not None
                    else None
                ),
                "primaryPerson": customer_row.get("primary_person") or "",
                "contactPeople": customer_row.get("contact_people") or [],
                "addresses": customer_row.get("addresses") or [],
                "contact": customer_row.get("email") or customer_row.get("phone") or "",
                "status": customer_row.get("status") or ("Action needed" if _float(customer_row.get("overdue_amount")) > 0 else "Current"),
                "openInvoices": open_invoices,
                "totalDue": _float(customer_row.get("total_due")),
                "overdue": _float(customer_row.get("overdue_amount")),
                "clientNotes": _serialize_timeline_items(notes_by_customer.get(customer_row["id"], []), "full_name", "body"),
                "payments": [_serialize_payment(payment) for payment in payments_by_customer.get(customer_row["id"], [])],
                "invoices": invoices,
            }
        )

    dashboard = dashboard_payload(tenant_id)
    last_sync_label = f'Last sync {dashboard["as_of"]}' if dashboard["as_of"] else "Waiting for first sync"
    if not xero_connected and dashboard["as_of"]:
        last_sync_label = "Xero disconnected"
    return {
        "organisation": {
            "name": xero_connection.get("tenant_name", "Xero Organisation") if xero_connected and xero_connection else "",
            "status": "Connected" if xero_connected else "Awaiting live connection",
            "lastSync": last_sync_label,
            "xeroConnected": xero_connected,
        },
        "dashboard": {
            "totalReceivables": dashboard["total_receivables"],
            "totalOverdue": dashboard["total_overdue"],
            "openInvoices": dashboard["invoice_count"],
            "accountsNeedingAction": dashboard["accounts_needing_action"],
            "potentialInterest": round(
                sum((invoice.get("late_payment") or {}).get("interest", 0) for customer in customers for invoice in customer["invoices"]),
                2,
            ),
        },
        "customers": customers,
        "audit": [
            {
                "id": row.get("id"),
                "entityType": row.get("entity_type") or "",
                "entityId": row.get("entity_id") or "",
                "title": row.get("event_type") or "Audit event",
                "body": row.get("payload") if isinstance(row.get("payload"), str) else __import__("json").dumps(row.get("payload") or {}),
                "stamp": _iso(row.get("created_at")) or "",
            }
            for row in audit_rows
        ],
        "selectedInvoice": selected_invoice,
    }


def _money(value) -> Decimal:
    try:
        return Decimal(str(value if value is not None else 0)).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def _parse_iso_date(value, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} must be a valid date.") from exc


def _positive_money(value, field_name: str) -> Decimal:
    amount = _money(value)
    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} must be greater than zero.")
    return amount


def _non_negative_money(value, field_name: str) -> Decimal:
    amount = _money(value)
    if amount < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} cannot be negative.")
    return amount


def _rate_percent(value, field_name: str = "Interest rate") -> Decimal:
    try:
        rate = Decimal(str(value if value is not None else 0)).quantize(Decimal("0.000001"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} must be a valid percentage.") from exc
    if rate < 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{field_name} cannot be negative.")
    return rate


def _jashflow_tenant_id(user: dict) -> str:
    return str(get_xero_connection_for_user(user["id"]).get("tenant_id") or "")


def _jashflow_interest_summary(loan: dict, payments_total: Decimal, as_of: date | None = None) -> dict:
    as_of = as_of or utcnow().date()
    start_date = loan.get("start_date") or as_of
    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if not isinstance(start_date, date):
        start_date = _parse_iso_date(start_date, "Start date")

    principal = _money(loan.get("principal_amount"))
    fee = _money(loan.get("arrangement_fee"))
    base = principal + fee
    annual_rate_percent = _rate_percent(loan.get("annual_interest_rate"))
    annual_rate = max(0.0, float(annual_rate_percent) / 100)
    elapsed_days = max((as_of - start_date).days, 0)
    daily_rate = (1 + annual_rate) ** (1 / 365) - 1 if annual_rate else 0
    monthly_rate = (1 + annual_rate) ** (1 / 12) - 1 if annual_rate else 0
    accrued_interest = _money(float(base) * (((1 + daily_rate) ** elapsed_days) - 1)) if daily_rate else Decimal("0.00")
    balance = _money(base + accrued_interest - payments_total)
    duration_months = max(1, int(loan.get("duration_months") or 1))
    if monthly_rate:
        monthly_repayment = _money(float(base) * (monthly_rate * ((1 + monthly_rate) ** duration_months)) / (((1 + monthly_rate) ** duration_months) - 1))
    else:
        monthly_repayment = _money(base / Decimal(duration_months))
    return {
        "daysAccrued": elapsed_days,
        "dailyInterestRate": daily_rate,
        "accruedInterest": accrued_interest,
        "balance": balance,
        "monthlyRepayment": monthly_repayment,
    }


def _serialize_jashflow_loan(loan: dict, transactions: list[dict], invoiced_interest_total: Decimal | None = None) -> dict:
    payments_total = sum((_money(row.get("amount")) for row in transactions if row.get("transaction_type") == "payment"), Decimal("0.00"))
    invoiced_interest_total = _money(invoiced_interest_total)
    summary = _jashflow_interest_summary(loan, payments_total)
    uninvoiced_interest = max(Decimal("0.00"), _money(summary["accruedInterest"] - invoiced_interest_total))
    running_balance = Decimal("0.00")
    statement_rows = []
    for row in sorted(transactions, key=lambda item: (item.get("transaction_date") or date.min, item.get("created_at") or datetime.min.replace(tzinfo=timezone.utc))):
        amount = _money(row.get("amount"))
        signed_amount = -amount if row.get("transaction_type") == "payment" else amount
        running_balance = _money(running_balance + signed_amount)
        statement_rows.append({
            "id": str(row.get("id")),
            "date": _iso(row.get("transaction_date")) or "",
            "type": row.get("transaction_type") or "adjustment",
            "description": row.get("description") or "",
            "amount": float(signed_amount),
            "balance": float(running_balance),
            "createdAt": _iso(row.get("created_at")) or "",
            "isVirtual": False,
        })
    if summary["accruedInterest"] > 0:
        running_balance = _money(running_balance + summary["accruedInterest"])
        statement_rows.append({
            "id": f"{loan['id']}:interest",
            "date": utcnow().date().isoformat(),
            "type": "interest",
            "description": f"Daily compound interest accrued over {summary['daysAccrued']} day{'s' if summary['daysAccrued'] != 1 else ''}",
            "amount": float(summary["accruedInterest"]),
            "balance": float(running_balance),
            "createdAt": "",
            "isVirtual": True,
        })

    return {
        "id": str(loan["id"]),
        "customerId": str(loan.get("customer_id")),
        "customerName": loan.get("customer_name") or "Unnamed client",
        "xeroContactId": loan.get("xero_contact_id") or "",
        "principalAmount": float(_money(loan.get("principal_amount"))),
        "arrangementFee": float(_money(loan.get("arrangement_fee"))),
        "annualInterestRate": float(_rate_percent(loan.get("annual_interest_rate"))),
        "durationMonths": int(loan.get("duration_months") or 0),
        "startDate": _iso(loan.get("start_date")) or "",
        "status": loan.get("status") or "active",
        "createdAt": _iso(loan.get("created_at")) or "",
        "updatedAt": _iso(loan.get("updated_at")) or "",
        "daysAccrued": summary["daysAccrued"],
        "dailyInterestRate": summary["dailyInterestRate"],
        "accruedInterest": float(summary["accruedInterest"]),
        "invoicedInterest": float(invoiced_interest_total),
        "uninvoicedInterest": float(uninvoiced_interest),
        "paymentsTotal": float(payments_total),
        "balance": float(summary["balance"]),
        "monthlyRepayment": float(summary["monthlyRepayment"]),
        "transactions": statement_rows,
    }


def jashflow_payload(user: dict) -> dict:
    tenant_id = _jashflow_tenant_id(user)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, email, xero_contact_id
                FROM customers
                WHERE tenant_id = %s
                ORDER BY name ASC
                """,
                (tenant_id,),
            )
            customers = cursor.fetchall()
            cursor.execute(
                """
                SELECT loans.*, customers.name AS customer_name, customers.xero_contact_id
                FROM jashflow_loans AS loans
                JOIN customers ON customers.id = loans.customer_id
                WHERE loans.tenant_id = %s
                ORDER BY loans.created_at DESC
                """,
                (tenant_id,),
            )
            loan_rows = cursor.fetchall()
            loan_ids = [row["id"] for row in loan_rows]
            transactions_by_loan = defaultdict(list)
            interest_posted_by_loan = defaultdict(lambda: Decimal("0.00"))
            if loan_ids:
                cursor.execute(
                    """
                    SELECT *
                    FROM jashflow_transactions
                    WHERE loan_id = ANY(%s)
                    ORDER BY transaction_date ASC, created_at ASC
                    """,
                    (loan_ids,),
                )
                for row in cursor.fetchall():
                    transactions_by_loan[str(row["loan_id"])].append(row)
                cursor.execute(
                    """
                    SELECT lines.loan_id, COALESCE(SUM(lines.interest_amount), 0) AS posted_interest
                    FROM jashflow_interest_post_lines AS lines
                    JOIN jashflow_interest_post_batches AS batches ON batches.id = lines.batch_id
                    WHERE lines.loan_id = ANY(%s)
                      AND batches.status = 'completed'
                    GROUP BY lines.loan_id
                    """,
                    (loan_ids,),
                )
                for row in cursor.fetchall():
                    interest_posted_by_loan[str(row["loan_id"])] = _money(row.get("posted_interest"))
            cursor.execute(
                """
                SELECT *
                FROM jashflow_settings
                WHERE tenant_id = %s
                """,
                (tenant_id,),
            )
            settings_row = cursor.fetchone() or {}
            cursor.execute(
                """
                SELECT *
                FROM jashflow_interest_post_batches
                WHERE tenant_id = %s
                  AND status = 'completed'
                ORDER BY created_at DESC
                LIMIT 10
                """,
                (tenant_id,),
            )
            interest_posts = cursor.fetchall()
        connection.commit()

    loans = [
        _serialize_jashflow_loan(
            row,
            transactions_by_loan.get(str(row["id"]), []),
            interest_posted_by_loan.get(str(row["id"]), Decimal("0.00")),
        )
        for row in loan_rows
    ]
    active_loans = [loan for loan in loans if loan["status"] == "active"]
    return {
        "customers": [
            {
                "id": str(customer["id"]),
                "name": customer.get("name") or "Unnamed client",
                "email": customer.get("email") or "",
                "xeroContactId": customer.get("xero_contact_id") or "",
            }
            for customer in customers
        ],
        "loans": loans,
        "summary": {
            "activeLoans": len(active_loans),
            "principalTotal": round(sum(loan["principalAmount"] for loan in active_loans), 2),
            "accruedInterestTotal": round(sum(loan["accruedInterest"] for loan in active_loans), 2),
            "invoicedInterestTotal": round(sum(loan["invoicedInterest"] for loan in active_loans), 2),
            "uninvoicedInterestTotal": round(sum(loan["uninvoicedInterest"] for loan in active_loans), 2),
            "balanceTotal": round(sum(loan["balance"] for loan in active_loans), 2),
        },
        "settings": {
            "invoiceContactId": settings_row.get("invoice_contact_id") or "",
            "invoiceContactName": settings_row.get("invoice_contact_name") or "",
            "interestAccountCode": settings_row.get("interest_account_code") or "",
            "updatedAt": _iso(settings_row.get("updated_at")) or "",
        },
        "interestPosts": [
            {
                "id": str(row["id"]),
                "status": row.get("status") or "",
                "xeroInvoiceId": row.get("xero_invoice_id") or "",
                "xeroInvoiceNumber": row.get("xero_invoice_number") or "",
                "invoiceContactName": row.get("invoice_contact_name") or "",
                "interestAccountCode": row.get("interest_account_code") or "",
                "periodEndDate": _iso(row.get("period_end_date")) or "",
                "totalInterestAmount": float(_money(row.get("total_interest_amount"))),
                "attachmentFilename": row.get("attachment_filename") or "",
                "createdAt": _iso(row.get("created_at")) or "",
                "completedAt": _iso(row.get("completed_at")) or "",
            }
            for row in interest_posts
        ],
    }


def create_jashflow_loan(user: dict, payload: dict) -> dict:
    tenant_id = _jashflow_tenant_id(user)
    customer_id = str(payload.get("customerId") or "").strip()
    if not customer_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a Xero customer for the loan.")
    principal = _positive_money(payload.get("principalAmount"), "Loan amount")
    arrangement_fee = _non_negative_money(payload.get("arrangementFee"), "Arrangement fee")
    annual_interest_rate = _rate_percent(payload.get("annualInterestRate"), "Compound interest rate")
    try:
        duration_months = int(payload.get("durationMonths") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duration must be a number of months.") from exc
    if duration_months < 1 or duration_months > 240:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duration must be between 1 and 240 months.")
    start_date = _parse_iso_date(payload.get("startDate"), "Start date")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM customers
                WHERE id = %s
                  AND tenant_id = %s
                """,
                (customer_id, tenant_id),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found in this Xero tenant.")
            cursor.execute(
                """
                INSERT INTO jashflow_loans (
                    tenant_id, customer_id, principal_amount, arrangement_fee,
                    annual_interest_rate, duration_months, start_date, status,
                    created_by_user_id, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
                RETURNING id
                """,
                (tenant_id, customer_id, principal, arrangement_fee, annual_interest_rate, duration_months, start_date, user["id"], utcnow(), utcnow()),
            )
            loan_id = cursor.fetchone()["id"]
            cursor.execute(
                """
                INSERT INTO jashflow_transactions (
                    loan_id, transaction_date, transaction_type, amount, description, created_by_user_id
                )
                VALUES (%s, %s, 'advance', %s, 'Loan advance', %s)
                """,
                (loan_id, start_date, principal, user["id"]),
            )
            if arrangement_fee > 0:
                cursor.execute(
                    """
                    INSERT INTO jashflow_transactions (
                        loan_id, transaction_date, transaction_type, amount, description, created_by_user_id
                    )
                    VALUES (%s, %s, 'fee', %s, 'Arrangement fee', %s)
                    """,
                    (loan_id, start_date, arrangement_fee, user["id"]),
                )
        connection.commit()

    record_audit_event(
        "jashflow_loan",
        str(loan_id),
        "jashflow.loan_created",
        {
            "customer_id": customer_id,
            "principal_amount": float(principal),
            "arrangement_fee": float(arrangement_fee),
            "annual_interest_rate": float(annual_interest_rate),
            "duration_months": duration_months,
            "start_date": start_date.isoformat(),
        },
        user["id"],
    )
    return jashflow_payload(user)


def add_jashflow_payment(user: dict, loan_id: str, payload: dict) -> dict:
    tenant_id = _jashflow_tenant_id(user)
    amount = _positive_money(payload.get("amount"), "Payment amount")
    payment_date = _parse_iso_date(payload.get("paymentDate"), "Payment date")
    description = str(payload.get("description") or "Payment received").strip()[:500]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM jashflow_loans
                WHERE id = %s
                  AND tenant_id = %s
                """,
                (loan_id, tenant_id),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Jashflow loan not found.")
            cursor.execute(
                """
                INSERT INTO jashflow_transactions (
                    loan_id, transaction_date, transaction_type, amount, description, created_by_user_id
                )
                VALUES (%s, %s, 'payment', %s, %s, %s)
                RETURNING id
                """,
                (loan_id, payment_date, amount, description, user["id"]),
            )
            transaction_id = cursor.fetchone()["id"]
            cursor.execute(
                """
                UPDATE jashflow_loans
                SET updated_at = %s
                WHERE id = %s
                """,
                (utcnow(), loan_id),
            )
        connection.commit()

    record_audit_event(
        "jashflow_loan",
        str(loan_id),
        "jashflow.payment_added",
        {"transaction_id": str(transaction_id), "amount": float(amount), "payment_date": payment_date.isoformat()},
        user["id"],
    )
    return jashflow_payload(user)


def save_jashflow_settings(user: dict, payload: dict) -> dict:
    tenant_id = _jashflow_tenant_id(user)
    customer_id = str(payload.get("invoiceContactCustomerId") or payload.get("customerId") or "").strip()
    account_code = str(payload.get("interestAccountCode") or "").strip()
    if not customer_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose the Xero contact used for Jashflow interest invoices.")
    if not account_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the Xero interest received account code.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT name, xero_contact_id
                FROM customers
                WHERE id = %s
                  AND tenant_id = %s
                """,
                (customer_id, tenant_id),
            )
            customer = cursor.fetchone()
            if customer is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Selected invoice contact was not found in this Xero tenant.")
            xero_contact_id = customer.get("xero_contact_id")
            if not xero_contact_id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected invoice contact is missing a Xero contact id.")
            cursor.execute(
                """
                INSERT INTO jashflow_settings (
                    tenant_id, invoice_contact_id, invoice_contact_name,
                    interest_account_code, updated_by_user_id, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE
                SET invoice_contact_id = EXCLUDED.invoice_contact_id,
                    invoice_contact_name = EXCLUDED.invoice_contact_name,
                    interest_account_code = EXCLUDED.interest_account_code,
                    updated_by_user_id = EXCLUDED.updated_by_user_id,
                    updated_at = EXCLUDED.updated_at
                """,
                (tenant_id, xero_contact_id, customer.get("name") or "", account_code, user["id"], utcnow(), utcnow()),
            )
        connection.commit()

    record_audit_event(
        "jashflow_settings",
        tenant_id,
        "jashflow.settings_saved",
        {"invoice_contact_name": customer.get("name") or "", "interest_account_code": account_code},
        user["id"],
    )
    return jashflow_payload(user)


def _xlsx_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _xlsx_cell(reference: str, value) -> str:
    if isinstance(value, (int, float, Decimal)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    text = xml_escape(str(value if value is not None else ""))
    return f'<c r="{reference}" t="inlineStr"><is><t>{text}</t></is></c>'


def _build_jashflow_interest_workbook(lines: list[dict], period_end: date, total: Decimal) -> bytes:
    rows = [
        ["Client", "Xero Contact ID", "Loan ID", "Period End", "Accrued Interest", "Previously Posted", "Posting Now", "Loan Balance"],
        *[
            [
                line["customerName"],
                line["xeroContactId"],
                line["loanId"],
                period_end.isoformat(),
                f"{line['accruedInterest']:.2f}",
                f"{line['previouslyPosted']:.2f}",
                f"{line['interestAmount']:.2f}",
                f"{line['balance']:.2f}",
            ]
            for line in lines
        ],
        ["", "", "", "Total", "", "", f"{total:.2f}", ""],
    ]
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = [
            _xlsx_cell(f"{_xlsx_column_name(column_index)}{row_index}", value)
            for column_index, value in enumerate(row, start=1)
        ]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    worksheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>'
        f'{"".join(sheet_rows)}'
        '</sheetData>'
        '</worksheet>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as workbook:
        workbook.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        )
        workbook.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        )
        workbook.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Interest" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>',
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>',
        )
        workbook.writestr("xl/worksheets/sheet1.xml", worksheet_xml)
    return buffer.getvalue()


async def post_jashflow_interest_invoice(user: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    tenant_id = _jashflow_tenant_id(user)
    period_end = _parse_iso_date(payload.get("periodEndDate") or utcnow().date(), "Period end date")
    current_payload = jashflow_payload(user)
    settings = current_payload.get("settings") or {}
    invoice_contact_id = settings.get("invoiceContactId") or ""
    invoice_contact_name = settings.get("invoiceContactName") or ""
    account_code = settings.get("interestAccountCode") or ""
    if not invoice_contact_id or not account_code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Save the Jashflow interest invoice contact and account code before posting interest.")

    lines = []
    for loan in current_payload.get("loans") or []:
        if loan.get("status") != "active":
            continue
        interest_amount = _money(loan.get("uninvoicedInterest"))
        if interest_amount < Decimal("0.01"):
            continue
        lines.append(
            {
                "loanId": loan["id"],
                "customerId": loan["customerId"],
                "customerName": loan["customerName"],
                "xeroContactId": loan.get("xeroContactId") or "",
                "accruedInterest": _money(loan.get("accruedInterest")),
                "previouslyPosted": _money(loan.get("invoicedInterest")),
                "interestAmount": interest_amount,
                "balance": _money(loan.get("balance")),
            }
        )
    if not lines:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="There is no uninvoiced Jashflow interest to post.")

    total = sum((line["interestAmount"] for line in lines), Decimal("0.00")).quantize(Decimal("0.01"))
    connection_row = get_xero_connection_for_user(user["id"])
    description = (
        f"Jashflow interest earned to {_invoice_date_description(period_end)}. "
        f"Supporting client breakdown is attached to this invoice."
    )
    invoice_payload = {
        "Type": "ACCREC",
        "Contact": {"ContactID": invoice_contact_id},
        "Date": period_end.isoformat(),
        "DueDate": period_end.isoformat(),
        "Reference": f"Jashflow interest to {period_end.isoformat()}",
        "LineAmountTypes": "NoTax",
        "Status": "AUTHORISED",
        "LineItems": [
            {
                "Description": description,
                "Quantity": 1,
                "UnitAmount": float(total),
                "AccountCode": account_code,
                "TaxType": "NONE",
            }
        ],
    }
    idempotency_seed = json.dumps(
        {"periodEnd": period_end.isoformat(), "lines": [(line["loanId"], str(line["interestAmount"])) for line in lines]},
        sort_keys=True,
    )
    idempotency_key = f"jashflow-interest-{hashlib.sha256(idempotency_seed.encode()).hexdigest()[:32]}"
    xero_response = await create_sales_invoice(connection_row, invoice_payload, idempotency_key=idempotency_key)
    created_invoice = ((xero_response or {}).get("Invoices") or [{}])[0]
    invoice_id = created_invoice.get("InvoiceID") or created_invoice.get("ID") or ""
    invoice_number = created_invoice.get("InvoiceNumber") or ""
    if not invoice_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Xero created the Jashflow interest invoice but did not return an invoice id.")

    attachment_filename = f"jashflow-interest-{period_end.isoformat()}.xlsx"
    attachment_error = ""
    workbook_bytes = _build_jashflow_interest_workbook(lines, period_end, total)
    try:
        await attach_file_to_invoice(
            connection_row,
            invoice_id,
            attachment_filename,
            workbook_bytes,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        attachment_error = _sync_error_message(exc)
        logger.exception("Unable to attach Jashflow interest workbook to Xero invoice %s", invoice_id)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO jashflow_interest_post_batches (
                    tenant_id, status, xero_invoice_id, xero_invoice_number,
                    invoice_contact_id, invoice_contact_name, interest_account_code,
                    period_end_date, total_interest_amount, attachment_filename,
                    error_message, created_by_user_id, created_at, completed_at
                )
                VALUES (%s, 'completed', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    invoice_id,
                    invoice_number,
                    invoice_contact_id,
                    invoice_contact_name,
                    account_code,
                    period_end,
                    total,
                    attachment_filename,
                    attachment_error,
                    user["id"],
                    utcnow(),
                    utcnow(),
                ),
            )
            batch_id = cursor.fetchone()["id"]
            for line in lines:
                cursor.execute(
                    """
                    INSERT INTO jashflow_interest_post_lines (
                        batch_id, loan_id, customer_id, period_end_date,
                        accrued_interest_amount, previously_posted_amount,
                        interest_amount, balance_after_interest, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        batch_id,
                        line["loanId"],
                        line["customerId"],
                        period_end,
                        line["accruedInterest"],
                        line["previouslyPosted"],
                        line["interestAmount"],
                        line["balance"],
                        utcnow(),
                    ),
                )
        connection.commit()

    record_audit_event(
        "jashflow_interest",
        str(batch_id),
        "jashflow.interest_posted",
        {
            "xero_invoice_id": invoice_id,
            "xero_invoice_number": invoice_number,
            "total_interest_amount": float(total),
            "line_count": len(lines),
            "attachment_filename": attachment_filename,
            "attachment_error": attachment_error,
        },
        user["id"],
    )
    return {
        "interestPost": {
            "id": str(batch_id),
            "xeroInvoiceId": invoice_id,
            "xeroInvoiceNumber": invoice_number,
            "totalInterestAmount": float(total),
            "lineCount": len(lines),
            "attachmentFilename": attachment_filename,
            "attachmentError": attachment_error,
        },
        "jashflow": jashflow_payload(user),
    }


ME_REPORT_TAX_RATE = Decimal("0.19")
ME_REPORT_CATEGORIES = [
    {"group": "Income", "items": ["Sales", "Other income", "Bank interest", "Grants", "Tax refunds", "Directors' income items needing review"]},
    {"group": "Normal allowable expenses", "items": ["Software", "Subscriptions", "Accountancy fees", "Office costs", "Telephone and internet", "Staff wages", "Employer pension", "Employer NIC", "Insurance", "Travel", "Training", "Bank charges"]},
    {"group": "Disallowable or partly disallowable", "items": ["Client entertaining", "Fines and penalties", "Depreciation", "Non-business expenses", "Donations needing review", "Private use items", "Legal fees needing review"]},
    {"group": "Capital allowances", "items": ["Plant and machinery", "Computer equipment", "Office equipment", "Fixtures and fittings", "Vans", "Cars", "Special rate pool items", "Assets needing review"]},
    {"group": "Balance sheet", "items": ["Bank", "Trade debtors", "Trade creditors", "VAT", "PAYE/NIC", "Corporation tax creditor", "Director loan account", "Dividends", "Retained earnings", "Share capital"]},
    {"group": "Special tax categories", "items": ["R&D costs", "Losses", "Accruals", "Prepayments", "Associated company adjustment", "s455/director loan risk", "Illegal dividend risk"]},
]


def _me_report_xero_connection(user: dict, client: dict | None = None) -> dict:
    if client and client.get("xero_connection_id"):
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM xero_connections WHERE id = %s AND user_id = %s",
                    (client["xero_connection_id"], user["id"]),
                )
                row = cursor.fetchone()
            connection.commit()
        if row:
            return row
    return get_xero_connection_for_user(user["id"])


def _me_report_empty_summary() -> dict:
    return {
        "clientCount": 0,
        "green": 0,
        "amber": 0,
        "red": 0,
        "reportsGenerated": 0,
        "estimatedCorporationTax": 0,
        "dividendCapacity": 0,
        "dlaRedCount": 0,
    }


def _serialize_me_report_sync_run(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "clientId": str(row.get("client_id") or ""),
        "status": row.get("status") or "",
        "currentStep": row.get("current_step") or "",
        "summary": row.get("summary") or "",
        "errorMessage": row.get("error_message") or "",
        "progress": int(row.get("progress") or 0),
        "recordsSynced": int(row.get("records_synced") or 0),
        "createdAt": _iso(row.get("created_at")) or "",
        "startedAt": _iso(row.get("started_at")) or "",
        "heartbeatAt": _iso(row.get("heartbeat_at")) or "",
        "completedAt": _iso(row.get("completed_at")) or "",
        "isActive": (row.get("status") or "") in ACTIVE_SYNC_STATUSES,
    }


def _serialize_me_report_client(row: dict, mappings: list[dict], reviews: list[dict], exceptions: list[dict], reports: list[dict]) -> dict:
    latest_review = reviews[0] if reviews else None
    review_summary = latest_review.get("summary") if latest_review else {}
    if isinstance(review_summary, str):
        try:
            review_summary = json.loads(review_summary)
        except ValueError:
            review_summary = {}
    open_exceptions = [item for item in exceptions if (item.get("status") or "open") == "open"]
    traffic_light = (latest_review or {}).get("traffic_light") or ("red" if any((item.get("severity") or "") == "red" for item in open_exceptions) else "amber")
    return {
        "id": str(row["id"]),
        "clientName": row.get("client_name") or "",
        "internalClientOwner": row.get("internal_client_owner") or "",
        "bookkeepingFrequency": row.get("bookkeeping_frequency") or "Monthly",
        "reportRecipientEmail": row.get("report_recipient_email") or "",
        "yearEndMonth": int(row.get("year_end_month") or 3),
        "xeroConnectionStatus": row.get("xero_connection_status") or "not_connected",
        "xeroTenantName": row.get("xero_tenant_name") or "",
        "lastSyncAt": _iso(row.get("last_sync_at")) or "",
        "lastCalculatedAt": _iso(row.get("last_calculated_at")) or "",
        "lastReportAt": _iso(row.get("last_report_at")) or "",
        "createdAt": _iso(row.get("created_at")) or "",
        "trafficLight": traffic_light,
        "summary": review_summary or {},
        "mappings": [
            {
                "id": str(mapping["id"]),
                "xeroAccountId": mapping.get("xero_account_id") or "",
                "accountCode": mapping.get("account_code") or "",
                "accountName": mapping.get("account_name") or "",
                "accountType": mapping.get("account_type") or "",
                "suggestedTreatment": mapping.get("suggested_treatment") or "",
                "taxTreatment": mapping.get("tax_treatment") or "",
                "category": mapping.get("category") or "",
                "confidence": int(mapping.get("confidence") or 0),
                "reviewRequired": bool(mapping.get("review_required")),
                "status": mapping.get("status") or "suggested",
                "note": mapping.get("note") or "",
                "reason": mapping.get("reason") or "",
            }
            for mapping in mappings
        ],
        "reviews": [
            {
                "id": str(review["id"]),
                "periodStart": _iso(review.get("period_start")) or "",
                "periodEnd": _iso(review.get("period_end")) or "",
                "status": review.get("status") or "",
                "trafficLight": review.get("traffic_light") or "amber",
                "summary": review.get("summary") if isinstance(review.get("summary"), dict) else {},
                "createdAt": _iso(review.get("created_at")) or "",
            }
            for review in reviews
        ],
        "exceptions": [
            {
                "id": str(item["id"]),
                "reviewId": str(item.get("review_id") or ""),
                "severity": item.get("severity") or "amber",
                "title": item.get("title") or "",
                "detail": item.get("detail") or "",
                "suggestedAction": item.get("suggested_action") or "",
                "actionPayload": item.get("action_payload") if isinstance(item.get("action_payload"), dict) else {},
                "status": item.get("status") or "open",
                "note": item.get("note") or "",
                "createdAt": _iso(item.get("created_at")) or "",
            }
            for item in exceptions
        ],
        "reports": [
            {
                "id": str(report["id"]),
                "reviewId": str(report.get("review_id") or ""),
                "status": report.get("status") or "draft",
                "recipientEmail": report.get("recipient_email") or "",
                "commentary": report.get("commentary") or "",
                "createdAt": _iso(report.get("created_at")) or "",
            }
            for report in reports
        ],
    }


def _me_report_client_row(user: dict, client_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM me_report_clients WHERE id = %s AND user_id = %s",
                (client_id, user["id"]),
            )
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ME Report client not found.")
    return row


def _me_report_client_payloads(user: dict) -> tuple[list[dict], dict | None, dict | None]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM me_report_clients
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user["id"],),
            )
            client_rows = cursor.fetchall()
            client_ids = [row["id"] for row in client_rows]
            mappings_by_client = defaultdict(list)
            reviews_by_client = defaultdict(list)
            exceptions_by_client = defaultdict(list)
            reports_by_client = defaultdict(list)
            if client_ids:
                cursor.execute(
                    """
                    SELECT *
                    FROM me_report_account_mappings
                    WHERE client_id = ANY(%s)
                    ORDER BY account_code ASC, account_name ASC
                    """,
                    (client_ids,),
                )
                for row in cursor.fetchall():
                    mappings_by_client[row["client_id"]].append(row)
                cursor.execute(
                    """
                    SELECT *
                    FROM me_report_reviews
                    WHERE client_id = ANY(%s)
                    ORDER BY period_end DESC, created_at DESC
                    """,
                    (client_ids,),
                )
                for row in cursor.fetchall():
                    reviews_by_client[row["client_id"]].append(row)
                cursor.execute(
                    """
                    SELECT *
                    FROM me_report_exceptions
                    WHERE client_id = ANY(%s)
                    ORDER BY CASE severity WHEN 'red' THEN 0 WHEN 'amber' THEN 1 ELSE 2 END, created_at DESC
                    """,
                    (client_ids,),
                )
                for row in cursor.fetchall():
                    exceptions_by_client[row["client_id"]].append(row)
                cursor.execute(
                    """
                    SELECT *
                    FROM me_report_reports
                    WHERE client_id = ANY(%s)
                    ORDER BY created_at DESC
                    """,
                    (client_ids,),
                )
                for row in cursor.fetchall():
                    reports_by_client[row["client_id"]].append(row)
            cursor.execute(
                """
                SELECT *
                FROM me_report_sync_runs
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user["id"],),
            )
            latest_run = cursor.fetchone()
            cursor.execute(
                """
                SELECT *
                FROM me_report_sync_runs
                WHERE user_id = %s
                  AND status IN ('queued', 'running')
                ORDER BY COALESCE(heartbeat_at, started_at, created_at) DESC
                LIMIT 1
                """,
                (user["id"],),
            )
            active_run = cursor.fetchone()
        connection.commit()
    clients = [
        _serialize_me_report_client(
            row,
            mappings_by_client.get(row["id"], []),
            reviews_by_client.get(row["id"], [])[:8],
            exceptions_by_client.get(row["id"], [])[:50],
            reports_by_client.get(row["id"], [])[:12],
        )
        for row in client_rows
    ]
    return clients, active_run, latest_run


def me_report_payload(user: dict) -> dict:
    clients, active_run, latest_run = _me_report_client_payloads(user)
    summary = _me_report_empty_summary()
    summary["clientCount"] = len(clients)
    for client in clients:
        light = client.get("trafficLight") or "amber"
        if light in summary:
            summary[light] += 1
        latest_summary = client.get("summary") or {}
        summary["estimatedCorporationTax"] += float(latest_summary.get("estimatedCorporationTax") or 0)
        summary["dividendCapacity"] += float(latest_summary.get("dividendCapacity") or 0)
        if str(latest_summary.get("dlaStatus") or "").lower() == "red":
            summary["dlaRedCount"] += 1
        summary["reportsGenerated"] += len(client.get("reports") or [])
    try:
        xero_connection = get_xero_connection_for_user(user["id"])
    except HTTPException:
        xero_connection = None
    return {
        "summary": summary,
        "clients": clients,
        "treatmentCategories": ME_REPORT_CATEGORIES,
        "xero": {
            "connected": bool(xero_connection),
            "tenantName": xero_connection.get("tenant_name") if xero_connection else "",
            "tenantId": xero_connection.get("tenant_id") if xero_connection else "",
        },
        "syncRun": _serialize_me_report_sync_run(active_run or latest_run),
        "activeSyncRun": _serialize_me_report_sync_run(active_run),
    }


def create_me_report_client(user: dict, payload: dict) -> dict:
    client_name = str(payload.get("clientName") or "").strip()
    if not client_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Client name is required.")
    owner = str(payload.get("internalClientOwner") or "").strip()
    frequency = str(payload.get("bookkeepingFrequency") or "Monthly").strip() or "Monthly"
    recipient = str(payload.get("reportRecipientEmail") or "").strip()
    try:
        year_end_month = int(payload.get("yearEndMonth") or 3)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Year end month must be a number from 1 to 12.") from exc
    year_end_month = min(12, max(1, year_end_month))
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO me_report_clients (
                    user_id, client_name, internal_client_owner,
                    bookkeeping_frequency, report_recipient_email,
                    year_end_month, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (user["id"], client_name, owner, frequency, recipient, year_end_month, utcnow(), utcnow()),
            )
            client_id = cursor.fetchone()["id"]
        connection.commit()
    record_audit_event("me_report_client", str(client_id), "me_report.client_created", {"client_name": client_name}, user["id"])
    return me_report_payload(user)


def connect_me_report_client_to_current_xero(user: dict, client_id: str) -> dict:
    _me_report_client_row(user, client_id)
    connection_row = get_xero_connection_for_user(user["id"])
    now = utcnow()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE me_report_clients
                SET xero_connection_id = %s,
                    xero_tenant_id = %s,
                    xero_tenant_name = %s,
                    xero_connection_status = 'connected',
                    updated_at = %s
                WHERE id = %s
                  AND user_id = %s
                """,
                (
                    connection_row.get("id"),
                    connection_row.get("tenant_id"),
                    connection_row.get("tenant_name") or "Xero organisation",
                    now,
                    client_id,
                    user["id"],
                ),
            )
        connection.commit()
    record_audit_event(
        "me_report_client",
        client_id,
        "me_report.xero_connected",
        {"tenant_id": connection_row.get("tenant_id"), "tenant_name": connection_row.get("tenant_name")},
        user["id"],
    )
    return me_report_payload(user)


def _me_treatment_for_account(account: dict) -> dict:
    name = str(account.get("Name") or account.get("name") or "").lower()
    code = str(account.get("Code") or account.get("code") or "")
    account_type = str(account.get("Type") or account.get("type") or "")
    text = f"{code} {name} {account_type}".lower()
    rules = [
        (("client entertaining", "entertain"), ("Client entertaining", "Add back for CT", "Client entertaining", 99, True, "Entertainment is normally disallowable for corporation tax.")),
        (("fine", "penalt"), ("Fines and penalties", "Add back for CT", "Fines and penalties", 98, True, "Fines and penalties are normally disallowable.")),
        (("depreciation",), ("Depreciation", "Add back for CT", "Depreciation", 99, False, "Depreciation is added back before capital allowance claims.")),
        (("computer", "laptop", "equipment"), ("Computer equipment", "Review for capital allowances", "Computer equipment", 92, True, "Asset-style account name suggests a capital allowances review.")),
        (("director loan", "directors loan", "dla"), ("Director loan account", "Include in DLA engine", "Director loan account", 100, False, "Director loan account identified from account name.")),
        (("dividend",), ("Dividends", "Include in dividend engine", "Dividends", 100, False, "Dividend account identified from account name.")),
        (("corporation tax", "corp tax", "ct creditor"), ("Corporation tax creditor", "Compare provision to estimated CT", "Corporation tax creditor", 96, False, "Corporation tax account identified from account name.")),
        (("vat",), ("VAT", "Balance sheet tax creditor/debtor", "VAT", 95, False, "VAT balance sheet account identified.")),
        (("paye", "nic", "hmrc payroll"), ("PAYE/NIC", "Balance sheet tax creditor", "PAYE/NIC", 95, False, "Payroll tax creditor account identified.")),
        (("retained earnings", "profit and loss account"), ("Retained earnings", "Use in dividend capacity engine", "Retained earnings", 96, False, "Retained reserves account identified.")),
        (("motor", "vehicle", "fuel"), ("Motor Expenses", "Review if private use risk", "Private use items", 75, True, "Motor expenses can carry private use risk.")),
        (("legal",), ("Legal fees needing review", "Review deductibility", "Legal fees needing review", 72, True, "Legal fees can be allowable, capital or disallowable depending on the matter.")),
        (("sales", "revenue", "turnover"), ("Sales", "Taxable trading income", "Sales", 94, False, "Income account identified.")),
        (("bank interest", "interest received"), ("Bank interest", "Taxable income", "Bank interest", 90, False, "Interest income account identified.")),
        (("software", "subscription"), ("Software", "Allowable expense", "Software", 90, False, "Normal software or subscription cost.")),
        (("accountancy", "bookkeeping"), ("Accountancy fees", "Allowable expense", "Accountancy fees", 90, False, "Accountancy costs are normally allowable.")),
        (("wages", "salary", "payroll"), ("Staff wages", "Allowable expense", "Staff wages", 90, False, "Payroll cost account identified.")),
        (("bank charges",), ("Bank charges", "Allowable expense", "Bank charges", 90, False, "Bank charges are normally allowable.")),
        (("trade debtor", "accounts receivable", "debtors"), ("Trade debtors", "Balance sheet working capital", "Trade debtors", 90, False, "Debtor control account identified.")),
        (("trade creditor", "accounts payable", "creditors"), ("Trade creditors", "Balance sheet working capital", "Trade creditors", 90, False, "Creditor control account identified.")),
        (("bank", "current account"), ("Bank", "Bank balance", "Bank", 88, False, "Bank account identified.")),
    ]
    for needles, result in rules:
        if any(needle in text for needle in needles):
            treatment, tax_treatment, category, confidence, review_required, reason = result
            return {
                "suggestedTreatment": treatment,
                "taxTreatment": tax_treatment,
                "category": category,
                "confidence": confidence,
                "reviewRequired": review_required,
                "reason": reason,
            }
    if account_type.upper() in ("REVENUE", "SALES"):
        return {"suggestedTreatment": "Sales", "taxTreatment": "Taxable trading income", "category": "Sales", "confidence": 84, "reviewRequired": False, "reason": "Xero account type is revenue."}
    if account_type.upper() in ("EXPENSE", "DIRECTCOSTS", "OVERHEADS"):
        return {"suggestedTreatment": "Normal allowable expense", "taxTreatment": "Review standard deductibility", "category": "Office costs", "confidence": 68, "reviewRequired": True, "reason": "Expense account needs staff confirmation before tax treatment is relied on."}
    return {"suggestedTreatment": "Needs review", "taxTreatment": "Staff review required", "category": "Directors' income items needing review", "confidence": 50, "reviewRequired": True, "reason": "No confident Jaccountancy treatment rule matched this account."}


def _money_from_report_cell(value) -> Decimal:
    text = str(value if value is not None else "").strip()
    if not text:
        return Decimal("0.00")
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace("£", "").replace("%", "")
    try:
        amount = Decimal(text)
    except Exception:
        return Decimal("0.00")
    return -amount if negative else amount


def _xero_report_lines(report_payload: dict) -> list[dict]:
    lines = []

    def visit(rows):
        for row in rows or []:
            cells = row.get("Cells") or row.get("cells") or []
            values = [cell.get("Value") or cell.get("value") or "" for cell in cells if isinstance(cell, dict)]
            if values:
                label = str(values[0] or "").strip()
                amounts = [_money_from_report_cell(value) for value in values[1:]]
                lines.append({"label": label, "amounts": amounts, "raw": row})
            visit(row.get("Rows") or row.get("rows") or [])

    for report in report_payload.get("Reports") or report_payload.get("reports") or []:
        visit(report.get("Rows") or report.get("rows") or [])
    return lines


def _report_amount(lines: list[dict], keywords: tuple[str, ...], fallback: Decimal = Decimal("0.00")) -> Decimal:
    for line in lines:
        label = (line.get("label") or "").lower()
        if all(keyword in label for keyword in keywords):
            amounts = [amount for amount in line.get("amounts") or [] if amount is not None]
            if amounts:
                return _money(amounts[-1])
    return fallback


def _normalise_contact_match_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _xero_contact_name(contact: dict) -> str:
    return str(contact.get("Name") or contact.get("name") or "").strip()


def _xero_contact_id(contact: dict) -> str:
    return str(contact.get("ContactID") or contact.get("contactID") or contact.get("ContactId") or "").strip()


def _find_duplicate_contact_candidates(contacts: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for contact in contacts:
        name = _xero_contact_name(contact)
        contact_id = _xero_contact_id(contact)
        key = _normalise_contact_match_name(name)
        if key and contact_id:
            grouped[key].append({"name": name, "contactId": contact_id})
    candidates = []
    for rows in grouped.values():
        distinct_names = {row["name"].lower() for row in rows}
        distinct_ids = {row["contactId"] for row in rows}
        if len(distinct_names) > 1 and len(distinct_ids) > 1:
            keep = rows[0]
            for duplicate in rows[1:4]:
                candidates.append({
                    "keepContactId": keep["contactId"],
                    "keepName": keep["name"],
                    "mergeContactId": duplicate["contactId"],
                    "mergeName": duplicate["name"],
                })
    return candidates[:10]


def _xero_bank_transaction_amount(transaction: dict) -> Decimal:
    if transaction.get("Total") is not None:
        return abs(_money(transaction.get("Total")))
    return abs(sum(_money(item.get("LineAmount")) for item in transaction.get("LineItems") or []))


def _xero_invoice_amount_due(invoice: dict) -> Decimal:
    return abs(_money(invoice.get("AmountDue") if invoice.get("AmountDue") is not None else invoice.get("Total")))


def _xero_invoice_contact_id(invoice: dict) -> str:
    contact = invoice.get("Contact") or {}
    return str(contact.get("ContactID") or contact.get("contactID") or "").strip()


def _find_duplicate_spend_bill_candidates(bank_transactions: list[dict], bills: list[dict]) -> list[dict]:
    candidates = []
    open_bills = [
        {
            "invoiceId": bill.get("InvoiceID") or bill.get("invoiceID") or "",
            "invoiceNumber": bill.get("InvoiceNumber") or bill.get("Reference") or "Bill",
            "contactId": _xero_invoice_contact_id(bill),
            "amountDue": _xero_invoice_amount_due(bill),
        }
        for bill in bills
        if _xero_invoice_amount_due(bill) > Decimal("0.00")
    ]
    for transaction in bank_transactions:
        transaction_type = str(transaction.get("Type") or "").upper()
        if transaction_type not in ("SPEND", "SPEND-MONEY", "SPEND MONEY"):
            continue
        amount = _xero_bank_transaction_amount(transaction)
        contact_id = _xero_invoice_contact_id({"Contact": transaction.get("Contact") or {}})
        for bill in open_bills:
            if bill["contactId"] and contact_id and bill["contactId"] != contact_id:
                continue
            if abs(bill["amountDue"] - amount) <= Decimal("1.00"):
                candidates.append({
                    "bankTransactionId": transaction.get("BankTransactionID") or "",
                    "bankTransactionReference": transaction.get("Reference") or transaction.get("Url") or "Spend money transaction",
                    "billId": bill["invoiceId"],
                    "billNumber": bill["invoiceNumber"],
                    "amount": float(amount),
                })
                break
    return candidates[:12]


def _asset_book_value(asset: dict) -> Decimal:
    for key in ("BookValue", "bookValue", "CurrentValue", "currentValue", "PurchasePrice", "purchasePrice"):
        if asset.get(key) is not None:
            return _money(asset.get(key))
    return Decimal("0.00")


def _asset_register_total(asset_payload: dict) -> Decimal | None:
    if asset_payload.get("_error"):
        return None
    rows = asset_payload.get("Items") or asset_payload.get("Assets") or asset_payload.get("assets") or []
    return sum((_asset_book_value(row) for row in rows), Decimal("0.00"))


def _balance_sheet_fixed_asset_total(lines: list[dict]) -> Decimal:
    fixed_asset_keywords = (("fixed", "asset"), ("plant",), ("equipment",), ("computer",), ("fixtures",), ("vehicle",), ("motor",))
    total = Decimal("0.00")
    for line in lines:
        label = (line.get("label") or "").lower()
        if any(all(keyword in label for keyword in keywords) for keywords in fixed_asset_keywords):
            amounts = line.get("amounts") or []
            if amounts:
                total += _money(amounts[-1])
    return abs(_money(total))


async def _me_xero_optional_get(connection_row: dict, url: str, params: dict | None = None) -> dict:
    try:
        return await xero_api_get(connection_row, url, params=params)
    except Exception as exc:
        return {"_error": _sync_error_message(exc), "_type": exc.__class__.__name__}


def _update_me_report_sync_run(sync_run_id: str, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = %s" for key in fields)
    values = list(fields.values())
    values.append(sync_run_id)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE me_report_sync_runs SET {assignments} WHERE id = %s", values)
        connection.commit()


def request_me_report_sync_run(user: dict, client_id: str) -> tuple[dict, bool]:
    client = _me_report_client_row(user, client_id)
    if client.get("xero_connection_status") != "connected":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Connect this ME Report client to Xero before syncing.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM me_report_sync_runs
                WHERE user_id = %s
                  AND client_id = %s
                  AND status IN ('queued', 'running')
                ORDER BY COALESCE(heartbeat_at, started_at, created_at) DESC
                LIMIT 1
                """,
                (user["id"], client_id),
            )
            active = cursor.fetchone()
            if active:
                connection.commit()
                return active, False
            cursor.execute(
                """
                INSERT INTO me_report_sync_runs (
                    client_id, user_id, status, current_step,
                    summary, progress, heartbeat_at, created_at
                )
                VALUES (%s, %s, 'queued', 'Queued', 'ME Report Xero sync queued.', 2, %s, %s)
                RETURNING *
                """,
                (client_id, user["id"], utcnow(), utcnow()),
            )
            row = cursor.fetchone()
        connection.commit()
    return row, True


def get_me_report_sync_run(user: dict, sync_run_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM me_report_sync_runs WHERE id = %s AND user_id = %s", (sync_run_id, user["id"]))
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ME Report sync run not found.")
    return row


def active_me_report_sync_run_for_user(user: dict | None) -> dict | None:
    if not user or not user.get("id"):
        return None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM me_report_sync_runs
                WHERE user_id = %s
                  AND status IN ('queued', 'running')
                ORDER BY COALESCE(heartbeat_at, started_at, created_at) DESC
                LIMIT 1
                """,
                (user["id"],),
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def serialize_me_report_sync_run(row: dict | None) -> dict | None:
    return _serialize_me_report_sync_run(row)


def run_me_report_sync_job(user: dict, sync_run_id: str) -> None:
    try:
        asyncio.run(run_me_report_sync(user, sync_run_id))
    except Exception as exc:
        logger.exception("Background ME Report sync failed")
        _update_me_report_sync_run(
            sync_run_id,
            status="failed",
            current_step="ME Report sync failed",
            summary="ME Report sync failed before it could complete.",
            error_message=_sync_error_message(exc),
            progress=100,
            heartbeat_at=utcnow(),
            completed_at=utcnow(),
        )


async def run_me_report_sync(user: dict, sync_run_id: str) -> dict:
    sync_run = get_me_report_sync_run(user, sync_run_id)
    client = _me_report_client_row(user, str(sync_run["client_id"]))
    connection_row = _me_report_xero_connection(user, client)
    today = utcnow().date()
    period_start = date(today.year, today.month, 1)
    period_end = today
    now = utcnow()
    _update_me_report_sync_run(
        sync_run_id,
        status="running",
        current_step="Syncing chart of accounts",
        summary="Reading account codes and names from Xero.",
        progress=8,
        started_at=now,
        heartbeat_at=now,
    )
    accounts_payload = await xero_api_get(connection_row, "https://api.xero.com/api.xro/2.0/Accounts")
    accounts = accounts_payload.get("Accounts") or accounts_payload.get("accounts") or []
    with get_connection() as db:
        with db.cursor() as cursor:
            for account in accounts:
                suggestion = _me_treatment_for_account(account)
                cursor.execute(
                    """
                    INSERT INTO me_report_account_mappings (
                        client_id, xero_account_id, account_code, account_name,
                        account_type, suggested_treatment, tax_treatment, category,
                        confidence, review_required, status, reason, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'suggested', %s, %s, %s)
                    ON CONFLICT (client_id, account_code) DO UPDATE
                    SET xero_account_id = EXCLUDED.xero_account_id,
                        account_name = EXCLUDED.account_name,
                        account_type = EXCLUDED.account_type,
                        suggested_treatment = CASE
                            WHEN me_report_account_mappings.status IN ('approved', 'amended') THEN me_report_account_mappings.suggested_treatment
                            ELSE EXCLUDED.suggested_treatment
                        END,
                        tax_treatment = CASE
                            WHEN me_report_account_mappings.status IN ('approved', 'amended') THEN me_report_account_mappings.tax_treatment
                            ELSE EXCLUDED.tax_treatment
                        END,
                        category = CASE
                            WHEN me_report_account_mappings.status IN ('approved', 'amended') THEN me_report_account_mappings.category
                            ELSE EXCLUDED.category
                        END,
                        confidence = CASE
                            WHEN me_report_account_mappings.status IN ('approved', 'amended') THEN me_report_account_mappings.confidence
                            ELSE EXCLUDED.confidence
                        END,
                        review_required = CASE
                            WHEN me_report_account_mappings.status IN ('approved', 'amended') THEN me_report_account_mappings.review_required
                            ELSE EXCLUDED.review_required
                        END,
                        reason = CASE
                            WHEN me_report_account_mappings.status IN ('approved', 'amended') THEN me_report_account_mappings.reason
                            ELSE EXCLUDED.reason
                        END,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        client["id"],
                        account.get("AccountID") or account.get("accountID") or "",
                        account.get("Code") or account.get("code") or "",
                        account.get("Name") or account.get("name") or "",
                        account.get("Type") or account.get("type") or "",
                        suggestion["suggestedTreatment"],
                        suggestion["taxTreatment"],
                        suggestion["category"],
                        suggestion["confidence"],
                        suggestion["reviewRequired"],
                        suggestion["reason"],
                        utcnow(),
                        utcnow(),
                    ),
                )
        db.commit()

    _update_me_report_sync_run(
        sync_run_id,
        current_step="Fetching financial reports",
        summary="Reading profit and loss, balance sheet, trial balance, bank transactions and journals.",
        progress=32,
        records_synced=len(accounts),
        heartbeat_at=utcnow(),
    )
    report_params = {"fromDate": period_start.isoformat(), "toDate": period_end.isoformat()}
    last_12_months_start = period_end - timedelta(days=365)
    year_end_month = int(client.get("year_end_month") or 3)
    ytd_start_month = 1 if year_end_month == 12 else year_end_month + 1
    ytd_year = period_end.year if period_end.month >= ytd_start_month else period_end.year - 1
    ytd_start = date(ytd_year, ytd_start_month, 1)
    profit_loss_payload = await _me_xero_optional_get(connection_row, "https://api.xero.com/api.xro/2.0/Reports/ProfitAndLoss", report_params)
    ytd_profit_loss_payload = await _me_xero_optional_get(
        connection_row,
        "https://api.xero.com/api.xro/2.0/Reports/ProfitAndLoss",
        {"fromDate": ytd_start.isoformat(), "toDate": period_end.isoformat()},
    )
    annual_profit_loss_payload = await _me_xero_optional_get(
        connection_row,
        "https://api.xero.com/api.xro/2.0/Reports/ProfitAndLoss",
        {"fromDate": last_12_months_start.isoformat(), "toDate": period_end.isoformat()},
    )
    balance_sheet_payload = await _me_xero_optional_get(connection_row, "https://api.xero.com/api.xro/2.0/Reports/BalanceSheet", {"date": period_end.isoformat()})
    trial_balance_payload = await _me_xero_optional_get(connection_row, "https://api.xero.com/api.xro/2.0/Reports/TrialBalance", {"date": period_end.isoformat()})
    bank_transactions_payload = await _me_xero_optional_get(connection_row, "https://api.xero.com/api.xro/2.0/BankTransactions", {"page": 1, "pageSize": 100})
    journals_payload = await _me_xero_optional_get(connection_row, "https://api.xero.com/api.xro/2.0/Journals", {"offset": 0})
    fixed_assets_payload = await _me_xero_optional_get(connection_row, "https://api.xero.com/assets.xro/1.0/Assets", {"page": 1})
    try:
        contacts = await fetch_paginated_collection(connection_row, CONTACTS_URL, "Contacts", max_pages=10)
        contacts_payload = {"Contacts": contacts}
    except Exception as exc:
        contacts = []
        contacts_payload = {"_error": _sync_error_message(exc)}
    try:
        outstanding_bills = await fetch_paginated_collection(
            connection_row,
            INVOICES_URL,
            "Invoices",
            params={"where": 'Type=="ACCPAY"&&Status!="VOIDED"&&Status!="DELETED"&&Status!="PAID"'},
            max_pages=5,
        )
        outstanding_bills_payload = {"Invoices": outstanding_bills}
    except Exception as exc:
        outstanding_bills = []
        outstanding_bills_payload = {"_error": _sync_error_message(exc)}

    _update_me_report_sync_run(
        sync_run_id,
        current_step="Calculating month-end review",
        summary="Applying Jaccountancy tax rules for CT estimate, dividend capacity and DLA risk.",
        progress=68,
        records_synced=len(accounts) + len(bank_transactions_payload.get("BankTransactions") or []) + len(contacts),
        heartbeat_at=utcnow(),
    )
    with get_connection() as db:
        with db.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM me_report_account_mappings
                WHERE client_id = %s
                ORDER BY account_code ASC
                """,
                (client["id"],),
            )
            mapping_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT summary, created_at
                FROM me_report_reviews
                WHERE client_id = %s
                ORDER BY created_at DESC
                LIMIT 6
                """,
                (client["id"],),
            )
            previous_review_rows = cursor.fetchall()
        db.commit()

    pl_lines = _xero_report_lines(profit_loss_payload)
    ytd_pl_lines = _xero_report_lines(ytd_profit_loss_payload)
    annual_pl_lines = _xero_report_lines(annual_profit_loss_payload)
    bs_lines = _xero_report_lines(balance_sheet_payload)
    monthly_sales = _report_amount(pl_lines, ("sales",), _report_amount(pl_lines, ("income",)))
    ytd_sales = _report_amount(ytd_pl_lines, ("sales",), _report_amount(ytd_pl_lines, ("income",), monthly_sales))
    annual_turnover = _report_amount(annual_pl_lines, ("sales",), _report_amount(annual_pl_lines, ("income",)))
    monthly_profit = _report_amount(pl_lines, ("net profit",), _report_amount(pl_lines, ("profit",)))
    ytd_profit = _report_amount(ytd_pl_lines, ("net profit",), _report_amount(ytd_pl_lines, ("profit",), monthly_profit))
    ytd_expenses = max(Decimal("0.00"), ytd_sales - ytd_profit)
    monthly_expenses = max(Decimal("0.00"), monthly_sales - monthly_profit)
    retained_earnings = _report_amount(bs_lines, ("retained",))
    dla_balance = _report_amount(bs_lines, ("director", "loan"))
    dividends_taken = abs(_report_amount(pl_lines, ("dividend",)))
    disallowable_addbacks = sum(
        Decimal("100.00")
        for row in mapping_rows
        if row.get("category") in ("Client entertaining", "Fines and penalties", "Depreciation", "Non-business expenses", "Private use items")
    )
    capital_allowance_review = sum(1 for row in mapping_rows if row.get("category") in ("Computer equipment", "Plant and machinery", "Office equipment", "Assets needing review"))
    taxable_profit = max(Decimal("0.00"), _money(monthly_profit + disallowable_addbacks))
    estimated_ct = _money(taxable_profit * ME_REPORT_TAX_RATE)
    post_tax_profit = _money(monthly_profit - estimated_ct)
    dividend_capacity = max(Decimal("0.00"), _money(retained_earnings + post_tax_profit - dividends_taken))
    total_extractable = _money(max(dla_balance, Decimal("0.00")) + dividend_capacity)
    low_confidence = [row for row in mapping_rows if int(row.get("confidence") or 0) < 80 or row.get("review_required")]
    reports_with_errors = [
        ("Profit and loss", profit_loss_payload.get("_error")),
        ("Year-to-date profit and loss", ytd_profit_loss_payload.get("_error")),
        ("12-month turnover report", annual_profit_loss_payload.get("_error")),
        ("Balance sheet", balance_sheet_payload.get("_error")),
        ("Trial balance", trial_balance_payload.get("_error")),
        ("Bank transactions", bank_transactions_payload.get("_error")),
        ("Journals", journals_payload.get("_error")),
        ("Contacts", contacts_payload.get("_error")),
        ("Accounts payable bills", outstanding_bills_payload.get("_error")),
    ]
    previous_vat_breach_at = None
    for previous_review in previous_review_rows:
        previous_summary = previous_review.get("summary") or {}
        if isinstance(previous_summary, str):
            try:
                previous_summary = json.loads(previous_summary)
            except ValueError:
                previous_summary = {}
        breach_value = previous_summary.get("vatThresholdFirstBreachedAt")
        if breach_value:
            previous_vat_breach_at = _parse_optional_iso_date(breach_value)
            break
    vat_threshold = Decimal("90000.00")
    vat_threshold_exceeded = annual_turnover > vat_threshold
    vat_threshold_first_breached_at = previous_vat_breach_at if vat_threshold_exceeded and previous_vat_breach_at else (period_end if vat_threshold_exceeded else previous_vat_breach_at)
    vat_warning_visible = bool(vat_threshold_exceeded or (vat_threshold_first_breached_at and (period_end - vat_threshold_first_breached_at).days <= 62))
    asset_register_total = _asset_register_total(fixed_assets_payload)
    balance_sheet_fixed_assets = _balance_sheet_fixed_asset_total(bs_lines)
    fixed_asset_difference = None if asset_register_total is None else _money(asset_register_total - balance_sheet_fixed_assets)
    duplicate_spend_candidates = _find_duplicate_spend_bill_candidates(bank_transactions_payload.get("BankTransactions") or [], outstanding_bills)
    duplicate_contact_candidates = _find_duplicate_contact_candidates(contacts)
    open_exceptions = []
    for label, error_message in reports_with_errors:
        if error_message:
            open_exceptions.append({
                "severity": "amber" if label in ("Bank transactions", "Journals", "Fixed asset register", "Contacts", "Accounts payable bills") else "red",
                "title": f"{label} could not be read from Xero",
                "detail": error_message,
                "suggested_action": "Review Xero OAuth scopes and reconnect Xero if required.",
            })
    if vat_warning_visible:
        open_exceptions.append({
            "severity": "amber",
            "title": "VAT registration threshold review",
            "detail": (
                f"Xero shows estimated rolling 12-month turnover of £{_money(annual_turnover):,.2f}. "
                f"The VAT registration threshold is £{vat_threshold:,.2f}. "
                f"This warning remains visible for two months after the first breach date."
            ),
            "suggested_action": "Review VAT registration position, confirm taxable turnover, and document the client advice.",
        })
    if asset_register_total is None:
        open_exceptions.append({
            "severity": "amber",
            "title": "Fixed asset register could not be reconciled",
            "detail": fixed_assets_payload.get("_error") or "Xero fixed asset data was not returned.",
            "suggested_action": "Confirm fixed asset API access and compare the fixed asset register to the Xero balance sheet manually.",
        })
    elif abs(fixed_asset_difference or Decimal("0.00")) > Decimal("1.00"):
        open_exceptions.append({
            "severity": "amber",
            "title": "Fixed asset register does not match balance sheet",
            "detail": (
                f"AI-assisted matching found fixed asset register value £{_money(asset_register_total):,.2f} "
                f"versus balance sheet fixed asset accounts £{_money(balance_sheet_fixed_assets):,.2f}."
            ),
            "suggested_action": "Review fixed asset account mapping, depreciation postings and disposals before report approval.",
        })
    for candidate in duplicate_spend_candidates:
        open_exceptions.append({
            "severity": "red",
            "title": "Possible duplicate bill payment posting",
            "detail": (
                f"Spend money transaction {candidate['bankTransactionReference']} for £{_money(candidate['amount']):,.2f} "
                f"matches outstanding bill {candidate['billNumber']}."
            ),
            "suggested_action": "Review whether the spend money transaction should be removed and the payment allocated to the bill.",
        })
    for candidate in duplicate_contact_candidates:
        open_exceptions.append({
            "severity": "amber",
            "title": "Possible duplicate Xero contact",
            "detail": f"{candidate['keepName']} and {candidate['mergeName']} look like the same contact.",
            "suggested_action": "Use Merge contact after staff review to combine the duplicate contact into the selected primary contact in Xero.",
            "action_payload": {"type": "duplicate_contact", **candidate},
        })
    for mapping in low_confidence[:20]:
        open_exceptions.append({
            "severity": "amber",
            "title": f"{mapping.get('account_name') or mapping.get('account_code')} needs mapping review",
            "detail": mapping.get("reason") or "AI/rules confidence is below the approval threshold.",
            "suggested_action": "Approve, amend or mark the account as needs review before issuing the month-end report.",
        })
    if dla_balance < Decimal("0.00"):
        open_exceptions.append({
            "severity": "red",
            "title": "Director loan account appears overdrawn",
            "detail": f"Closing DLA balance appears to be £{abs(dla_balance):,.2f} overdrawn.",
            "suggested_action": "Review possible s455 tax and dividend legality before report issue.",
        })
    if dividends_taken > retained_earnings + post_tax_profit:
        open_exceptions.append({
            "severity": "red",
            "title": "Possible illegal dividend risk",
            "detail": "Dividends appear to exceed estimated distributable reserves after corporation tax.",
            "suggested_action": "Accountant review required before client commentary is approved.",
        })
    if not any("corporation tax" in (row.get("account_name") or "").lower() for row in mapping_rows):
        open_exceptions.append({
            "severity": "amber",
            "title": "No corporation tax provision account identified",
            "detail": "The account mapping did not identify a corporation tax creditor account.",
            "suggested_action": "Confirm where the CT provision should be posted or tracked.",
        })
    traffic_light = "red" if any(item["severity"] == "red" for item in open_exceptions) else ("amber" if open_exceptions else "green")
    dla_status = "red" if dla_balance < 0 else ("amber" if dla_balance == 0 else "green")
    summary = {
        "periodStart": period_start.isoformat(),
        "periodEnd": period_end.isoformat(),
        "monthlySales": float(_money(monthly_sales)),
        "monthlyExpenses": float(_money(monthly_expenses)),
        "monthlyProfit": float(_money(monthly_profit)),
        "yearToDateSales": float(_money(ytd_sales)),
        "yearToDateExpenses": float(_money(ytd_expenses)),
        "yearToDateProfit": float(_money(ytd_profit)),
        "rolling12MonthTurnover": float(_money(annual_turnover)),
        "vatThreshold": float(vat_threshold),
        "vatThresholdExceeded": vat_threshold_exceeded,
        "vatThresholdFirstBreachedAt": _iso(vat_threshold_first_breached_at) or "",
        "vatWarningVisible": vat_warning_visible,
        "accountingProfit": float(_money(monthly_profit)),
        "taxAdjustments": float(_money(disallowable_addbacks)),
        "estimatedTaxableProfit": float(_money(taxable_profit)),
        "estimatedCorporationTax": float(estimated_ct),
        "effectiveTaxRate": float((estimated_ct / taxable_profit * 100).quantize(Decimal("0.1"))) if taxable_profit else 0,
        "taxProvisionRequired": float(estimated_ct),
        "openingRetainedReserves": float(_money(retained_earnings)),
        "dividendsTaken": float(_money(dividends_taken)),
        "dividendCapacity": float(_money(dividend_capacity)),
        "directorLoanCreditBalance": float(_money(max(dla_balance, Decimal("0.00")))),
        "totalPotentialExtraction": float(total_extractable),
        "dlaBalance": float(_money(dla_balance)),
        "dlaStatus": dla_status,
        "capitalAllowanceReviewCount": capital_allowance_review,
        "fixedAssetRegisterTotal": float(_money(asset_register_total or 0)),
        "balanceSheetFixedAssetTotal": float(_money(balance_sheet_fixed_assets)),
        "fixedAssetDifference": float(_money(fixed_asset_difference or 0)),
        "duplicateSpendBillCount": len(duplicate_spend_candidates),
        "duplicateContactCount": len(duplicate_contact_candidates),
        "mappingReviewCount": len(low_confidence),
        "exceptionCount": len(open_exceptions),
        "trafficLight": traffic_light,
        "commentary": (
            f"This month shows estimated profit of £{_money(monthly_profit):,.2f}. "
            f"Year-to-date profit is £{_money(ytd_profit):,.2f}. "
            f"Estimated corporation tax is £{estimated_ct:,.2f}, subject to accountant review and final year-end adjustments. "
            f"Rolling 12-month turnover is £{_money(annual_turnover):,.2f}, "
            f"{'above' if vat_threshold_exceeded else 'below'} the £{vat_threshold:,.0f} VAT threshold. "
            f"The director loan account is {'in credit' if dla_balance >= 0 else 'overdrawn'}, and DLA repayment should be considered before dividends where credit is available."
        ),
    }
    raw_payload = {
        "accounts": accounts,
        "profitAndLoss": profit_loss_payload,
        "yearToDateProfitAndLoss": ytd_profit_loss_payload,
        "annualProfitAndLoss": annual_profit_loss_payload,
        "balanceSheet": balance_sheet_payload,
        "trialBalance": trial_balance_payload,
        "bankTransactions": bank_transactions_payload,
        "journals": journals_payload,
        "fixedAssets": fixed_assets_payload,
        "contacts": contacts_payload,
        "outstandingBills": outstanding_bills_payload,
    }
    with get_connection() as db:
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO me_report_reviews (
                    client_id, period_start, period_end, status, traffic_light,
                    summary, raw_payload, created_by_user_id, created_at, updated_at
                )
                VALUES (%s, %s, %s, 'calculated', %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                RETURNING id
                """,
                (
                    client["id"],
                    period_start,
                    period_end,
                    traffic_light,
                    json.dumps(summary, default=_json_default),
                    json.dumps(raw_payload, default=_json_default),
                    user["id"],
                    utcnow(),
                    utcnow(),
                ),
            )
            review_id = cursor.fetchone()["id"]
            cursor.execute(
                "UPDATE me_report_exceptions SET status = 'superseded', updated_at = %s WHERE client_id = %s AND status = 'open'",
                (utcnow(), client["id"]),
            )
            for item in open_exceptions:
                cursor.execute(
                    """
                    INSERT INTO me_report_exceptions (
                        client_id, review_id, severity, title, detail,
                        suggested_action, action_payload, status, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'open', %s, %s)
                    """,
                    (
                        client["id"],
                        review_id,
                        item["severity"],
                        item["title"],
                        item["detail"],
                        item["suggested_action"],
                        json.dumps(item.get("action_payload") or {}, default=_json_default),
                        utcnow(),
                        utcnow(),
                    ),
                )
            cursor.execute(
                """
                UPDATE me_report_clients
                SET last_sync_at = %s,
                    last_calculated_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (utcnow(), utcnow(), utcnow(), client["id"]),
            )
        db.commit()
    _update_me_report_sync_run(
        sync_run_id,
        status="completed",
        current_step="ME Report calculation complete",
        summary=f"Calculated {client.get('client_name') or 'client'}: {traffic_light.upper()} status, {len(open_exceptions)} review point(s).",
        progress=100,
        records_synced=len(accounts),
        heartbeat_at=utcnow(),
        completed_at=utcnow(),
    )
    record_audit_event(
        "me_report_review",
        str(review_id),
        "me_report.review_calculated",
        {"client_id": str(client["id"]), "traffic_light": traffic_light, "exception_count": len(open_exceptions)},
        user["id"],
    )
    return me_report_payload(user)


def update_me_report_mapping(user: dict, mapping_id: str, payload: dict) -> dict:
    status_value = str(payload.get("status") or "approved").strip() or "approved"
    note = str(payload.get("note") or "").strip()
    suggested_treatment = str(payload.get("suggestedTreatment") or "").strip()
    tax_treatment = str(payload.get("taxTreatment") or "").strip()
    category = str(payload.get("category") or "").strip()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT mappings.*, clients.user_id
                FROM me_report_account_mappings AS mappings
                JOIN me_report_clients AS clients ON clients.id = mappings.client_id
                WHERE mappings.id = %s
                  AND clients.user_id = %s
                """,
                (mapping_id, user["id"]),
            )
            mapping = cursor.fetchone()
            if not mapping:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ME Report account mapping not found.")
            cursor.execute(
                """
                UPDATE me_report_account_mappings
                SET status = %s,
                    note = %s,
                    suggested_treatment = COALESCE(NULLIF(%s, ''), suggested_treatment),
                    tax_treatment = COALESCE(NULLIF(%s, ''), tax_treatment),
                    category = COALESCE(NULLIF(%s, ''), category),
                    review_required = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (status_value, note, suggested_treatment, tax_treatment, category, status_value != "approved", utcnow(), mapping_id),
            )
        connection.commit()
    record_audit_event("me_report_mapping", mapping_id, "me_report.mapping_updated", {"status": status_value, "note": note}, user["id"])
    return me_report_payload(user)


def update_me_report_exception(user: dict, exception_id: str, payload: dict) -> dict:
    status_value = str(payload.get("status") or "resolved").strip() or "resolved"
    note = str(payload.get("note") or "").strip()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT exceptions.*, clients.user_id
                FROM me_report_exceptions AS exceptions
                JOIN me_report_clients AS clients ON clients.id = exceptions.client_id
                WHERE exceptions.id = %s
                  AND clients.user_id = %s
                """,
                (exception_id, user["id"]),
            )
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ME Report exception not found.")
            cursor.execute(
                """
                UPDATE me_report_exceptions
                SET status = %s,
                    note = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (status_value, note, utcnow(), exception_id),
            )
        connection.commit()
    record_audit_event("me_report_exception", exception_id, "me_report.exception_updated", {"status": status_value, "note": note}, user["id"])
    return me_report_payload(user)


async def merge_me_report_duplicate_contact(user: dict, exception_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT exceptions.*, clients.xero_connection_id, clients.user_id
                FROM me_report_exceptions AS exceptions
                JOIN me_report_clients AS clients ON clients.id = exceptions.client_id
                WHERE exceptions.id = %s
                  AND clients.user_id = %s
                """,
                (exception_id, user["id"]),
            )
            exception = cursor.fetchone()
        connection.commit()
    if not exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ME Report duplicate contact exception not found.")
    action_payload = exception.get("action_payload") or {}
    if isinstance(action_payload, str):
        try:
            action_payload = json.loads(action_payload)
        except ValueError:
            action_payload = {}
    if action_payload.get("type") != "duplicate_contact":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This ME Report exception is not a duplicate contact merge action.")
    keep_contact_id = str(action_payload.get("keepContactId") or "").strip()
    merge_contact_id = str(action_payload.get("mergeContactId") or "").strip()
    if not keep_contact_id or not merge_contact_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duplicate contact exception is missing Xero contact ids.")
    connection_row = _me_report_xero_connection(user, {"xero_connection_id": exception.get("xero_connection_id")})
    await merge_contacts(connection_row, keep_contact_id, merge_contact_id)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE me_report_exceptions
                SET status = 'resolved',
                    note = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    f"Merged {action_payload.get('mergeName') or merge_contact_id} into {action_payload.get('keepName') or keep_contact_id} in Xero.",
                    utcnow(),
                    exception_id,
                ),
            )
        connection.commit()
    record_audit_event("me_report_exception", exception_id, "me_report.contact_merged", action_payload, user["id"])
    return me_report_payload(user)


def _latest_me_report_review(user: dict, client_id: str, review_id: str | None = None) -> dict:
    _me_report_client_row(user, client_id)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            if review_id:
                cursor.execute(
                    """
                    SELECT *
                    FROM me_report_reviews
                    WHERE id = %s
                      AND client_id = %s
                    """,
                    (review_id, client_id),
                )
            else:
                cursor.execute(
                    """
                    SELECT *
                    FROM me_report_reviews
                    WHERE client_id = %s
                    ORDER BY period_end DESC, created_at DESC
                    LIMIT 1
                    """,
                    (client_id,),
                )
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Run ME Report sync and calculation before generating a report.")
    return row


def generate_me_report(user: dict, client_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    client = _me_report_client_row(user, client_id)
    review = _latest_me_report_review(user, client_id, payload.get("reviewId"))
    summary = review.get("summary") or {}
    if isinstance(summary, str):
        try:
            summary = json.loads(summary)
        except ValueError:
            summary = {}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM me_report_exceptions
                WHERE client_id = %s
                  AND review_id = %s
                  AND status = 'open'
                ORDER BY CASE severity WHEN 'red' THEN 0 WHEN 'amber' THEN 1 ELSE 2 END, created_at DESC
                """,
                (client_id, review["id"]),
            )
            exceptions = cursor.fetchall()
            cursor.execute(
                """
                SELECT *
                FROM me_report_account_mappings
                WHERE client_id = %s
                ORDER BY account_code ASC
                """,
                (client_id,),
            )
            mappings = cursor.fetchall()
        connection.commit()
    commentary = str(payload.get("commentary") or summary.get("commentary") or "").strip()
    if not commentary:
        commentary = "Month-end bookkeeping review completed. Accountant approval is required before issuing final advice."
    mapping_rows = "".join(
        f"<tr><td>{xml_escape(row.get('account_code') or '')}</td><td>{xml_escape(row.get('account_name') or '')}</td><td>{xml_escape(row.get('category') or '')}</td><td>{xml_escape(row.get('tax_treatment') or '')}</td><td>{int(row.get('confidence') or 0)}%</td></tr>"
        for row in mappings[:80]
    )
    exception_rows = "".join(
        f"<li><strong>{xml_escape(item.get('severity') or '').upper()} - {xml_escape(item.get('title') or '')}</strong><br>{xml_escape(item.get('detail') or '')}<br><em>{xml_escape(item.get('suggested_action') or '')}</em></li>"
        for item in exceptions
    )
    report_html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>ME Report - {xml_escape(client.get('client_name') or 'Client')}</title>
<style>
body {{ font-family: Arial, sans-serif; color: #1e2f4d; margin: 32px; line-height: 1.45; }}
.cover {{ border-bottom: 4px solid #1d67f2; padding-bottom: 24px; margin-bottom: 28px; }}
.grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
.metric {{ border: 1px solid #d8e2f2; border-radius: 8px; padding: 12px; }}
.metric span {{ display: block; color: #6b7890; font-size: 12px; text-transform: uppercase; }}
.metric strong {{ font-size: 22px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
th, td {{ border-bottom: 1px solid #d8e2f2; text-align: left; padding: 8px; font-size: 13px; }}
h1, h2 {{ color: #1e2f4d; }}
</style>
</head>
<body>
<section class="cover">
<p>Jaccountancy Month-End Bookkeeping Report</p>
<h1>{xml_escape(client.get('client_name') or 'Client')}</h1>
<p>Period {xml_escape(summary.get('periodStart') or '')} to {xml_escape(summary.get('periodEnd') or '')}</p>
</section>
<section>
<h2>Executive summary</h2>
<p>{xml_escape(commentary)}</p>
<div class="grid">
<div class="metric"><span>Monthly sales</span><strong>£{_money(summary.get('monthlySales')):,.2f}</strong></div>
<div class="metric"><span>Monthly profit</span><strong>£{_money(summary.get('monthlyProfit')):,.2f}</strong></div>
<div class="metric"><span>YTD sales</span><strong>£{_money(summary.get('yearToDateSales')):,.2f}</strong></div>
<div class="metric"><span>YTD profit</span><strong>£{_money(summary.get('yearToDateProfit')):,.2f}</strong></div>
<div class="metric"><span>Estimated CT</span><strong>£{_money(summary.get('estimatedCorporationTax')):,.2f}</strong></div>
<div class="metric"><span>12m turnover</span><strong>£{_money(summary.get('rolling12MonthTurnover')):,.2f}</strong></div>
<div class="metric"><span>Dividend capacity</span><strong>£{_money(summary.get('dividendCapacity')):,.2f}</strong></div>
<div class="metric"><span>DLA balance</span><strong>£{_money(summary.get('dlaBalance')):,.2f}</strong></div>
<div class="metric"><span>Traffic light</span><strong>{xml_escape(summary.get('trafficLight') or review.get('traffic_light') or 'amber').upper()}</strong></div>
</div>
</section>
<section>
<h2>Profit and loss versus year to date</h2>
<p>For the current month, sales are £{_money(summary.get('monthlySales')):,.2f}, expenses are £{_money(summary.get('monthlyExpenses')):,.2f}, and profit is £{_money(summary.get('monthlyProfit')):,.2f}. Year to date, sales are £{_money(summary.get('yearToDateSales')):,.2f}, expenses are £{_money(summary.get('yearToDateExpenses')):,.2f}, and profit is £{_money(summary.get('yearToDateProfit')):,.2f}.</p>
</section>
<section>
<h2>Estimated corporation tax</h2>
<p>Accounting profit £{_money(summary.get('accountingProfit')):,.2f}, tax adjustments £{_money(summary.get('taxAdjustments')):,.2f}, taxable profit £{_money(summary.get('estimatedTaxableProfit')):,.2f}, estimated CT £{_money(summary.get('estimatedCorporationTax')):,.2f}.</p>
</section>
<section>
<h2>VAT threshold check</h2>
<p>Rolling 12-month turnover is £{_money(summary.get('rolling12MonthTurnover')):,.2f} against the £{_money(summary.get('vatThreshold')):,.2f} VAT threshold. Status: {'warning visible' if summary.get('vatWarningVisible') else 'below active warning threshold'}. First breach date: {xml_escape(summary.get('vatThresholdFirstBreachedAt') or 'not breached')}.</p>
</section>
<section>
<h2>Dividend availability and DLA</h2>
<p>The director loan account appears to be {'in credit' if _money(summary.get('dlaBalance')) >= 0 else 'overdrawn'} by £{abs(_money(summary.get('dlaBalance'))):,.2f}. If the DLA is in credit, repay that before considering dividends. Estimated further dividend capacity is £{_money(summary.get('dividendCapacity')):,.2f}.</p>
</section>
<section>
<h2>Balance sheet and fixed assets</h2>
<p>AI-assisted fixed asset matching shows fixed asset register value £{_money(summary.get('fixedAssetRegisterTotal')):,.2f} against balance sheet fixed asset accounts £{_money(summary.get('balanceSheetFixedAssetTotal')):,.2f}. Difference: £{_money(summary.get('fixedAssetDifference')):,.2f}.</p>
</section>
<section>
<h2>Duplicate and data quality checks</h2>
<p>Possible duplicate spend-money/bill issues: {int(summary.get('duplicateSpendBillCount') or 0)}. Possible duplicate Xero contacts: {int(summary.get('duplicateContactCount') or 0)}. Review any open exceptions before approval.</p>
</section>
<section>
<h2>Bookkeeping review points</h2>
<ul>{exception_rows or '<li>No open exceptions at the time this report was generated.</li>'}</ul>
</section>
<section>
<h2>Account mappings</h2>
<table><thead><tr><th>Code</th><th>Account</th><th>Category</th><th>Tax treatment</th><th>Confidence</th></tr></thead><tbody>{mapping_rows}</tbody></table>
</section>
</body>
</html>
"""
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO me_report_reports (
                    client_id, review_id, status, recipient_email,
                    report_html, commentary, created_by_user_id, created_at
                )
                VALUES (%s, %s, 'draft', %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    client_id,
                    review["id"],
                    client.get("report_recipient_email") or "",
                    report_html,
                    commentary,
                    user["id"],
                    utcnow(),
                ),
            )
            report_id = cursor.fetchone()["id"]
            cursor.execute(
                "UPDATE me_report_clients SET last_report_at = %s, updated_at = %s WHERE id = %s",
                (utcnow(), utcnow(), client_id),
            )
        connection.commit()
    record_audit_event("me_report_report", str(report_id), "me_report.report_generated", {"client_id": client_id, "review_id": str(review["id"])}, user["id"])
    return {"report": {"id": str(report_id), "downloadUrl": f"/api/me-report/reports/{report_id}/download"}, "meReport": me_report_payload(user)}


def me_report_report_html(user: dict, report_id: str) -> str:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT reports.report_html
                FROM me_report_reports AS reports
                JOIN me_report_clients AS clients ON clients.id = reports.client_id
                WHERE reports.id = %s
                  AND clients.user_id = %s
                """,
                (report_id, user["id"]),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="ME Report output not found.")
    return row.get("report_html") or ""


BANK_STATEMENT_EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["statementStartDate", "statementEndDate", "openingBalance", "closingBalance", "accountNumber", "transactions"],
    "properties": {
        "statementStartDate": {"type": ["string", "null"]},
        "statementEndDate": {"type": ["string", "null"]},
        "openingBalance": {"type": ["number", "null"]},
        "closingBalance": {"type": ["number", "null"]},
        "accountNumber": {"type": ["string", "null"]},
        "transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["date", "description", "amount", "balance", "type"],
                "properties": {
                    "date": {"type": "string"},
                    "description": {"type": "string"},
                    "amount": {"type": "number"},
                    "balance": {"type": ["number", "null"]},
                    "type": {"type": "string"},
                },
            },
        },
    },
}


def _bank_statement_tenant_id(user: dict) -> str:
    return str(get_xero_connection_for_user(user["id"]).get("tenant_id") or "")


def _normalise_hash_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _bank_transaction_source_hash(bank_account_id: str, transaction: dict) -> str:
    balance = transaction.get("balance")
    components = [
        str(bank_account_id),
        str(transaction.get("date") or ""),
        _normalise_hash_text(transaction.get("description")),
        f"{_money(transaction.get('amount')):.2f}",
        "" if balance is None else f"{_money(balance):.2f}",
    ]
    return hashlib.sha256("|".join(components).encode()).hexdigest()


def _bank_statement_flags(transactions: list[dict]) -> list[dict]:
    flags = []
    ordered = sorted(transactions, key=lambda row: (row.get("transaction_date") or date.min, row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)))
    previous = None
    for row in ordered:
        if previous:
            previous_date = previous.get("transaction_date")
            current_date = row.get("transaction_date")
            if isinstance(previous_date, date) and isinstance(current_date, date):
                day_gap = (current_date - previous_date).days
                if day_gap > 45:
                    flags.append({
                        "type": "date_gap",
                        "severity": "medium",
                        "message": f"No extracted transactions between {previous_date.isoformat()} and {current_date.isoformat()}.",
                    })
            previous_balance = previous.get("balance")
            current_balance = row.get("balance")
            if previous_balance is not None and current_balance is not None:
                expected = _money(previous_balance) + _money(row.get("amount"))
                actual = _money(current_balance)
                if abs(expected - actual) > Decimal("0.02"):
                    flags.append({
                        "type": "balance_mismatch",
                        "severity": "high",
                        "message": (
                            f"Running balance mismatch on {current_date.isoformat() if isinstance(current_date, date) else 'a transaction'}: "
                            f"expected £{expected:,.2f}, extracted £{actual:,.2f}."
                        ),
                    })
        previous = row
    return flags[:20]


def _serialize_bank_account(account: dict, uploads: list[dict], transactions: list[dict]) -> dict:
    ordered_transactions = sorted(transactions, key=lambda row: (row.get("transaction_date") or date.min, row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)))
    return {
        "id": str(account["id"]),
        "clientId": str(account["extraction_client_id"]),
        "accountName": account.get("account_name") or "Bank account",
        "accountNumber": account.get("account_number") or "",
        "sortCode": account.get("sort_code") or "",
        "currencyCode": account.get("currency_code") or "GBP",
        "createdAt": _iso(account.get("created_at")) or "",
        "uploads": [
            {
                "id": str(upload["id"]),
                "filename": upload.get("filename") or "",
                "status": upload.get("status") or "",
                "errorMessage": upload.get("error_message") or "",
                "statementStartDate": _iso(upload.get("statement_start_date")) or "",
                "statementEndDate": _iso(upload.get("statement_end_date")) or "",
                "openingBalance": float(_money(upload.get("opening_balance"))) if upload.get("opening_balance") is not None else None,
                "closingBalance": float(_money(upload.get("closing_balance"))) if upload.get("closing_balance") is not None else None,
                "extractedCount": int(upload.get("extracted_count") or 0),
                "insertedCount": int(upload.get("inserted_count") or 0),
                "duplicateCount": int(upload.get("duplicate_count") or 0),
                "createdAt": _iso(upload.get("created_at")) or "",
                "completedAt": _iso(upload.get("completed_at")) or "",
            }
            for upload in uploads
        ],
        "transactions": [
            {
                "id": str(row["id"]),
                "uploadId": str(row.get("upload_id")) if row.get("upload_id") else "",
                "date": _iso(row.get("transaction_date")) or "",
                "description": row.get("description") or "",
                "amount": float(_money(row.get("amount"))),
                "balance": float(_money(row.get("balance"))) if row.get("balance") is not None else None,
                "type": row.get("transaction_type") or "",
                "createdAt": _iso(row.get("created_at")) or "",
            }
            for row in ordered_transactions
        ],
        "flags": _bank_statement_flags(ordered_transactions),
    }


def bank_statement_payload(user: dict) -> dict:
    tenant_id = _bank_statement_tenant_id(user)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, email, xero_contact_id
                FROM customers
                WHERE tenant_id = %s
                ORDER BY name ASC
                """,
                (tenant_id,),
            )
            customers = cursor.fetchall()
            cursor.execute(
                """
                SELECT clients.*, customers.name AS customer_name, customers.email, customers.xero_contact_id
                FROM bank_statement_clients AS clients
                JOIN customers ON customers.id = clients.customer_id
                WHERE clients.tenant_id = %s
                  AND clients.status = 'active'
                ORDER BY customers.name ASC
                """,
                (tenant_id,),
            )
            clients = cursor.fetchall()
            client_ids = [row["id"] for row in clients]
            accounts_by_client = defaultdict(list)
            uploads_by_account = defaultdict(list)
            transactions_by_account = defaultdict(list)
            if client_ids:
                cursor.execute(
                    """
                    SELECT *
                    FROM bank_statement_accounts
                    WHERE extraction_client_id = ANY(%s)
                    ORDER BY created_at DESC
                    """,
                    (client_ids,),
                )
                account_rows = cursor.fetchall()
                account_ids = [row["id"] for row in account_rows]
                for row in account_rows:
                    accounts_by_client[str(row["extraction_client_id"])].append(row)
                if account_ids:
                    cursor.execute(
                        """
                        SELECT *
                        FROM bank_statement_uploads
                        WHERE bank_account_id = ANY(%s)
                        ORDER BY created_at DESC
                        """,
                        (account_ids,),
                    )
                    for row in cursor.fetchall():
                        uploads_by_account[str(row["bank_account_id"])].append(row)
                    cursor.execute(
                        """
                        SELECT *
                        FROM bank_statement_transactions
                        WHERE bank_account_id = ANY(%s)
                        ORDER BY transaction_date ASC, created_at ASC
                        """,
                        (account_ids,),
                    )
                    for row in cursor.fetchall():
                        transactions_by_account[str(row["bank_account_id"])].append(row)
        connection.commit()

    serialized_clients = []
    for client in clients:
        accounts = [
            _serialize_bank_account(account, uploads_by_account.get(str(account["id"]), []), transactions_by_account.get(str(account["id"]), []))
            for account in accounts_by_client.get(str(client["id"]), [])
        ]
        serialized_clients.append({
            "id": str(client["id"]),
            "customerId": str(client["customer_id"]),
            "customerName": client.get("customer_name") or "Unnamed client",
            "email": client.get("email") or "",
            "xeroContactId": client.get("xero_contact_id") or "",
            "createdAt": _iso(client.get("created_at")) or "",
            "accounts": accounts,
        })

    return {
        "customers": [
            {
                "id": str(customer["id"]),
                "name": customer.get("name") or "Unnamed client",
                "email": customer.get("email") or "",
                "xeroContactId": customer.get("xero_contact_id") or "",
            }
            for customer in customers
        ],
        "clients": serialized_clients,
        "summary": {
            "clientCount": len(serialized_clients),
            "accountCount": sum(len(client["accounts"]) for client in serialized_clients),
            "transactionCount": sum(len(account["transactions"]) for client in serialized_clients for account in client["accounts"]),
            "flagCount": sum(len(account["flags"]) for client in serialized_clients for account in client["accounts"]),
        },
    }


def add_bank_statement_client(user: dict, payload: dict) -> dict:
    tenant_id = _bank_statement_tenant_id(user)
    customer_id = str(payload.get("customerId") or "").strip()
    if not customer_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose a Xero customer for bank statement extraction.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM customers WHERE id = %s AND tenant_id = %s",
                (customer_id, tenant_id),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found in this Xero tenant.")
            cursor.execute(
                """
                INSERT INTO bank_statement_clients (tenant_id, customer_id, status, created_by_user_id, created_at, updated_at)
                VALUES (%s, %s, 'active', %s, %s, %s)
                ON CONFLICT (tenant_id, customer_id) DO UPDATE
                SET status = 'active',
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                (tenant_id, customer_id, user["id"], utcnow(), utcnow()),
            )
            client_id = cursor.fetchone()["id"]
        connection.commit()
    record_audit_event("bank_statement_client", str(client_id), "bank_statement.client_added", {"customer_id": customer_id}, user["id"])
    return bank_statement_payload(user)


def create_bank_statement_account(user: dict, extraction_client_id: str, payload: dict) -> dict:
    tenant_id = _bank_statement_tenant_id(user)
    account_name = str(payload.get("accountName") or "Bank account").strip()[:160]
    account_number = str(payload.get("accountNumber") or "").strip()[:80]
    sort_code = str(payload.get("sortCode") or "").strip()[:80]
    currency_code = str(payload.get("currencyCode") or "GBP").strip().upper()[:8] or "GBP"
    if not account_number:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the bank account number.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM bank_statement_clients
                WHERE id = %s
                  AND tenant_id = %s
                  AND status = 'active'
                """,
                (extraction_client_id, tenant_id),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extraction client not found.")
            cursor.execute(
                """
                INSERT INTO bank_statement_accounts (
                    extraction_client_id, account_name, account_number, sort_code,
                    currency_code, created_by_user_id, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (extraction_client_id, account_name, account_number, sort_code, currency_code, user["id"], utcnow(), utcnow()),
            )
            account_id = cursor.fetchone()["id"]
        connection.commit()
    record_audit_event("bank_statement_account", str(account_id), "bank_statement.account_added", {"account_number": account_number}, user["id"])
    return bank_statement_payload(user)


async def _extract_bank_statement_pdf(file_bytes: bytes, filename: str, account: dict) -> dict:
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OpenAI extraction is not configured. Add OPENAI_API_KEY before uploading statements.")
    if len(file_bytes) > 50 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PDF files must be under 50 MB for extraction.")
    encoded = base64.b64encode(file_bytes).decode("ascii")
    prompt = (
        "Extract bank statement transactions from this PDF. Return JSON only. "
        "Use ISO dates in YYYY-MM-DD format. "
        "Use signed amounts: money paid in is positive, money paid out is negative. "
        "Include every posted transaction line with date, description, amount, running balance where shown, and a short type. "
        "Ignore page headers, brought forward/carried forward labels unless they are opening or closing balances. "
        f"The expected account number is {account.get('account_number') or 'unknown'}."
    )
    request_body = {
        "model": settings.openai_model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": filename,
                        "file_data": f"data:application/pdf;base64,{encoded}",
                    },
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "bank_statement_extraction",
                "schema": BANK_STATEMENT_EXTRACTION_SCHEMA,
                "strict": True,
            }
        },
        "max_output_tokens": 12000,
    }
    async with httpx.AsyncClient(timeout=OPENAI_INSIGHTS_TIMEOUT_SECONDS) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
            json=request_body,
        )
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"OpenAI statement extraction failed with status {response.status_code}: {response.text[:500]}",
        )
    text = _extract_response_text(response.json())
    try:
        parsed = json.loads(text) if text else {}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="OpenAI returned statement extraction that was not valid JSON.") from exc
    return parsed


async def upload_bank_statement_pdf(user: dict, bank_account_id: str, filename: str, content_type: str, file_bytes: bytes) -> dict:
    tenant_id = _bank_statement_tenant_id(user)
    if not filename.lower().endswith(".pdf") and "pdf" not in (content_type or "").lower():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload a PDF bank statement.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT accounts.*, clients.tenant_id
                FROM bank_statement_accounts AS accounts
                JOIN bank_statement_clients AS clients ON clients.id = accounts.extraction_client_id
                WHERE accounts.id = %s
                  AND clients.tenant_id = %s
                """,
                (bank_account_id, tenant_id),
            )
            account = cursor.fetchone()
            if account is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bank account not found.")
            cursor.execute(
                """
                INSERT INTO bank_statement_uploads (
                    bank_account_id, filename, content_type, status,
                    created_by_user_id, created_at
                )
                VALUES (%s, %s, %s, 'processing', %s, %s)
                RETURNING id
                """,
                (bank_account_id, filename, content_type, user["id"], utcnow()),
            )
            upload_id = cursor.fetchone()["id"]
        connection.commit()

    try:
        extracted = await _extract_bank_statement_pdf(file_bytes, filename, account)
        transactions = extracted.get("transactions") or []
        inserted_count = 0
        duplicate_count = 0
        valid_count = 0
        with get_connection() as connection:
            with connection.cursor() as cursor:
                for transaction in transactions:
                    try:
                        transaction_date = _parse_iso_date(transaction.get("date"), "Transaction date")
                        amount = _money(transaction.get("amount"))
                        description = str(transaction.get("description") or "").strip()
                        if not description:
                            continue
                        balance = transaction.get("balance")
                        balance_amount = _money(balance) if balance is not None else None
                    except Exception:
                        continue
                    valid_count += 1
                    source_hash = _bank_transaction_source_hash(
                        bank_account_id,
                        {"date": transaction_date.isoformat(), "description": description, "amount": amount, "balance": balance_amount},
                    )
                    cursor.execute(
                        """
                        INSERT INTO bank_statement_transactions (
                            bank_account_id, upload_id, transaction_date, description,
                            amount, balance, transaction_type, source_hash, raw, created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                        ON CONFLICT (bank_account_id, source_hash) DO NOTHING
                        RETURNING id
                        """,
                        (
                            bank_account_id,
                            upload_id,
                            transaction_date,
                            description,
                            amount,
                            balance_amount,
                            str(transaction.get("type") or "")[:80],
                            source_hash,
                            json.dumps(transaction, default=_json_default),
                            utcnow(),
                        ),
                    )
                    if cursor.fetchone():
                        inserted_count += 1
                    else:
                        duplicate_count += 1
                cursor.execute(
                    """
                    UPDATE bank_statement_uploads
                    SET status = 'completed',
                        statement_start_date = %s,
                        statement_end_date = %s,
                        opening_balance = %s,
                        closing_balance = %s,
                        extracted_count = %s,
                        inserted_count = %s,
                        duplicate_count = %s,
                        completed_at = %s
                    WHERE id = %s
                    """,
                    (
                        _parse_optional_iso_date(extracted.get("statementStartDate")),
                        _parse_optional_iso_date(extracted.get("statementEndDate")),
                        _money(extracted.get("openingBalance")) if extracted.get("openingBalance") is not None else None,
                        _money(extracted.get("closingBalance")) if extracted.get("closingBalance") is not None else None,
                        valid_count,
                        inserted_count,
                        duplicate_count,
                        utcnow(),
                        upload_id,
                    ),
                )
            connection.commit()
    except Exception as exc:
        error = _sync_error_message(exc)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE bank_statement_uploads
                    SET status = 'failed',
                        error_message = %s,
                        completed_at = %s
                    WHERE id = %s
                    """,
                    (error, utcnow(), upload_id),
                )
            connection.commit()
        raise

    record_audit_event(
        "bank_statement_upload",
        str(upload_id),
        "bank_statement.upload_extracted",
        {"filename": filename, "inserted_count": inserted_count, "duplicate_count": duplicate_count},
        user["id"],
    )
    return bank_statement_payload(user)


IGNITION_PLAN_LABELS = ("Solo", "Solo+", "Solo MTD", "Micro", "Starter", "Standard", "Premium", "Ultimate")


def _ignition_record_id(dataset: str, row: dict, index: int) -> str:
    for key in ("slug", "id", "uuid", "reference_number", "external_client_id", "client_slug", "service_slug"):
        value = row.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=_json_default).encode("utf-8")).hexdigest()[:32] or f"{dataset}-{index}"


def _parse_ignition_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _ignition_source_dates(row: dict) -> tuple[datetime | None, datetime | None]:
    created = _parse_ignition_datetime(row.get("created_at") or row.get("created") or row.get("created_on"))
    updated = _parse_ignition_datetime(row.get("updated_at") or row.get("modified_at") or row.get("last_updated_at") or row.get("accepted_at") or row.get("sent_at"))
    return created, updated


def _update_ignition_sync_run(sync_run_id: str, **fields) -> dict:
    if not fields:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM ignition_sync_runs WHERE id = %s", (sync_run_id,))
                row = cursor.fetchone()
            connection.commit()
        return row or {}
    assignments = []
    values = []
    for key, value in fields.items():
        if key == "datasets_synced":
            assignments.append(f"{key} = %s::jsonb")
            values.append(json.dumps(value or {}, default=_json_default))
        else:
            assignments.append(f"{key} = %s")
            values.append(value)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE ignition_sync_runs SET {', '.join(assignments)} WHERE id = %s RETURNING *",
                (*values, sync_run_id),
            )
            row = cursor.fetchone()
        connection.commit()
    return row or {}


def _upsert_ignition_records(user: dict, practice_id: str, dataset: str, rows: list[dict]) -> int:
    now = utcnow()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            for index, row in enumerate(rows):
                external_id = _ignition_record_id(dataset, row, index)
                created_at, updated_at = _ignition_source_dates(row)
                cursor.execute(
                    """
                    INSERT INTO ignition_reporting_records (
                        user_id, practice_id, dataset, external_id, payload,
                        source_created_at, source_updated_at, synced_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (user_id, dataset, external_id) DO UPDATE
                    SET practice_id = EXCLUDED.practice_id,
                        payload = EXCLUDED.payload,
                        source_created_at = EXCLUDED.source_created_at,
                        source_updated_at = EXCLUDED.source_updated_at,
                        synced_at = EXCLUDED.synced_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        user["id"],
                        practice_id,
                        dataset,
                        external_id,
                        json.dumps(row, default=_json_default),
                        created_at,
                        updated_at,
                        now,
                        now,
                    ),
                )
        connection.commit()
    return len(rows)


def request_ignition_sync_run(user: dict) -> tuple[dict, bool]:
    try:
        get_ignition_connection_for_user(user["id"])
    except HTTPException:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Connect Ignition before syncing.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM ignition_sync_runs
                WHERE user_id = %s
                  AND status IN ('queued', 'running')
                ORDER BY COALESCE(heartbeat_at, started_at, created_at) DESC
                LIMIT 1
                """,
                (user["id"],),
            )
            active = cursor.fetchone()
            if active:
                connection.commit()
                return active, False
            cursor.execute(
                """
                INSERT INTO ignition_sync_runs (
                    user_id, status, current_step, summary,
                    fetched_count, processed_count, failed_count,
                    datasets_synced, heartbeat_at, created_at
                )
                VALUES (%s, 'queued', 'Queued', 'Ignition reporting sync queued.', 0, 0, 0, '{}'::jsonb, %s, %s)
                RETURNING *
                """,
                (user["id"], utcnow(), utcnow()),
            )
            row = cursor.fetchone()
        connection.commit()
    return row, True


def get_ignition_sync_run(user: dict, sync_run_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM ignition_sync_runs WHERE id = %s AND user_id = %s", (sync_run_id, user["id"]))
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ignition sync run not found.")
    return row


def active_ignition_sync_run_for_user(user: dict | None) -> dict | None:
    if not user or not user.get("id"):
        return None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM ignition_sync_runs
                WHERE user_id = %s
                  AND status IN ('queued', 'running')
                ORDER BY COALESCE(heartbeat_at, started_at, created_at) DESC
                LIMIT 1
                """,
                (user["id"],),
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def serialize_ignition_sync_run(row: dict | None) -> dict | None:
    if not row:
        return None
    status_value = row.get("status") or ""
    dataset_counts = row.get("datasets_synced") or {}
    if isinstance(dataset_counts, str):
        try:
            dataset_counts = json.loads(dataset_counts)
        except ValueError:
            dataset_counts = {}
    dataset_total = len(IGNITION_DATASETS)
    datasets_done = len([name for name, count in dataset_counts.items() if count is not None])
    if status_value in ("completed", "failed"):
        progress = 100
    elif status_value == "queued":
        progress = 4
    else:
        progress = min(95, max(8, round((datasets_done / max(dataset_total, 1)) * 95)))
    return {
        "id": str(row["id"]),
        "status": status_value,
        "currentStep": row.get("current_step") or "",
        "summary": row.get("summary") or "",
        "errorMessage": row.get("error_message") or "",
        "fetchedCount": int(row.get("fetched_count") or 0),
        "processedCount": int(row.get("processed_count") or 0),
        "failedCount": int(row.get("failed_count") or 0),
        "datasetsSynced": dataset_counts,
        "progress": progress,
        "createdAt": _iso(row.get("created_at")) or "",
        "startedAt": _iso(row.get("started_at")) or "",
        "heartbeatAt": _iso(row.get("heartbeat_at")) or "",
        "completedAt": _iso(row.get("completed_at")) or "",
        "isActive": status_value in ACTIVE_SYNC_STATUSES,
    }


def _money_from_ignition(value) -> Decimal:
    if isinstance(value, dict):
        return _money(value.get("amount"))
    return _money(value)


def _proposal_value(row: dict) -> Decimal:
    amount = _money_from_ignition(row.get("minimum_contract_value") or row.get("contract_value") or row.get("total_value"))
    if amount:
        return amount
    return sum(_money_from_ignition((service.get("pricing") or {}).get("minimum_contract_value")) for service in row.get("services") or [])


def _proposal_mrr(row: dict) -> Decimal:
    total = Decimal("0.00")
    for service in row.get("services") or []:
        pricing = service.get("pricing") or {}
        billing = service.get("billing") or {}
        period = _money_from_ignition(pricing.get("minimum_period_value"))
        if period:
            summary = str(billing.get("summary") or "").lower()
            if "year" in summary or "annual" in summary:
                total += period / Decimal("12")
            elif "quarter" in summary:
                total += period / Decimal("3")
            else:
                total += period
    if total:
        return total
    length = _money(row.get("minimum_contract_length") or 0)
    value = _proposal_value(row)
    return value / length if length > 0 else Decimal("0.00")


def _service_names_from_proposal(row: dict) -> list[str]:
    names = []
    for service in row.get("services") or []:
        text = " ".join(
            str(value or "")
            for value in (service.get("name"), service.get("description"), ((service.get("service_category") or {}).get("name")))
        ).strip()
        if text:
            names.append(text)
    return names or [row.get("name") or ""]


def _plan_label_for_text(text: str) -> str:
    lowered = str(text or "").lower()
    if "solo+" in lowered or "solo plus" in lowered:
        return "Solo+"
    if "mtd" in lowered and "solo" in lowered:
        return "Solo MTD"
    for label in ("Ultimate", "Premium", "Standard", "Starter", "Micro", "Solo"):
        if label.lower() in lowered:
            return label
    return "Other"


def _is_renewal_proposal(row: dict) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            row.get("name"),
            row.get("reference_number"),
            row.get("client_name"),
            " ".join(_service_names_from_proposal(row)),
            row.get("state"),
        )
    ).lower()
    renewal_markers = ("renewal", "renew", "existing client", "existing-client", "retainer continuation", "subscription renewal")
    return any(marker in text for marker in renewal_markers)


def _invoice_status(row: dict) -> str:
    return str(row.get("status") or row.get("state") or row.get("payment_status") or "").lower()


def _dataset_payloads(records: dict[str, list[dict]], dataset: str) -> list[dict]:
    return [record.get("payload") or {} for record in records.get(dataset, [])]


def _ignition_dashboard(records: dict[str, list[dict]]) -> dict:
    today = utcnow().date()
    month_start = date(today.year, today.month, 1)
    proposals = _dataset_payloads(records, "proposals")
    invoices = _dataset_payloads(records, "invoices")
    payments = _dataset_payloads(records, "payments")
    collections = _dataset_payloads(records, "collections")
    clients = _dataset_payloads(records, "clients")
    deals = _dataset_payloads(records, "deals")

    sent_mtd = [row for row in proposals if (_parse_optional_iso_date(row.get("sent_at") or row.get("created_at")) or date.min) >= month_start]
    accepted = [row for row in proposals if str(row.get("state") or "").lower() == "accepted" or row.get("accepted_at")]
    accepted_mtd = [row for row in accepted if (_parse_optional_iso_date(row.get("accepted_at") or row.get("created_at")) or date.min) >= month_start]
    new_clients_mtd = [row for row in clients if (_parse_optional_iso_date(row.get("created_at")) or date.min) >= month_start]
    payments_mtd = [row for row in payments if (_parse_optional_iso_date(row.get("paid_at") or row.get("payment_date") or row.get("created_at")) or date.min) >= month_start]
    outstanding_invoices = [
        row for row in invoices
        if _invoice_status(row) not in ("paid", "void", "voided", "cancelled", "canceled")
        and _money_from_ignition(row.get("amount_due") or row.get("balance") or row.get("outstanding_amount") or row.get("total")) > 0
    ]
    overdue_invoices = [row for row in outstanding_invoices if (_parse_optional_iso_date(row.get("due_date")) or today) < today]
    awaiting = [row for row in proposals if str(row.get("state") or "").lower() in ("awaiting_acceptance", "sent")]
    renewal_proposals = [row for row in proposals if _is_renewal_proposal(row)]
    new_work_proposals = [row for row in proposals if not _is_renewal_proposal(row)]
    accepted_renewals = [row for row in renewal_proposals if str(row.get("state") or "").lower() == "accepted" or row.get("accepted_at")]
    accepted_new_work = [row for row in new_work_proposals if str(row.get("state") or "").lower() == "accepted" or row.get("accepted_at")]
    pipeline_value = sum(_money_from_ignition(row.get("value") or row.get("amount") or row.get("total_value")) for row in deals)
    collection_fees = sum(_money_from_ignition(row.get("fee") or row.get("fees") or row.get("processing_fee")) for row in collections)
    collection_clawbacks = sum(_money_from_ignition(row.get("clawback") or row.get("clawbacks")) for row in collections)
    collection_disbursements = sum(_money_from_ignition(row.get("net_disbursement") or row.get("disbursement") or row.get("amount")) for row in collections)

    service_rollups: dict[str, dict] = {}
    for proposal in proposals:
        state_value = str(proposal.get("state") or "").lower()
        proposal_mrr = _proposal_mrr(proposal)
        labels = {_plan_label_for_text(name) for name in _service_names_from_proposal(proposal)}
        for label in labels:
            rollup = service_rollups.setdefault(label, {"name": label, "proposals": 0, "accepted": 0, "mrr": Decimal("0.00"), "contractValue": Decimal("0.00")})
            rollup["proposals"] += 1
            if state_value == "accepted" or proposal.get("accepted_at"):
                rollup["accepted"] += 1
                rollup["mrr"] += proposal_mrr
                rollup["contractValue"] += _proposal_value(proposal)
    service_performance = []
    for label in (*IGNITION_PLAN_LABELS, "Other"):
        rollup = service_rollups.get(label, {"name": label, "proposals": 0, "accepted": 0, "mrr": Decimal("0.00"), "contractValue": Decimal("0.00")})
        service_performance.append({
            "name": rollup["name"],
            "proposals": rollup["proposals"],
            "accepted": rollup["accepted"],
            "mrr": float(_money(rollup["mrr"])),
            "contractValue": float(_money(rollup["contractValue"])),
        })

    creator_rollups: dict[str, dict] = {}
    for proposal in proposals:
        creator = proposal.get("creator") or proposal.get("sender") or {}
        manager = creator.get("name") if isinstance(creator, dict) else str(creator or "")
        manager = manager or "Unassigned"
        rollup = creator_rollups.setdefault(manager, {"name": manager, "proposals": 0, "accepted": 0, "value": Decimal("0.00")})
        rollup["proposals"] += 1
        if str(proposal.get("state") or "").lower() == "accepted" or proposal.get("accepted_at"):
            rollup["accepted"] += 1
            rollup["value"] += _proposal_value(proposal)

    accepted_value = sum(_proposal_value(row) for row in accepted)
    accepted_value_mtd = sum(_proposal_value(row) for row in accepted_mtd)
    expected_mrr = sum(_proposal_mrr(row) for row in accepted)
    awaiting_value = sum(_proposal_value(row) for row in awaiting)
    payment_total_mtd = sum(_money_from_ignition(row.get("amount") or row.get("total") or row.get("payment_amount")) for row in payments_mtd)
    outstanding_total = sum(_money_from_ignition(row.get("amount_due") or row.get("balance") or row.get("outstanding_amount") or row.get("total")) for row in outstanding_invoices)
    conversion_rate = (len(accepted) / len(proposals) * 100) if proposals else 0
    mtd_conversion_rate = (len(accepted_mtd) / len(sent_mtd) * 100) if sent_mtd else 0

    return {
        "totals": {
            "clientCount": len(clients),
            "newClientsMtd": len(new_clients_mtd),
            "proposalsCreated": len(proposals),
            "proposalsSentMtd": len(sent_mtd),
            "proposalsAccepted": len(accepted),
            "proposalsAcceptedMtd": len(accepted_mtd),
            "proposalConversionRate": round(conversion_rate, 1),
            "mtdProposalConversionRate": round(mtd_conversion_rate, 1),
            "acceptedProposalValue": float(_money(accepted_value)),
            "acceptedProposalValueMtd": float(_money(accepted_value_mtd)),
            "expectedMrr": float(_money(expected_mrr)),
            "expectedArr": float(_money(expected_mrr * Decimal("12"))),
            "awaitingProposalValue": float(_money(awaiting_value)),
            "outstandingInvoices": len(outstanding_invoices),
            "overdueInvoices": len(overdue_invoices),
            "outstandingInvoiceValue": float(_money(outstanding_total)),
            "paymentsReceivedMtd": float(_money(payment_total_mtd)),
            "collectionFees": float(_money(collection_fees)),
            "collectionClawbacks": float(_money(collection_clawbacks)),
            "collectionDisbursements": float(_money(collection_disbursements)),
            "pipelineValue": float(_money(pipeline_value)),
        },
        "servicePerformance": service_performance,
        "managerPerformance": sorted(
            [
                {
                    "name": row["name"],
                    "proposals": row["proposals"],
                    "accepted": row["accepted"],
                    "value": float(_money(row["value"])),
                    "conversionRate": round((row["accepted"] / row["proposals"] * 100) if row["proposals"] else 0, 1),
                }
                for row in creator_rollups.values()
            ],
            key=lambda row: row["value"],
            reverse=True,
        )[:12],
        "awaitingProposals": [
            {
                "name": row.get("name") or row.get("reference_number") or "Proposal",
                "clientName": row.get("client_name") or "",
                "value": float(_money(_proposal_value(row))),
                "createdAt": row.get("created_at") or "",
                "link": row.get("link") or "",
            }
            for row in sorted(awaiting, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:10]
        ],
        "renewalAnalysis": {
            "renewalCount": len(renewal_proposals),
            "renewalAccepted": len(accepted_renewals),
            "renewalValue": float(_money(sum(_proposal_value(row) for row in renewal_proposals))),
            "renewalAcceptedValue": float(_money(sum(_proposal_value(row) for row in accepted_renewals))),
            "renewalConversionRate": round((len(accepted_renewals) / len(renewal_proposals) * 100) if renewal_proposals else 0, 1),
            "newWorkCount": len(new_work_proposals),
            "newWorkAccepted": len(accepted_new_work),
            "newWorkValue": float(_money(sum(_proposal_value(row) for row in new_work_proposals))),
            "newWorkAcceptedValue": float(_money(sum(_proposal_value(row) for row in accepted_new_work))),
            "newWorkConversionRate": round((len(accepted_new_work) / len(new_work_proposals) * 100) if new_work_proposals else 0, 1),
        },
    }


def _ignition_records_for_user(user: dict) -> dict[str, list[dict]]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM ignition_reporting_records
                WHERE user_id = %s
                ORDER BY dataset ASC, COALESCE(source_created_at, created_at) DESC
                """,
                (user["id"],),
            )
            rows = cursor.fetchall()
        connection.commit()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row.get("dataset") or ""].append(row)
    return grouped


def ignition_payload(user: dict) -> dict:
    try:
        connection = get_ignition_connection_for_user(user["id"])
    except HTTPException:
        connection = None
    active_run = active_ignition_sync_run_for_user(user)
    with get_connection() as db:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM ignition_sync_runs WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
                (user["id"],),
            )
            latest_run = cursor.fetchone()
        db.commit()
    records = _ignition_records_for_user(user)
    dataset_counts = {dataset: len(records.get(dataset, [])) for dataset, _ in IGNITION_DATASETS}
    return {
        "connection": {
            "connected": bool(connection and connection.get("status") == "connected"),
            "oauthConfigured": ignition_oauth_configured(),
            "practiceId": connection.get("practice_id") if connection else "",
            "practiceName": connection.get("practice_name") if connection else "",
            "lastSyncAt": _iso(connection.get("last_sync_at")) if connection else "",
            "status": connection.get("status") if connection else "disconnected",
            "errorMessage": connection.get("error_message") if connection else "",
        },
        "datasetCounts": dataset_counts,
        "dashboard": _ignition_dashboard(records),
        "syncRun": serialize_ignition_sync_run(active_run or latest_run),
        "activeSyncRun": serialize_ignition_sync_run(active_run),
    }


def run_ignition_sync_job(user: dict, sync_run_id: str) -> None:
    try:
        asyncio.run(run_ignition_sync(user, sync_run_id))
    except Exception as exc:
        logger.exception("Background Ignition sync failed")
        _update_ignition_sync_run(
            sync_run_id,
            status="failed",
            current_step="Ignition sync failed",
            summary="Ignition sync failed before it could complete.",
            error_message=_sync_error_message(exc),
            failed_count=1,
            heartbeat_at=utcnow(),
            completed_at=utcnow(),
        )


async def run_ignition_sync(user: dict, sync_run_id: str) -> dict:
    connection = get_ignition_connection_for_user(user["id"])
    dataset_counts: dict[str, int] = {}
    total_fetched = 0
    total_processed = 0
    _update_ignition_sync_run(
        sync_run_id,
        status="running",
        current_step="Connecting to Ignition",
        summary="Refreshing Ignition OAuth access and preparing Reporting API sync.",
        started_at=utcnow(),
        heartbeat_at=utcnow(),
    )
    practice = {"id": connection.get("practice_id") or "", "name": connection.get("practice_name") or ""}
    for dataset, endpoint in IGNITION_DATASETS:
        _update_ignition_sync_run(
            sync_run_id,
            current_step=f"Importing {dataset.replace('_', ' ')}",
            summary=f"Fetching {dataset.replace('_', ' ')} from Ignition Reporting API.",
            heartbeat_at=utcnow(),
            datasets_synced=dataset_counts,
            fetched_count=total_fetched,
            processed_count=total_processed,
        )
        try:
            rows, meta = await fetch_ignition_collection(connection, endpoint)
        except HTTPException as exc:
            if dataset in ("deals", "deal_stages"):
                dataset_counts[dataset] = 0
                continue
            raise exc
        practice_meta = (meta or {}).get("practice") or {}
        if practice_meta.get("id") or practice_meta.get("name"):
            practice = {"id": str(practice_meta.get("id") or practice.get("id") or ""), "name": practice_meta.get("name") or practice.get("name") or ""}
        processed = _upsert_ignition_records(user, practice.get("id") or "", dataset, rows)
        dataset_counts[dataset] = processed
        total_fetched += len(rows)
        total_processed += processed
        record_audit_event("ignition_sync_run", str(sync_run_id), f"ignition.{dataset}.synced", {"dataset": dataset, "records": processed}, user["id"])
    summary = f"Ignition sync complete: imported {total_processed} records across {len(dataset_counts)} reporting datasets."
    completed = _update_ignition_sync_run(
        sync_run_id,
        status="completed",
        current_step="Ignition sync complete",
        summary=summary,
        fetched_count=total_fetched,
        processed_count=total_processed,
        datasets_synced=dataset_counts,
        heartbeat_at=utcnow(),
        completed_at=utcnow(),
    )
    with get_connection() as db:
        with db.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ignition_connections
                SET practice_id = COALESCE(NULLIF(%s, ''), practice_id),
                    practice_name = COALESCE(NULLIF(%s, ''), practice_name),
                    status = 'connected',
                    error_message = '',
                    last_sync_at = %s,
                    updated_at = %s
                WHERE user_id = %s
                """,
                (practice.get("id") or "", practice.get("name") or "", utcnow(), utcnow(), user["id"]),
            )
        db.commit()
    record_audit_event("ignition_sync_run", str(sync_run_id), "ignition.sync.completed", {"summary": summary}, user["id"])
    return completed


def _xero_payload_date(value) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not value:
        return ""
    text = str(value)
    match = re.match(r"/Date\((-?\d+)", text)
    if match:
        return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc).date().isoformat()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else text


def _xero_contact_where(contact_id: str) -> str:
    return f'Contact.ContactID==Guid("{contact_id}")'


def _xero_transaction_contact_id(payload: dict) -> str:
    contact = payload.get("Contact") or {}
    return str(contact.get("ContactID") or payload.get("ContactID") or "").lower()


def _xero_line_items(line_items: list[dict] | None) -> list[dict]:
    items = []
    for item in line_items or []:
        description = str(item.get("Description") or item.get("Item", {}).get("Name") or "").strip()
        items.append(
            {
                "description": description,
                "quantity": item.get("Quantity"),
                "unitAmount": item.get("UnitAmount"),
                "lineAmount": item.get("LineAmount"),
                "accountCode": item.get("AccountCode"),
                "taxType": item.get("TaxType"),
            }
        )
    return items


def _xero_allocations(allocations: list[dict] | None) -> list[dict]:
    items = []
    for allocation in allocations or []:
        invoice = allocation.get("Invoice") or {}
        items.append(
            {
                "id": allocation.get("AllocationID") or allocation.get("ID") or "",
                "invoiceId": invoice.get("InvoiceID") or "",
                "invoiceNumber": invoice.get("InvoiceNumber") or "",
                "date": _xero_payload_date(allocation.get("DateString") or allocation.get("Date")),
                "amount": _float(allocation.get("Amount")),
            }
        )
    return items


def _remaining_credit(source: dict) -> Decimal:
    for key in ("RemainingCredit", "AmountDue"):
        if source.get(key) is not None:
            return _money(source.get(key))
    total = _money(source.get("Total"))
    allocated = sum(_money(item.get("Amount")) for item in source.get("Allocations") or [])
    payments = sum(_money(item.get("Amount")) for item in source.get("Payments") or [])
    return max(Decimal("0.00"), total - allocated - payments)


def _serialize_credit_note_transaction(credit_note: dict) -> dict:
    remaining = _remaining_credit(credit_note)
    total = _money(credit_note.get("Total"))
    return {
        "sourceType": "creditNote",
        "id": credit_note.get("CreditNoteID") or credit_note.get("ID") or "",
        "number": credit_note.get("CreditNoteNumber") or "",
        "reference": credit_note.get("Reference") or "",
        "date": _xero_payload_date(credit_note.get("DateString") or credit_note.get("Date")),
        "status": credit_note.get("Status") or "",
        "type": credit_note.get("Type") or "",
        "currencyCode": credit_note.get("CurrencyCode") or "GBP",
        "total": float(total),
        "remainingCredit": float(remaining),
        "appliedAmount": float(max(Decimal("0.00"), total - remaining)),
        "lineItems": _xero_line_items(credit_note.get("LineItems")),
        "allocations": _xero_allocations(credit_note.get("Allocations")),
        "contactId": _xero_transaction_contact_id(credit_note),
    }


def _serialize_overpayment_transaction(overpayment: dict) -> dict:
    remaining = _remaining_credit(overpayment)
    total = _money(overpayment.get("Total"))
    return {
        "sourceType": "overpayment",
        "id": overpayment.get("OverpaymentID") or overpayment.get("ID") or "",
        "number": overpayment.get("OverpaymentNumber") or overpayment.get("Reference") or overpayment.get("OverpaymentID") or "",
        "reference": overpayment.get("Reference") or "",
        "date": _xero_payload_date(overpayment.get("DateString") or overpayment.get("Date")),
        "status": overpayment.get("Status") or "",
        "type": overpayment.get("Type") or "",
        "currencyCode": overpayment.get("CurrencyCode") or "GBP",
        "total": float(total),
        "remainingCredit": float(remaining),
        "appliedAmount": float(max(Decimal("0.00"), total - remaining)),
        "lineItems": _xero_line_items(overpayment.get("LineItems")),
        "allocations": _xero_allocations(overpayment.get("Allocations")),
        "contactId": _xero_transaction_contact_id(overpayment),
    }


def _serialize_xero_invoice_transaction(invoice: dict, local_lookup: dict[str, dict]) -> dict:
    normalised = normalise_invoice(invoice)
    local = local_lookup.get(normalised["xero_invoice_id"], {})
    return {
        "id": str(local.get("id") or ""),
        "xeroInvoiceId": normalised["xero_invoice_id"],
        "invoiceNumber": normalised["invoice_number"],
        "status": normalised["status"],
        "dueDate": _iso(normalised["due_date"]),
        "invoiceDate": _iso(normalised["invoice_date"]),
        "description": normalised["description"],
        "lineItems": normalised["line_items"],
        "currencyCode": normalised["currency_code"] or "GBP",
        "total": _float(normalised["total"]),
        "amountDue": _float(normalised["amount_due"]),
        "amountPaid": _float(normalised["amount_paid"]),
        "contactId": str(normalised.get("xero_contact_id") or "").lower(),
    }


def _validate_customer_xero_access(customer_id: str, user: dict) -> tuple[dict, dict]:
    try:
        parsed_customer_id = UUID(str(customer_id))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer id.") from exc

    connection_row = get_xero_connection_for_user(user["id"])
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM customers WHERE id = %s AND tenant_id = %s",
                (parsed_customer_id, connection_row["tenant_id"]),
            )
            customer = cursor.fetchone()
        connection.commit()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found for this Xero organisation.")
    if not customer.get("xero_contact_id"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Customer is not linked to a Xero contact.")
    return customer, connection_row


def _local_invoice_lookup(customer_id: str) -> dict[str, dict]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, xero_invoice_id, invoice_number, amount_due
                FROM invoices
                WHERE customer_id = %s
                  AND xero_invoice_id IS NOT NULL
                """,
                (customer_id,),
            )
            rows = cursor.fetchall()
        connection.commit()
    return {row["xero_invoice_id"]: row for row in rows if row.get("xero_invoice_id")}


async def _fetch_contact_invoices(connection_row: dict, contact_id: str) -> list[dict]:
    params = {
        "ContactIDs": contact_id,
        "where": OUTSTANDING_INVOICE_WHERE,
        "order": "DueDate ASC",
    }
    try:
        return await fetch_paginated_collection(connection_row, INVOICES_URL, "Invoices", params=params)
    except HTTPException:
        records = await fetch_paginated_collection(
            connection_row,
            INVOICES_URL,
            "Invoices",
            params={"where": f"{OUTSTANDING_INVOICE_WHERE}&&{_xero_contact_where(contact_id)}", "order": "DueDate ASC"},
        )
        return [record for record in records if _xero_transaction_contact_id(record) == contact_id.lower()]


async def _fetch_contact_credit_sources(
    connection_row: dict,
    url: str,
    collection_key: str,
    contact_id: str,
    where: str,
    fallback_where: str,
    serializer,
    include_allocated: bool = False,
) -> list[dict]:
    try:
        records = await fetch_paginated_collection(connection_row, url, collection_key, params={"where": where, "order": "Date DESC"})
    except HTTPException:
        records = await fetch_paginated_collection(connection_row, url, collection_key, params={"where": fallback_where, "order": "Date DESC"})

    contact_id_lower = contact_id.lower()
    items = []
    for record in records:
        if _xero_transaction_contact_id(record) != contact_id_lower:
            continue
        if url == OVERPAYMENTS_URL:
            overpayment_type = str(record.get("Type") or "").upper()
            if overpayment_type and "RECEIVE" not in overpayment_type:
                continue
        item = serializer(record)
        has_remaining_credit = _money(item.get("remainingCredit")) > 0
        has_allocated_credit = _money(item.get("appliedAmount")) > 0 or bool(item.get("allocations"))
        if has_remaining_credit or (include_allocated and has_allocated_credit):
            items.append(item)
    return items


async def _customer_xero_transactions_payload(customer: dict, connection_row: dict) -> dict:
    contact_id = customer["xero_contact_id"]
    local_lookup = _local_invoice_lookup(customer["id"])
    raw_invoices = await _fetch_contact_invoices(connection_row, contact_id)
    outstanding_invoices = []
    for raw_invoice in raw_invoices:
        invoice = _serialize_xero_invoice_transaction(raw_invoice, local_lookup)
        if invoice["contactId"] and invoice["contactId"] != contact_id.lower():
            continue
        if _money(invoice["amountDue"]) > 0:
            outstanding_invoices.append(invoice)

    credit_where = f'{_xero_contact_where(contact_id)}&&Type=="ACCRECCREDIT"&&Status=="AUTHORISED"'
    credit_notes = await _fetch_contact_credit_sources(
        connection_row,
        CREDIT_NOTES_URL,
        "CreditNotes",
        contact_id,
        credit_where,
        'Type=="ACCRECCREDIT"&&Status=="AUTHORISED"',
        _serialize_credit_note_transaction,
        include_allocated=True,
    )
    overpayment_where = f'{_xero_contact_where(contact_id)}&&Status=="AUTHORISED"'
    overpayments = await _fetch_contact_credit_sources(
        connection_row,
        OVERPAYMENTS_URL,
        "Overpayments",
        contact_id,
        overpayment_where,
        'Status=="AUTHORISED"',
        _serialize_overpayment_transaction,
        include_allocated=True,
    )

    unallocated_credits = [item for item in credit_notes if _money(item.get("remainingCredit")) > 0]
    unallocated_overpayments = [item for item in overpayments if _money(item.get("remainingCredit")) > 0]
    allocated_credit_sources = [
        item
        for item in [*credit_notes, *overpayments]
        if _money(item.get("appliedAmount")) > 0 or item.get("allocations")
    ]
    outstanding_total = sum(_money(invoice.get("amountDue")) for invoice in outstanding_invoices)
    credit_total = sum(_money(item.get("remainingCredit")) for item in [*unallocated_credits, *unallocated_overpayments])
    allocated_credit_total = sum(_money(item.get("appliedAmount")) for item in allocated_credit_sources)
    line_count = sum(len(invoice.get("lineItems") or []) for invoice in outstanding_invoices)
    return {
        "customerId": str(customer["id"]),
        "xeroContactId": contact_id,
        "fetchedAt": utcnow().isoformat(),
        "outstandingInvoices": outstanding_invoices,
        "unallocatedCredits": unallocated_credits,
        "overpayments": unallocated_overpayments,
        "allocatedCredits": allocated_credit_sources,
        "summary": {
            "outstandingTotal": float(outstanding_total),
            "outstandingInvoiceCount": len(outstanding_invoices),
            "outstandingLineCount": line_count,
            "unallocatedCreditTotal": float(credit_total),
            "unallocatedCreditCount": len(unallocated_credits) + len(unallocated_overpayments),
            "overpaymentCount": len(unallocated_overpayments),
            "allocatedCreditTotal": float(allocated_credit_total),
            "allocatedCreditCount": len(allocated_credit_sources),
        },
    }


async def customer_xero_transactions(customer_id: str, user: dict) -> dict:
    customer, connection_row = _validate_customer_xero_access(customer_id, user)
    return await _customer_xero_transactions_payload(customer, connection_row)


async def _refresh_local_invoice_from_xero(
    connection_row: dict,
    customer_id: str,
    xero_invoice_id: str,
    user_id: str,
    status_note: str,
) -> str:
    payload = await xero_api_get(connection_row, f"{INVOICES_URL}/{xero_invoice_id}")
    raw_invoice = ((payload or {}).get("Invoices") or [{}])[0]
    if not raw_invoice.get("InvoiceID"):
        return ""
    invoice = normalise_invoice(raw_invoice)
    now = utcnow()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO invoices (
                    customer_id, xero_invoice_id, invoice_number, status, due_date, invoice_date,
                    description, line_items, currency_code, total, amount_due, amount_paid, xero_updated_at, synced_at, updated_at
                )
                VALUES (
                    %(customer_id)s, %(xero_invoice_id)s, %(invoice_number)s, %(status)s, %(due_date)s, %(invoice_date)s,
                    %(description)s, %(line_items_json)s::jsonb, %(currency_code)s, %(total)s, %(amount_due)s, %(amount_paid)s, %(xero_updated_at)s, %(synced_at)s, %(updated_at)s
                )
                ON CONFLICT (xero_invoice_id) DO UPDATE
                SET customer_id = EXCLUDED.customer_id,
                    invoice_number = EXCLUDED.invoice_number,
                    status = EXCLUDED.status,
                    due_date = EXCLUDED.due_date,
                    invoice_date = EXCLUDED.invoice_date,
                    description = EXCLUDED.description,
                    line_items = EXCLUDED.line_items,
                    currency_code = EXCLUDED.currency_code,
                    total = EXCLUDED.total,
                    amount_due = EXCLUDED.amount_due,
                    amount_paid = EXCLUDED.amount_paid,
                    xero_updated_at = EXCLUDED.xero_updated_at,
                    synced_at = EXCLUDED.synced_at,
                    updated_at = EXCLUDED.updated_at
                RETURNING id
                """,
                {
                    **invoice,
                    "line_items_json": json.dumps(invoice.get("line_items") or [], default=_json_default),
                    "customer_id": customer_id,
                    "synced_at": now,
                    "updated_at": now,
                },
            )
            stored = cursor.fetchone()
            if stored:
                cursor.execute(
                    """
                    INSERT INTO invoice_status_history (invoice_id, status, note, changed_by_user_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (stored["id"], "Credit allocated", status_note, user_id),
                )
            _refresh_customer_totals(cursor, connection_row["tenant_id"], now)
        connection.commit()
    return str(stored["id"]) if stored else ""


async def allocate_customer_credit(user: dict, customer_id: str, payload: dict) -> dict:
    customer, connection_row = _validate_customer_xero_access(customer_id, user)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Allocation payload is required.")

    source_type_input = str(payload.get("sourceType") or "").strip()
    source_type = {
        "creditNote": "creditNote",
        "credit_note": "creditNote",
        "credit-note": "creditNote",
        "overpayment": "overpayment",
    }.get(source_type_input)
    source_id = str(payload.get("sourceId") or "").strip()
    xero_invoice_id = str(payload.get("invoiceId") or payload.get("xeroInvoiceId") or "").strip()
    amount = _money(payload.get("amount"))
    if source_type is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select a credit note or overpayment to allocate.")
    if not source_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Allocation source id is required.")
    if not xero_invoice_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invoice id is required.")
    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Allocation amount must be greater than zero.")

    transactions = await _customer_xero_transactions_payload(customer, connection_row)
    invoice = next((item for item in transactions["outstandingInvoices"] if item.get("xeroInvoiceId") == xero_invoice_id), None)
    sources = transactions["unallocatedCredits"] if source_type == "creditNote" else transactions["overpayments"]
    source = next((item for item in sources if item.get("id") == source_id), None)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected invoice is not outstanding for this Xero contact.")
    if source is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected credit source is not available for this Xero contact.")

    remaining_credit = _money(source.get("remainingCredit"))
    amount_due = _money(invoice.get("amountDue"))
    maximum = min(remaining_credit, amount_due)
    if amount > maximum:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Allocation exceeds the available amount. Maximum allocation is {maximum:.2f}.",
        )

    today = utcnow().date()
    allocation_payload = {
        "Invoice": {"InvoiceID": xero_invoice_id},
        "Amount": float(amount),
        "Date": today.isoformat(),
    }
    if source_type == "creditNote":
        allocation_response = await allocate_credit_note(connection_row, source_id, allocation_payload)
        source_label = f"credit note {source.get('number') or source_id}"
    else:
        allocation_response = await allocate_overpayment(connection_row, source_id, allocation_payload)
        source_label = f"overpayment {source.get('number') or source_id}"

    invoice_number = invoice.get("invoiceNumber") or xero_invoice_id
    status_note = _with_jenius_signature(
        f"Allocated {source_label} to invoice {invoice_number} for "
        f"{source.get('currencyCode') or invoice.get('currencyCode') or 'GBP'} {amount:,.2f}."
    )
    local_invoice_id = await _refresh_local_invoice_from_xero(
        connection_row,
        customer["id"],
        xero_invoice_id,
        user["id"],
        status_note,
    )
    xero_note_synced = True
    xero_note_error = ""
    for resource, resource_id in (("Invoices", xero_invoice_id), ("Contacts", customer.get("xero_contact_id"))):
        if not resource_id:
            continue
        try:
            await create_history_record(connection_row, resource, resource_id, status_note)
        except Exception as exc:
            xero_note_synced = False
            xero_note_error = _sync_error_message(exc)
            logger.exception("Unable to add Xero allocation history note to %s %s", resource, resource_id)
    record_audit_event(
        "customer",
        str(customer["id"]),
        "xero.allocation.created",
        {
            "customer_name": customer.get("name"),
            "source_type": source_type,
            "source_id": source_id,
            "source_number": source.get("number"),
            "invoice_id": xero_invoice_id,
            "local_invoice_id": local_invoice_id or invoice.get("id"),
            "invoice_number": invoice_number,
            "amount": float(amount),
            "currency_code": source.get("currencyCode") or invoice.get("currencyCode") or "GBP",
            "allocation": allocation_response,
            "xero_note_synced": xero_note_synced,
            "xero_note_error": xero_note_error,
        },
        user["id"],
    )
    refreshed_transactions = await _customer_xero_transactions_payload(customer, connection_row)
    return {
        "status": "ok",
        "allocation": {
            "sourceType": source_type,
            "sourceId": source_id,
            "invoiceId": xero_invoice_id,
            "amount": float(amount),
            "date": today.isoformat(),
        },
        "xeroNoteSynced": xero_note_synced,
        "xeroNoteError": xero_note_error,
        "transactions": refreshed_transactions,
        "panel": panel_payload(user),
    }


def list_customers() -> list[dict]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM customers
                ORDER BY overdue_amount DESC, total_due DESC, name ASC
                """
            )
            rows = cursor.fetchall()
        connection.commit()
    return rows


def customer_detail(customer_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
            customer = cursor.fetchone()
            if customer is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

            cursor.execute(
                """
                SELECT *
                FROM invoices
                WHERE customer_id = %s
                ORDER BY due_date ASC NULLS LAST, invoice_number ASC
                """,
                (customer_id,),
            )
            invoices = cursor.fetchall()
        connection.commit()

    for invoice in invoices:
        due_date = invoice["due_date"]
        overdue_days = 0 if due_date is None else max((utcnow().date() - due_date).days, 0)
        invoice["late_payment"] = _late_payment_breakdown(float(invoice["amount_due"]), overdue_days)
        invoice["overdue_days"] = overdue_days

    return {"customer": customer, "invoices": invoices}


def invoice_detail(invoice_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT invoices.*, customers.name AS customer_name
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE invoices.id = %s
                """,
                (invoice_id,),
            )
            invoice = cursor.fetchone()
            if invoice is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found.")

            cursor.execute(
                "SELECT notes.*, users.full_name FROM notes LEFT JOIN users ON users.id = notes.user_id WHERE invoice_id = %s ORDER BY created_at DESC",
                (invoice_id,),
            )
            notes = cursor.fetchall()
            cursor.execute(
                "SELECT payment_promises.*, users.full_name FROM payment_promises LEFT JOIN users ON users.id = payment_promises.created_by_user_id WHERE invoice_id = %s ORDER BY created_at DESC",
                (invoice_id,),
            )
            promises = cursor.fetchall()
            cursor.execute(
                "SELECT invoice_status_history.*, users.full_name FROM invoice_status_history LEFT JOIN users ON users.id = invoice_status_history.changed_by_user_id WHERE invoice_id = %s ORDER BY created_at DESC",
                (invoice_id,),
            )
            statuses = cursor.fetchall()
            cursor.execute(
                """
                SELECT *
                FROM audit_events
                WHERE entity_id = %s
                   OR entity_id = %s
                ORDER BY created_at DESC
                """,
                (invoice_id, invoice["customer_id"]),
            )
            audit = cursor.fetchall()
        connection.commit()

    overdue_days = 0 if invoice["due_date"] is None else max((utcnow().date() - invoice["due_date"]).days, 0)
    invoice["late_payment"] = _late_payment_breakdown(float(invoice["amount_due"]), overdue_days)
    invoice["overdue_days"] = overdue_days
    return {
        "invoice": invoice,
        "notes": notes,
        "promises": promises,
        "statuses": statuses,
        "audit": audit,
    }


def add_note(invoice_id: str, user: dict, body: str) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO notes (invoice_id, user_id, body) VALUES (%s, %s, %s)",
                (invoice_id, user["id"], body),
            )
            cursor.execute(
                "UPDATE invoices SET notes_summary = %s, updated_at = %s WHERE id = %s",
                (body[:200], utcnow(), invoice_id),
            )
        connection.commit()
    record_audit_event("invoice", invoice_id, "note.added", {"body": body}, user["id"])


def add_customer_note(customer_id: str, user: dict, body: str) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM customers WHERE id = %s", (customer_id,))
            if cursor.fetchone() is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")
            cursor.execute(
                "INSERT INTO customer_notes (customer_id, user_id, body) VALUES (%s, %s, %s)",
                (customer_id, user["id"], body),
            )
        connection.commit()
    record_audit_event("customer", customer_id, "client.note.added", {"body": body}, user["id"])


def _format_xero_note(user: dict, body: str) -> str:
    author = user.get("full_name") or user.get("email") or "Credit Control Console user"
    return _with_jenius_signature(f"Credit Control Console note from {author}: {body.strip()}")


async def sync_invoice_workflow_note_to_xero(invoice_id: str, user: dict, body: str, event_type: str = "xero.workflow_note") -> dict:
    note_body = _with_jenius_signature(body)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT invoices.id,
                       invoices.xero_invoice_id,
                       invoices.invoice_number,
                       customers.id AS customer_id,
                       customers.name AS customer_name,
                       customers.xero_contact_id
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE invoices.id = %s
                """,
                (invoice_id,),
            )
            invoice = cursor.fetchone()
        connection.commit()

    if invoice is None:
        return {
            "synced": False,
            "invoiceNoteSynced": False,
            "contactNoteSynced": False,
            "error": "Invoice not found.",
        }

    targets = {
        "Invoices": invoice.get("xero_invoice_id"),
        "Contacts": invoice.get("xero_contact_id"),
    }
    results = {
        "Invoices": {"synced": False, "error": ""},
        "Contacts": {"synced": False, "error": ""},
    }

    try:
        connection_row = get_xero_connection_for_user(user["id"])
    except Exception as exc:
        error = _sync_error_message(exc)
        record_audit_event(
            "invoice",
            invoice_id,
            f"{event_type}.failed",
            {"error": error, "invoice_number": invoice.get("invoice_number"), "detail": _sync_error_payload(exc)},
            user["id"],
        )
        return {
            "synced": False,
            "invoiceNoteSynced": False,
            "contactNoteSynced": False,
            "error": error,
        }

    for resource, resource_id in targets.items():
        if not resource_id:
            results[resource]["error"] = (
                "Invoice is not linked to a Xero invoice."
                if resource == "Invoices"
                else "Customer is not linked to a Xero contact."
            )
            continue
        try:
            await create_history_record(connection_row, resource, resource_id, note_body)
            results[resource]["synced"] = True
        except Exception as exc:
            error = _sync_error_message(exc)
            results[resource]["error"] = error
            logger.exception("Unable to add Xero workflow history note to %s %s", resource, resource_id)

    errors = [result["error"] for result in results.values() if result["error"]]
    synced = results["Invoices"]["synced"] and results["Contacts"]["synced"]
    record_audit_event(
        "invoice",
        invoice_id,
        f"{event_type}.{'synced' if synced else 'failed'}",
        {
            "invoice_number": invoice.get("invoice_number"),
            "customer_id": str(invoice.get("customer_id")),
            "customer_name": invoice.get("customer_name"),
            "xero_invoice_id": invoice.get("xero_invoice_id"),
            "xero_contact_id": invoice.get("xero_contact_id"),
            "invoice_note_synced": results["Invoices"]["synced"],
            "contact_note_synced": results["Contacts"]["synced"],
            "errors": errors,
        },
        user["id"],
    )
    return {
        "synced": synced,
        "invoiceNoteSynced": results["Invoices"]["synced"],
        "contactNoteSynced": results["Contacts"]["synced"],
        "error": "; ".join(errors),
    }


async def sync_invoice_note_to_xero(invoice_id: str, user: dict, body: str) -> dict:
    return await sync_invoice_workflow_note_to_xero(invoice_id, user, _format_xero_note(user, body), "xero.note")


async def sync_customer_note_to_xero(customer_id: str, user: dict, body: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, xero_contact_id, name
                FROM customers
                WHERE id = %s
                """,
                (customer_id,),
            )
            customer = cursor.fetchone()
        connection.commit()

    if customer is None:
        return {"synced": False, "error": "Customer not found."}
    if not customer.get("xero_contact_id"):
        return {"synced": False, "error": "Customer is not linked to a Xero contact."}

    try:
        connection_row = get_xero_connection_for_user(user["id"])
        await create_history_record(connection_row, "Contacts", customer["xero_contact_id"], _format_xero_note(user, body))
    except Exception as exc:
        error = _sync_error_message(exc)
        record_audit_event(
            "customer",
            customer_id,
            "xero.client_note.failed",
            {"error": error, "customer": customer.get("name"), "detail": _sync_error_payload(exc)},
            user["id"],
        )
        return {"synced": False, "error": error}

    record_audit_event(
        "customer",
        customer_id,
        "xero.client_note.synced",
        {"customer": customer.get("name"), "xero_contact_id": customer.get("xero_contact_id")},
        user["id"],
    )
    return {"synced": True, "error": ""}


async def sync_invoice_status_to_xero(invoice_id: str, user: dict, status_value: str, note: str = "") -> dict:
    detail = f'Credit Control Console status updated to "{status_value}".'
    if note:
        detail = f"{detail} Note: {note.strip()}"
    return await sync_invoice_workflow_note_to_xero(invoice_id, user, detail, "xero.status_note")


async def sync_invoice_promise_to_xero(invoice_id: str, user: dict, promised_amount: str, promised_date: str, note: str = "") -> dict:
    detail = f"Payment promise recorded for £{promised_amount} due on {promised_date}."
    if note:
        detail = f"{detail} Note: {note.strip()}"
    return await sync_invoice_workflow_note_to_xero(invoice_id, user, detail, "xero.promise_note")


async def sync_payment_plan_to_xero(customer_id: str, user: dict, payment_plan: dict) -> dict:
    invoice_ids = [str(invoice_id) for invoice_id in payment_plan.get("invoiceIds") or [] if str(invoice_id).strip()]
    if not invoice_ids:
        return {"synced": False, "contactNoteSynced": False, "invoiceNoteSynced": False, "error": "Payment plan has no invoices."}

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, xero_contact_id
                FROM customers
                WHERE id = %s
                """,
                (customer_id,),
            )
            customer = cursor.fetchone()
            cursor.execute(
                """
                SELECT id, xero_invoice_id, invoice_number
                FROM invoices
                WHERE customer_id = %s
                  AND id = ANY(%s)
                ORDER BY due_date ASC NULLS LAST, invoice_number ASC
                """,
                (customer_id, [UUID(invoice_id) for invoice_id in invoice_ids]),
            )
            invoices = cursor.fetchall()
        connection.commit()

    if customer is None:
        return {"synced": False, "contactNoteSynced": False, "invoiceNoteSynced": False, "error": "Customer not found."}

    invoice_refs = ", ".join(invoice.get("invoice_number") or str(invoice["id"]) for invoice in invoices) or "selected invoices"
    note_body = _with_jenius_signature(
        f"Payment plan created for {payment_plan.get('durationMonths')} months covering {len(invoices)} invoice(s): "
        f"{invoice_refs}. Plan total: £{_float(payment_plan.get('totalAmount')):,.2f}. "
        f"Monthly payment: £{_float(payment_plan.get('monthlyAmount')):,.2f}. "
        f"Expected completion: {payment_plan.get('promisedDate') or 'not recorded'}."
    )
    errors = []
    contact_note_synced = False
    invoice_note_results = []

    try:
        connection_row = get_xero_connection_for_user(user["id"])
    except Exception as exc:
        error = _sync_error_message(exc)
        record_audit_event("customer", customer_id, "xero.payment_plan_note.failed", {"error": error, "detail": _sync_error_payload(exc)}, user["id"])
        return {"synced": False, "contactNoteSynced": False, "invoiceNoteSynced": False, "error": error}

    if customer.get("xero_contact_id"):
        try:
            await create_history_record(connection_row, "Contacts", customer["xero_contact_id"], note_body)
            contact_note_synced = True
        except Exception as exc:
            error = _sync_error_message(exc)
            errors.append(error)
            logger.exception("Unable to add payment plan history note to Xero contact %s", customer.get("xero_contact_id"))
    else:
        errors.append("Customer is not linked to a Xero contact.")

    for invoice in invoices:
        if not invoice.get("xero_invoice_id"):
            invoice_note_results.append(False)
            errors.append(f"Invoice {invoice.get('invoice_number') or invoice['id']} is not linked to a Xero invoice.")
            continue
        try:
            await create_history_record(connection_row, "Invoices", invoice["xero_invoice_id"], note_body)
            invoice_note_results.append(True)
        except Exception as exc:
            error = _sync_error_message(exc)
            errors.append(error)
            invoice_note_results.append(False)
            logger.exception("Unable to add payment plan history note to Xero invoice %s", invoice.get("invoice_number") or invoice["id"])

    invoice_notes_synced = bool(invoice_note_results) and all(invoice_note_results)
    synced = contact_note_synced and invoice_notes_synced
    record_audit_event(
        "customer",
        customer_id,
        f"xero.payment_plan_note.{'synced' if synced else 'failed'}",
        {
            "customer": customer.get("name"),
            "xero_contact_id": customer.get("xero_contact_id"),
            "invoice_ids": invoice_ids,
            "contact_note_synced": contact_note_synced,
            "invoice_note_synced": invoice_notes_synced,
            "errors": errors,
        },
        user["id"],
    )
    return {
        "synced": synced,
        "contactNoteSynced": contact_note_synced,
        "invoiceNoteSynced": invoice_notes_synced,
        "error": "; ".join(dict.fromkeys(error for error in errors if error)),
    }


def _parse_optional_iso_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def is_seven_day_notice_status(value: str) -> bool:
    control = str(value or "").lower()
    return (
        "7 day notice" in control
        or "7-day notice" in control
        or "seven day notice" in control
        or ("notice" in control and "sent" in control)
    )


def _analytics_category(invoice: dict) -> str:
    control = f"{invoice.get('controlStatus') or invoice.get('status') or ''}".lower()
    amount_due = _float(invoice.get("amountDue"))
    due_date = _parse_optional_iso_date(invoice.get("dueDate"))
    today = utcnow().date()
    if amount_due <= 0 or "paid" in control:
        return "paid"
    if "payment plan" in control or invoice.get("promiseStatus") == "open" or invoice.get("promises"):
        return "payment_plan"
    if is_seven_day_notice_status(control):
        return "seven_day_notice"
    if "bad debt" in control or "bad-debt" in control or "bad_debt" in control:
        return "bad_debt"
    if "court" in control or "legal" in control:
        return "legal"
    if "query" in control or "queried" in control or "dispute" in control or "disputed" in control:
        return "query"
    if due_date and due_date < today:
        return "overdue"
    return "outstanding"


def _month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def _month_label(value: date) -> str:
    return value.strftime("%b %Y")


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _previous_month(value: date) -> date:
    if value.month == 1:
        return date(value.year - 1, 12, 1)
    return date(value.year, value.month - 1, 1)


def _last_months(count: int = 12) -> list[date]:
    current = _month_start(utcnow().date())
    months = [current]
    for _ in range(count - 1):
        current = _previous_month(current)
        months.append(current)
    return list(reversed(months))


def _build_insights_analytics(user: dict) -> dict:
    panel = panel_payload(user)
    today = utcnow().date()
    invoices = []
    for customer in panel["customers"]:
        for invoice in customer.get("invoices", []):
            category = _analytics_category(invoice)
            amount_due = _float(invoice.get("amountDue"))
            total = _float(invoice.get("total")) or amount_due
            amount_paid = _float(invoice.get("amountPaid")) or max(total - amount_due, 0)
            due_date = _parse_optional_iso_date(invoice.get("dueDate"))
            invoice_date = _parse_optional_iso_date(invoice.get("invoiceDate")) or due_date
            overdue_days = max((today - due_date).days, 0) if due_date else 0
            invoices.append(
                {
                    "id": str(invoice.get("id") or ""),
                    "customerId": str(customer.get("id") or ""),
                    "customerName": customer.get("name") or "Client",
                    "invoiceNumber": invoice.get("invoiceNumber") or "",
                    "category": category,
                    "total": total,
                    "amountDue": amount_due,
                    "amountPaid": amount_paid,
                    "dueDate": due_date.isoformat() if due_date else "",
                    "invoiceDate": invoice_date.isoformat() if invoice_date else "",
                    "overdueDays": overdue_days,
                }
            )

    open_invoices = [invoice for invoice in invoices if invoice["category"] != "paid"]
    overdue_invoices = [invoice for invoice in open_invoices if invoice["overdueDays"] > 0]
    status_order = ["outstanding", "overdue", "seven_day_notice", "payment_plan", "query", "legal", "bad_debt", "paid"]
    status_counts = []
    for category in status_order:
        matching = [invoice for invoice in invoices if invoice["category"] == category]
        status_counts.append(
            {
                "category": category,
                "label": category.replace("_", " ").title(),
                "count": len(matching),
                "value": round(sum(invoice["amountDue"] if category != "paid" else invoice["total"] for invoice in matching), 2),
            }
        )

    customer_rollups = []
    for customer in panel["customers"]:
        customer_invoices = [invoice for invoice in invoices if invoice["customerId"] == str(customer.get("id"))]
        customer_open = [invoice for invoice in customer_invoices if invoice["category"] != "paid"]
        customer_overdue = [invoice for invoice in customer_open if invoice["overdueDays"] > 0]
        if not customer_invoices:
            continue
        customer_rollups.append(
            {
                "id": str(customer.get("id") or ""),
                "name": customer.get("name") or "Client",
                "totalDue": round(sum(invoice["amountDue"] for invoice in customer_open), 2),
                "overdue": round(sum(invoice["amountDue"] for invoice in customer_overdue), 2),
                "openInvoices": len(customer_open),
                "maxDaysOverdue": max((invoice["overdueDays"] for invoice in customer_overdue), default=0),
                "statusMix": {
                    category: sum(1 for invoice in customer_invoices if invoice["category"] == category)
                    for category in status_order
                },
            }
        )
    top_customers = sorted(customer_rollups, key=lambda item: (item["totalDue"], item["maxDaysOverdue"]), reverse=True)[:10]

    month_rows = {month: {"month": _month_key(month), "label": _month_label(month), "invoiced": 0.0, "paid": 0.0, "outstanding": 0.0} for month in _last_months(12)}
    month_lookup = {_month_key(month): month for month in month_rows}
    for invoice in invoices:
        invoice_date = _parse_optional_iso_date(invoice["invoiceDate"])
        if not invoice_date:
            continue
        key = _month_key(_month_start(invoice_date))
        month = month_lookup.get(key)
        if not month:
            continue
        month_rows[month]["invoiced"] += invoice["total"]
        month_rows[month]["paid"] += invoice["amountPaid"]
        month_rows[month]["outstanding"] += invoice["amountDue"]
    monthly = [
        {
            **row,
            "invoiced": round(row["invoiced"], 2),
            "paid": round(row["paid"], 2),
            "outstanding": round(row["outstanding"], 2),
        }
        for _, row in sorted(month_rows.items())
    ]

    ageing_buckets = [
        ("Current", lambda days: days <= 0),
        ("1-30", lambda days: 1 <= days <= 30),
        ("31-60", lambda days: 31 <= days <= 60),
        ("61-90", lambda days: 61 <= days <= 90),
        ("90+", lambda days: days > 90),
    ]
    ageing = []
    for label, predicate in ageing_buckets:
        matching = [invoice for invoice in open_invoices if predicate(invoice["overdueDays"])]
        ageing.append({"label": label, "count": len(matching), "value": round(sum(invoice["amountDue"] for invoice in matching), 2)})

    totals = {
        "invoiceCount": len(invoices),
        "openInvoiceCount": len(open_invoices),
        "customerCount": len(panel["customers"]),
        "totalInvoiced": round(sum(invoice["total"] for invoice in invoices), 2),
        "totalOutstanding": round(sum(invoice["amountDue"] for invoice in open_invoices), 2),
        "totalPaid": round(sum(invoice["amountPaid"] for invoice in invoices), 2),
        "totalOverdue": round(sum(invoice["amountDue"] for invoice in overdue_invoices), 2),
        "paymentPlanValue": round(sum(invoice["amountDue"] for invoice in invoices if invoice["category"] == "payment_plan"), 2),
        "legalValue": round(sum(invoice["amountDue"] for invoice in invoices if invoice["category"] == "legal"), 2),
        "sevenDayNoticeValue": round(sum(invoice["amountDue"] for invoice in invoices if invoice["category"] == "seven_day_notice"), 2),
        "maxDaysOverdue": max((invoice["overdueDays"] for invoice in overdue_invoices), default=0),
    }

    return {
        "generatedAt": utcnow().isoformat(),
        "organisation": panel.get("organisation", {}),
        "totals": totals,
        "topCustomers": top_customers,
        "monthly": monthly,
        "ageing": ageing,
        "statusCounts": status_counts,
        "riskInvoices": sorted(overdue_invoices, key=lambda item: (item["amountDue"], item["overdueDays"]), reverse=True)[:12],
    }


OPENAI_INSIGHTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "priorityActions", "risks", "opportunities", "narrative"],
    "properties": {
        "summary": {"type": "string"},
        "priorityActions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "reason", "impact", "urgency"],
                "properties": {
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "impact": {"type": "string"},
                    "urgency": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "detail", "value"],
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "detail", "value"],
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
        "narrative": {"type": "string"},
    },
}


def _fallback_ai_insights(analytics: dict, status_value: str = "local") -> dict:
    totals = analytics["totals"]
    top_customer = analytics["topCustomers"][0] if analytics["topCustomers"] else None
    actions = []
    if top_customer and top_customer["totalDue"] > 0:
        actions.append(
            {
                "title": f"Prioritise {top_customer['name']}",
                "reason": f"They owe £{top_customer['totalDue']:,.2f} across {top_customer['openInvoices']} open invoice(s).",
                "impact": "Largest current balance concentration.",
                "urgency": "high" if top_customer["maxDaysOverdue"] >= 90 else "medium",
            }
        )
    if totals["sevenDayNoticeValue"] > 0:
        actions.append(
            {
                "title": "Review active 7 day notices",
                "reason": f"£{totals['sevenDayNoticeValue']:,.2f} is currently in the notice countdown workflow.",
                "impact": "Keeps escalation decisions visible before deadlines expire.",
                "urgency": "high",
            }
        )
    if totals["paymentPlanValue"] > 0:
        actions.append(
            {
                "title": "Monitor payment plan performance",
                "reason": f"£{totals['paymentPlanValue']:,.2f} is committed to payment plans.",
                "impact": "Missed plan payments can be chased without manually checking every client.",
                "urgency": "medium",
            }
        )
    return {
        "status": status_value,
        "summary": f"{totals['openInvoiceCount']} open invoices total £{totals['totalOutstanding']:,.2f}, with £{totals['totalOverdue']:,.2f} overdue.",
        "priorityActions": actions[:5],
        "risks": [
            {
                "title": "Maximum overdue age",
                "detail": f"The oldest overdue balance is {totals['maxDaysOverdue']} days overdue.",
                "value": f"{totals['maxDaysOverdue']} days",
            },
            {
                "title": "Legal exposure",
                "detail": "Invoices already marked Legal should be checked before additional reminders are sent.",
                "value": f"£{totals['legalValue']:,.2f}",
            },
        ],
        "opportunities": [
            {
                "title": "Payment plan coverage",
                "detail": "Accounts with active plans can be separated from normal overdue chasing.",
                "value": f"£{totals['paymentPlanValue']:,.2f}",
            }
        ],
        "narrative": "These insights are calculated locally from the synced Xero data.",
    }


def _extract_response_text(payload: dict) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    parts = []
    for item in payload.get("output", []) or []:
        for content in item.get("content", []) or []:
            text = content.get("text") if isinstance(content, dict) else None
            if text:
                parts.append(str(text))
    return "\n".join(parts).strip()


async def _generate_openai_insights(analytics: dict) -> dict:
    settings = get_settings()
    if not settings.openai_api_key:
        return _fallback_ai_insights(analytics, "disabled")

    compact_analytics = {
        "totals": analytics["totals"],
        "topCustomers": analytics["topCustomers"][:8],
        "ageing": analytics["ageing"],
        "statusCounts": analytics["statusCounts"],
        "monthly": analytics["monthly"],
        "riskInvoices": analytics["riskInvoices"][:8],
    }
    request_body = {
        "model": settings.openai_model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "You are Jenius AI, an operational credit-control analyst. "
                            "Return concise JSON only. Focus on collection priority, overdue risk, payment plans, "
                            "legal escalation, and month-on-month movement. Do not invent figures not present in the input."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(compact_analytics, default=_json_default)}],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "credit_control_insights",
                "schema": OPENAI_INSIGHTS_SCHEMA,
                "strict": True,
            }
        },
        "max_output_tokens": 1800,
    }
    try:
        async with httpx.AsyncClient(timeout=OPENAI_INSIGHTS_TIMEOUT_SECONDS) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
                json=request_body,
            )
            response.raise_for_status()
        text = _extract_response_text(response.json())
        parsed = json.loads(text) if text else {}
        return {"status": "ready", **parsed}
    except Exception as exc:
        logger.exception("OpenAI insights generation failed")
        fallback = _fallback_ai_insights(analytics, "error")
        fallback["error"] = str(exc) or exc.__class__.__name__
        return fallback


async def insights_payload(user: dict) -> dict:
    analytics = _build_insights_analytics(user)
    ai = await _generate_openai_insights(analytics)
    return {"status": "ok", "analytics": analytics, "ai": ai}


def add_promise(invoice_id: str, user: dict, promised_amount: str, promised_date: str, note: str) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO payment_promises (invoice_id, promised_amount, promised_date, note, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (invoice_id, promised_amount, promised_date, note, user["id"]),
            )
            cursor.execute(
                """
                UPDATE invoices
                SET promised_date = %s,
                    promise_status = %s,
                    control_status = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (promised_date, "open", "Payment Plan", utcnow(), invoice_id),
            )
            cursor.execute(
                """
                INSERT INTO invoice_status_history (invoice_id, status, note, changed_by_user_id)
                VALUES (%s, %s, %s, %s)
                """,
                (invoice_id, "Payment Plan", note or "Payment promise recorded.", user["id"]),
            )
        connection.commit()
    record_audit_event(
        "invoice",
        invoice_id,
        "promise.created",
        {"promised_amount": promised_amount, "promised_date": promised_date, "note": note},
        user["id"],
    )


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + max(months, 0)
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _ordinal_day(value: date) -> str:
    if 10 <= value.day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value.day % 10, "th")
    return f"{value.day}{suffix}"


def _invoice_date_description(value: date | None) -> str:
    if value is None:
        return "unknown date"
    return f"{_ordinal_day(value)} {value.strftime('%B')} {value.year}"


def _normalise_late_payment_charge_base_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Late payment charge must be £20, £30 or £50 plus VAT.") from exc
    if amount not in LATE_PAYMENT_CHARGE_BASE_AMOUNTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Late payment charge must be £20, £30 or £50 plus VAT.")
    return amount


def _stored_late_payment_charge_base_amount(value) -> Decimal | None:
    if value is None:
        return None
    return _normalise_late_payment_charge_base_amount(value)


def _resolve_late_payment_charge_base_amount(invoice: dict, requested_base_amount: Decimal) -> tuple[Decimal | None, str]:
    stored_base_amount = _stored_late_payment_charge_base_amount(invoice.get("late_payment_charge_base_amount"))
    if stored_base_amount is None:
        return requested_base_amount, ""
    if requested_base_amount != stored_base_amount:
        return (
            None,
            f"Customer late payment charge is fixed at £{stored_base_amount:,.2f} + VAT. Refresh the ledger and try again.",
        )
    return stored_base_amount, ""


def _late_payment_charge_selection_lookup(invoice_refs: list[str], charge_selections=None) -> dict[str, Decimal]:
    selected = {invoice_ref: DEFAULT_LATE_PAYMENT_CHARGE_BASE_AMOUNT for invoice_ref in invoice_refs}
    if not charge_selections:
        return selected

    if isinstance(charge_selections, dict):
        selection_rows = [
            {"invoiceId": invoice_id, "baseAmount": amount}
            for invoice_id, amount in charge_selections.items()
        ]
    elif isinstance(charge_selections, list):
        selection_rows = charge_selections
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Late payment charge selections must be a list or object.")

    for row in selection_rows:
        if not isinstance(row, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid late payment charge selection.")
        raw_invoice_id = str(row.get("invoiceId") or row.get("id") or "").strip()
        if not raw_invoice_id:
            continue
        try:
            invoice_id = str(UUID(raw_invoice_id))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invoice id in late payment charge selection.") from exc
        if invoice_id not in selected:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Late payment charge selection did not match the selected invoices.")
        selected[invoice_id] = _normalise_late_payment_charge_base_amount(
            row.get("baseAmount", row.get("netAmount", row.get("amount", DEFAULT_LATE_PAYMENT_CHARGE_BASE_AMOUNT)))
        )
    return selected


def _late_payment_charge_gross_amount(base_amount: Decimal) -> Decimal:
    return (base_amount * (Decimal("1.00") + LATE_PAYMENT_CHARGE_VAT_RATE)).quantize(Decimal("0.01"))


def _late_payment_charge_error_message(exc: Exception) -> str:
    error = _sync_error_message(exc)
    if "quota has been exceeded" in error.lower():
        return (
            "Xero quota has been exceeded, so the charge invoice was not created. "
            "The local invoice was left uncharged; wait for Xero's quota to reset or create the charge manually in Xero."
        )
    return error


def _late_payment_charge_description(invoice: dict, overdue_days: int, base_amount: Decimal, gross_amount: Decimal) -> str:
    invoice_number = invoice.get("invoice_number") or str(invoice["id"])
    return (
        f"Contractual late payment charge for Invoice {invoice_number}, which remained outstanding "
        f"for {overdue_days} days beyond the due date. Charge applied in accordance with our "
        f"Terms of Engagement. £{base_amount:,.2f} plus VAT."
    )


async def create_late_payment_charges(user: dict, invoice_ids: list[str], charge_selections=None) -> dict:
    try:
        unique_invoice_refs = list(dict.fromkeys(str(UUID(str(invoice_id).strip())) for invoice_id in invoice_ids if str(invoice_id).strip()))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invoice id in late payment charge selection.") from exc
    if not unique_invoice_refs:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one invoice.")
    charge_selection_lookup = _late_payment_charge_selection_lookup(unique_invoice_refs, charge_selections)

    today = utcnow().date()
    settings = get_settings()
    local_invoice_ids = [UUID(invoice_ref) for invoice_ref in unique_invoice_refs]
    xero_invoice_ids = [invoice_ref.lower() for invoice_ref in unique_invoice_refs]
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT invoices.id,
                       invoices.xero_invoice_id,
                       invoices.invoice_number,
                       invoices.invoice_date,
                       invoices.due_date,
                       invoices.amount_due,
                       invoices.currency_code,
                       invoices.late_payment_charge_raised_at,
                       customers.id AS customer_id,
                       customers.name AS customer_name,
                       customers.xero_contact_id,
                       customers.late_payment_charge_base_amount
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE invoices.id = ANY(%s)
                   OR lower(invoices.xero_invoice_id) = ANY(%s)
                ORDER BY invoices.due_date ASC NULLS LAST, invoices.invoice_number ASC
                """,
                (local_invoice_ids, xero_invoice_ids),
            )
            invoices = cursor.fetchall()
        connection.commit()

    invoices_by_ref = {}
    for invoice in invoices:
        local_ref = str(invoice["id"])
        xero_ref = str(invoice.get("xero_invoice_id") or "").lower()
        if local_ref in charge_selection_lookup:
            invoices_by_ref[local_ref] = invoice
        if xero_ref in charge_selection_lookup and xero_ref not in invoices_by_ref:
            invoices_by_ref[xero_ref] = invoice

    selected_invoices = []
    charge_selection_by_invoice_id = {}
    skipped = []
    seen_invoice_ids = set()
    for invoice_ref in unique_invoice_refs:
        invoice = invoices_by_ref.get(invoice_ref)
        if invoice is None:
            skipped.append({"invoiceId": invoice_ref, "reason": "Invoice could not be found. Refresh the ledger and try again."})
            continue
        invoice_id = str(invoice["id"])
        if invoice_id in seen_invoice_ids:
            continue
        seen_invoice_ids.add(invoice_id)
        selected_invoices.append(invoice)
        charge_selection_by_invoice_id[invoice_id] = charge_selection_lookup[invoice_ref]

    if not selected_invoices:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected invoices could not be found. Refresh the ledger and try again.")

    chargeable = []
    for invoice in selected_invoices:
        due_date = invoice.get("due_date")
        overdue_days = 0 if due_date is None else max((today - due_date).days, 0)
        amount_due = _float(invoice.get("amount_due"))
        if amount_due <= 0:
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Invoice is not outstanding."})
            continue
        if overdue_days <= 14:
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Invoice is not more than 14 days overdue."})
            continue
        if invoice.get("late_payment_charge_raised_at"):
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Late payment charge already raised."})
            continue
        if not invoice.get("xero_contact_id"):
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Customer is not linked to a Xero contact."})
            continue
        requested_charge_base_amount = charge_selection_by_invoice_id[str(invoice["id"])]
        charge_base_amount, fixed_amount_error = _resolve_late_payment_charge_base_amount(invoice, requested_charge_base_amount)
        if fixed_amount_error:
            skipped.append({"invoiceId": str(invoice["id"]), "reason": fixed_amount_error})
            continue
        charge_amount = _late_payment_charge_gross_amount(charge_base_amount)
        if charge_amount <= 0:
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Calculated late payment charge is zero."})
            continue
        chargeable.append((invoice, overdue_days, charge_base_amount, charge_amount))

    if not chargeable:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No selected invoices are eligible for late payment charges.")

    connection_row = get_xero_connection_for_user(user["id"])
    created = []
    for invoice, overdue_days, charge_base_amount, charge_amount in chargeable:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT invoices.id,
                           invoices.xero_invoice_id,
                           invoices.invoice_number,
                           invoices.invoice_date,
                           invoices.due_date,
                           invoices.amount_due,
                           invoices.currency_code,
                           invoices.late_payment_charge_raised_at,
                           customers.id AS customer_id,
                           customers.name AS customer_name,
                           customers.xero_contact_id,
                           customers.late_payment_charge_base_amount
                    FROM invoices
                    JOIN customers ON customers.id = invoices.customer_id
                    WHERE invoices.id = %s
                    FOR UPDATE
                    """,
                    (invoice["id"],),
                )
                locked_invoice = cursor.fetchone()
                if locked_invoice is None:
                    skipped.append({"invoiceId": str(invoice["id"]), "reason": "Invoice could not be found."})
                    connection.commit()
                    continue

                due_date = locked_invoice.get("due_date")
                overdue_days = 0 if due_date is None else max((today - due_date).days, 0)
                amount_due = _float(locked_invoice.get("amount_due"))
                if amount_due <= 0:
                    skipped.append({"invoiceId": str(locked_invoice["id"]), "reason": "Invoice is not outstanding."})
                    connection.commit()
                    continue
                if overdue_days <= 14:
                    skipped.append({"invoiceId": str(locked_invoice["id"]), "reason": "Invoice is not more than 14 days overdue."})
                    connection.commit()
                    continue
                if locked_invoice.get("late_payment_charge_raised_at"):
                    skipped.append({"invoiceId": str(locked_invoice["id"]), "reason": "Late payment charge already raised."})
                    connection.commit()
                    continue
                if not locked_invoice.get("xero_contact_id"):
                    skipped.append({"invoiceId": str(locked_invoice["id"]), "reason": "Customer is not linked to a Xero contact."})
                    connection.commit()
                    continue

                requested_charge_base_amount = charge_selection_by_invoice_id[str(locked_invoice["id"])]
                charge_base_amount, fixed_amount_error = _resolve_late_payment_charge_base_amount(locked_invoice, requested_charge_base_amount)
                if fixed_amount_error:
                    skipped.append({"invoiceId": str(locked_invoice["id"]), "reason": fixed_amount_error})
                    connection.commit()
                    continue
                charge_amount = _late_payment_charge_gross_amount(charge_base_amount)
                if charge_amount <= 0:
                    skipped.append({"invoiceId": str(locked_invoice["id"]), "reason": "Calculated late payment charge is zero."})
                    connection.commit()
                    continue

                invoice = locked_invoice
                currency_code = invoice.get("currency_code") or "GBP"
                description = _late_payment_charge_description(invoice, overdue_days, charge_base_amount, charge_amount)
                tax_type = (settings.late_payment_charge_tax_type or "").strip()
                line_item = {
                    "Description": description,
                    "Quantity": 1,
                    "UnitAmount": float(charge_base_amount),
                    "AccountCode": settings.late_payment_charge_account_code,
                }
                if tax_type:
                    line_item["TaxType"] = tax_type
                invoice_payload = {
                    "Type": "ACCREC",
                    "Contact": {"ContactID": invoice["xero_contact_id"]},
                    "Date": today.isoformat(),
                    "DueDate": today.isoformat(),
                    "Reference": f"Late charge for {invoice.get('invoice_number') or invoice['id']}",
                    "LineAmountTypes": "Exclusive",
                    "Status": "AUTHORISED",
                    "LineItems": [line_item],
                }
                if currency_code:
                    invoice_payload["CurrencyCode"] = currency_code

                try:
                    xero_response = await create_sales_invoice(
                        connection_row,
                        invoice_payload,
                        idempotency_key=f"late-payment-charge-{invoice['id']}",
                    )
                    created_invoice = ((xero_response or {}).get("Invoices") or [{}])[0]
                    created_invoice_id = created_invoice.get("InvoiceID") or created_invoice.get("ID") or ""
                    created_invoice_number = created_invoice.get("InvoiceNumber") or ""
                    if not created_invoice_id:
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail="Xero created the late payment charge but did not return an invoice id.",
                        )
                except Exception as exc:
                    error = _late_payment_charge_error_message(exc)
                    skipped.append({"invoiceId": str(invoice["id"]), "reason": error})
                    connection.commit()
                    record_audit_event(
                        "invoice",
                        str(invoice["id"]),
                        "late_payment_charge.failed",
                        {
                            "invoice_number": invoice.get("invoice_number"),
                            "customer_id": str(invoice.get("customer_id")),
                            "customer_name": invoice.get("customer_name"),
                            "overdue_days": overdue_days,
                            "amount": float(charge_amount),
                            "base_amount": float(charge_base_amount),
                            "vat_rate": float(LATE_PAYMENT_CHARGE_VAT_RATE),
                            "currency_code": currency_code,
                            "account_code": settings.late_payment_charge_account_code,
                            "tax_type": tax_type,
                            "error": error,
                            "detail": _sync_error_payload(exc),
                        },
                        user["id"],
                    )
                    continue

                now = utcnow()
                history_note = _with_jenius_signature(
                    f"Late payment charge raised on {_invoice_date_description(today)} for invoice "
                    f"{invoice.get('invoice_number') or invoice['id']}, originally due "
                    f"{_invoice_date_description(invoice.get('due_date'))}. Charge amount: "
                    f"£{charge_base_amount:,.2f} + VAT (£{charge_amount:,.2f} total). "
                    f"Xero charge invoice: {created_invoice_number or created_invoice_id or 'created'}."
                )
                cursor.execute(
                    """
                    UPDATE invoices
                    SET late_payment_charge_raised_at = %s,
                        late_payment_charge_invoice_id = %s,
                        late_payment_charge_invoice_number = %s,
                        late_payment_charge_amount = %s,
                        notes_summary = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (now, created_invoice_id, created_invoice_number, charge_amount, history_note[:200], now, invoice["id"]),
                )
                cursor.execute(
                    """
                    INSERT INTO invoice_status_history (invoice_id, status, note, changed_by_user_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        invoice["id"],
                        "Late Payment Charge Raised",
                        history_note,
                        user["id"],
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO notes (invoice_id, user_id, body)
                    VALUES (%s, %s, %s)
                    """,
                    (invoice["id"], user["id"], history_note),
                )
                cursor.execute(
                    """
                    INSERT INTO customer_notes (customer_id, user_id, body)
                    VALUES (%s, %s, %s)
                    """,
                    (invoice["customer_id"], user["id"], history_note),
                )
                cursor.execute(
                    """
                    UPDATE customers
                    SET late_payment_charge_base_amount = %s,
                        updated_at = %s
                    WHERE id = %s
                      AND late_payment_charge_base_amount IS NULL
                    """,
                    (charge_base_amount, now, invoice["customer_id"]),
                )
            connection.commit()

        history_note_synced = False
        history_note_error = ""
        if invoice.get("xero_invoice_id"):
            try:
                await create_history_record(connection_row, "Invoices", invoice["xero_invoice_id"], history_note)
                history_note_synced = True
            except Exception as exc:
                history_note_error = _sync_error_message(exc)
                logger.exception("Unable to add late payment charge history note to Xero invoice %s", invoice.get("invoice_number") or invoice["id"])

        contact_note_synced = False
        contact_note_error = ""
        if invoice.get("xero_contact_id"):
            try:
                await create_history_record(connection_row, "Contacts", invoice["xero_contact_id"], history_note)
                contact_note_synced = True
            except Exception as exc:
                contact_note_error = _sync_error_message(exc)
                logger.exception("Unable to add late payment charge history note to Xero contact %s", invoice.get("customer_name") or invoice["customer_id"])

        record_audit_event(
            "invoice",
            str(invoice["id"]),
            "late_payment_charge.raised",
            {
                "invoice_number": invoice.get("invoice_number"),
                "customer_id": str(invoice.get("customer_id")),
                "customer_name": invoice.get("customer_name"),
                "overdue_days": overdue_days,
                "amount": float(charge_amount),
                "base_amount": float(charge_base_amount),
                "vat_rate": float(LATE_PAYMENT_CHARGE_VAT_RATE),
                "currency_code": currency_code,
                "account_code": settings.late_payment_charge_account_code,
                "tax_type": tax_type,
                "created_invoice_id": created_invoice_id,
                "created_invoice_number": created_invoice_number,
                "description": description,
                "history_note_synced": history_note_synced,
                "history_note_error": history_note_error,
                "contact_note_synced": contact_note_synced,
                "contact_note_error": contact_note_error,
            },
            user["id"],
        )
        created.append(
            {
                "invoiceId": str(invoice["id"]),
                "chargeAmount": float(charge_amount),
                "baseAmount": float(charge_base_amount),
                "vatRate": float(LATE_PAYMENT_CHARGE_VAT_RATE),
                "currencyCode": currency_code,
                "accountCode": settings.late_payment_charge_account_code,
                "taxType": tax_type,
                "createdInvoiceId": created_invoice_id,
                "createdInvoiceNumber": created_invoice_number,
                "description": description,
                "historyNoteSynced": history_note_synced,
                "historyNoteError": history_note_error,
                "contactNoteSynced": contact_note_synced,
                "contactNoteError": contact_note_error,
            }
        )

    return {"created": created, "skipped": skipped}


async def create_bad_debt_write_offs(user: dict, invoice_ids: list[str]) -> dict:
    try:
        unique_invoice_ids = list(dict.fromkeys(UUID(str(invoice_id).strip()) for invoice_id in invoice_ids if str(invoice_id).strip()))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invoice id in write-off selection.") from exc
    if not unique_invoice_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one invoice.")

    today = utcnow().date()
    settings = get_settings()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT invoices.id,
                       invoices.xero_invoice_id,
                       invoices.invoice_number,
                       invoices.invoice_date,
                       invoices.due_date,
                       invoices.amount_due,
                       invoices.total,
                       invoices.currency_code,
                       invoices.bad_debt_write_off_at,
                       invoices.bad_debt_credit_note_id,
                       customers.id AS customer_id,
                       customers.name AS customer_name,
                       customers.xero_contact_id
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE invoices.id = ANY(%s)
                ORDER BY invoices.due_date ASC NULLS LAST, invoices.invoice_number ASC
                """,
                (unique_invoice_ids,),
            )
            invoices = cursor.fetchall()
        connection.commit()

    if len(invoices) != len(unique_invoice_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="One or more selected invoices could not be found.")

    writable = []
    skipped = []
    for invoice in invoices:
        amount_due = Decimal(str(_float(invoice.get("amount_due")))).quantize(Decimal("0.01"))
        if amount_due <= 0:
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Invoice is not outstanding."})
            continue
        if invoice.get("bad_debt_write_off_at") or invoice.get("bad_debt_credit_note_id"):
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Invoice has already been written off."})
            continue
        if not invoice.get("xero_invoice_id"):
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Invoice is not linked to a Xero invoice."})
            continue
        if not invoice.get("xero_contact_id"):
            skipped.append({"invoiceId": str(invoice["id"]), "reason": "Customer is not linked to a Xero contact."})
            continue
        writable.append((invoice, amount_due))

    if not writable:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No selected invoices are eligible for write-off.")

    connection_row = get_xero_connection_for_user(user["id"])
    account_code = settings.bad_debt_write_off_account_code
    created = []
    for invoice, amount_due in writable:
        now = utcnow()
        invoice_number = invoice.get("invoice_number") or str(invoice["id"])
        currency_code = invoice.get("currency_code") or "GBP"
        amount_label = f"{currency_code} {amount_due:,.2f}"
        description = (
            f"Bad debt write off for invoice {invoice_number} dated {_invoice_date_description(invoice.get('invoice_date'))}. "
            f"The outstanding balance of {amount_label} has been assessed as irrecoverable. "
            f"Raised via jeNIUS AI Credit Control Console to account code {account_code} "
            "Irrecoverable Receivables / Bad Debt Write Off and allocated directly against the original invoice."
        )
        credit_note_payload = {
            "Type": "ACCRECCREDIT",
            "Contact": {"ContactID": invoice["xero_contact_id"]},
            "Date": today.isoformat(),
            "LineAmountTypes": "NoTax",
            "Status": "AUTHORISED",
            "Reference": f"Bad debt write off {invoice_number}",
            "LineItems": [
                {
                    "Description": description,
                    "Quantity": 1,
                    "UnitAmount": float(amount_due),
                    "AccountCode": account_code,
                    "TaxType": "NONE",
                }
            ],
        }
        if currency_code:
            credit_note_payload["CurrencyCode"] = currency_code

        xero_response = await create_credit_note(connection_row, credit_note_payload)
        created_credit_note = ((xero_response or {}).get("CreditNotes") or [{}])[0]
        credit_note_id = created_credit_note.get("CreditNoteID") or created_credit_note.get("ID") or ""
        credit_note_number = created_credit_note.get("CreditNoteNumber") or ""
        if not credit_note_id:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Xero created the credit note but did not return a credit note id.")

        allocation_payload = {
            "Invoice": {"InvoiceID": invoice["xero_invoice_id"]},
            "Amount": float(amount_due),
            "Date": today.isoformat(),
        }
        allocation_response = await allocate_credit_note(connection_row, credit_note_id, allocation_payload)

        contact_note = _with_jenius_signature(
            f"Bad debt write-off completed for invoice {invoice_number}. "
            f"Credit note {credit_note_number or credit_note_id} was raised for {amount_label} "
            "and allocated against the invoice. "
            f"Account code: {account_code} Irrecoverable Receivables / Bad Debt Write Off. "
            "Reason: outstanding balance assessed as irrecoverable bad debt."
        )
        contact_note_synced = True
        contact_note_error = ""
        try:
            await create_history_record(connection_row, "Contacts", invoice["xero_contact_id"], contact_note)
        except Exception as exc:
            contact_note_synced = False
            contact_note_error = _sync_error_message(exc)

        status_note = (
            f"Bad debt write-off completed for invoice {invoice_number}. "
            f"Credit note {credit_note_number or credit_note_id} was raised for {amount_label} "
            f"and allocated against the invoice. Account code: {account_code} "
            "Irrecoverable Receivables / Bad Debt Write Off."
        )
        if not contact_note_synced:
            status_note = f"{status_note} Xero contact note was not added: {contact_note_error}"
        status_note = _with_jenius_signature(status_note)

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE invoices
                    SET amount_due = 0,
                        control_status = %s,
                        notes_summary = %s,
                        bad_debt_write_off_at = %s,
                        bad_debt_credit_note_id = %s,
                        bad_debt_credit_note_number = %s,
                        bad_debt_credit_note_amount = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        "Bad debt",
                        contact_note[:200],
                        now,
                        credit_note_id,
                        credit_note_number,
                        amount_due,
                        now,
                        invoice["id"],
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO invoice_status_history (invoice_id, status, note, changed_by_user_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (invoice["id"], "Bad debt", status_note, user["id"]),
                )
                cursor.execute(
                    """
                    INSERT INTO notes (invoice_id, user_id, body)
                    VALUES (%s, %s, %s)
                    """,
                    (invoice["id"], user["id"], contact_note),
                )
                cursor.execute(
                    """
                    INSERT INTO customer_notes (customer_id, user_id, body)
                    VALUES (%s, %s, %s)
                    """,
                    (invoice["customer_id"], user["id"], contact_note),
                )
                _refresh_customer_totals(cursor, connection_row["tenant_id"], now)
            connection.commit()

        record_audit_event(
            "invoice",
            str(invoice["id"]),
            "bad_debt.write_off",
            {
                "invoice_number": invoice_number,
                "customer_id": str(invoice.get("customer_id")),
                "customer_name": invoice.get("customer_name"),
                "amount": float(amount_due),
                "currency_code": currency_code,
                "account_code": account_code,
                "credit_note_id": credit_note_id,
                "credit_note_number": credit_note_number,
                "allocation": allocation_response,
                "contact_note_synced": contact_note_synced,
                "contact_note_error": contact_note_error,
                "description": description,
            },
            user["id"],
        )
        created.append(
            {
                "invoiceId": str(invoice["id"]),
                "writeOffAmount": float(amount_due),
                "creditNoteId": credit_note_id,
                "creditNoteNumber": credit_note_number,
                "contactNoteSynced": contact_note_synced,
                "contactNoteError": contact_note_error,
                "description": description,
            }
        )

    return {"created": created, "skipped": skipped}


def create_payment_plan(customer_id: str, user: dict, invoice_ids: list[str], duration_months: int, note: str = "") -> dict:
    try:
        unique_invoice_ids = list(dict.fromkeys(UUID(str(invoice_id).strip()) for invoice_id in invoice_ids if str(invoice_id).strip()))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invoice id in payment plan.") from exc
    clean_invoice_ids = [str(invoice_id) for invoice_id in unique_invoice_ids]
    if not clean_invoice_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one invoice is required.")
    if duration_months < 1 or duration_months > 60:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment plan duration must be between 1 and 60 months.")

    now = utcnow()
    promised_date = _add_months(now.date(), duration_months)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, name FROM customers WHERE id = %s", (customer_id,))
            customer = cursor.fetchone()
            if customer is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found.")

            cursor.execute(
                """
                SELECT id, invoice_number, amount_due
                FROM invoices
                WHERE customer_id = %s
                  AND id = ANY(%s)
                  AND amount_due > 0
                ORDER BY due_date ASC NULLS LAST, invoice_number ASC
                """,
                (customer_id, unique_invoice_ids),
            )
            invoices = cursor.fetchall()
            if len(invoices) != len(unique_invoice_ids):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Payment plans can only include open invoices for the selected customer.",
                )

            total_amount = sum(_float(invoice.get("amount_due")) for invoice in invoices)
            monthly_amount = round(total_amount / duration_months, 2)
            invoice_refs = ", ".join(invoice.get("invoice_number") or str(invoice["id"]) for invoice in invoices)
            auto_note = (
                note.strip()
                or (
                    f"Payment plan created for {duration_months} months covering {len(invoices)} invoice(s): {invoice_refs}. "
                    f"Plan total: £{total_amount:,.2f}. Monthly payment: £{monthly_amount:,.2f}."
                )
            )
            per_invoice_note = (
                f"{auto_note}\n\n"
                f"Duration: {duration_months} months. Monthly payment: £{monthly_amount:,.2f}. "
                f"Plan total: £{total_amount:,.2f}. Expected completion: {promised_date.isoformat()}."
            )

            for invoice in invoices:
                cursor.execute(
                    """
                    INSERT INTO payment_promises (invoice_id, promised_amount, promised_date, note, created_by_user_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (invoice["id"], invoice["amount_due"], promised_date, per_invoice_note, user["id"]),
                )
                cursor.execute(
                    """
                    UPDATE invoices
                    SET promised_date = %s,
                        promise_status = %s,
                        control_status = %s,
                        notes_summary = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (promised_date, "open", "Payment Plan", auto_note[:200], now, invoice["id"]),
                )
                cursor.execute(
                    """
                    INSERT INTO invoice_status_history (invoice_id, status, note, changed_by_user_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (invoice["id"], "Payment Plan", per_invoice_note, user["id"]),
                )
            cursor.execute(
                "INSERT INTO customer_notes (customer_id, user_id, body) VALUES (%s, %s, %s)",
                (customer_id, user["id"], per_invoice_note),
            )
            tenant_id = None
            cursor.execute("SELECT tenant_id FROM customers WHERE id = %s", (customer_id,))
            tenant_row = cursor.fetchone()
            if tenant_row:
                tenant_id = tenant_row["tenant_id"]
            if tenant_id:
                _refresh_customer_totals(cursor, tenant_id, now)
        connection.commit()

    for invoice in invoices:
        record_audit_event(
            "invoice",
            str(invoice["id"]),
            "payment_plan.created",
            {
                "customer_id": customer_id,
                "duration_months": duration_months,
                "promised_date": promised_date.isoformat(),
                "invoice_count": len(invoices),
                "total_amount": total_amount,
                "note": auto_note,
            },
            user["id"],
        )
    record_audit_event(
        "customer",
        customer_id,
        "payment_plan.created",
        {
            "duration_months": duration_months,
            "promised_date": promised_date.isoformat(),
            "invoice_ids": clean_invoice_ids,
            "total_amount": total_amount,
            "note": auto_note,
        },
        user["id"],
    )
    return {
        "customerId": customer_id,
        "invoiceIds": clean_invoice_ids,
        "durationMonths": duration_months,
        "promisedDate": promised_date.isoformat(),
        "totalAmount": total_amount,
        "monthlyAmount": monthly_amount,
        "note": auto_note,
    }


def update_control_status(invoice_id: str, user: dict, status_value: str, note: str) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE invoices SET control_status = %s, last_chased_at = %s, updated_at = %s WHERE id = %s",
                (status_value, utcnow(), utcnow(), invoice_id),
            )
            cursor.execute(
                """
                INSERT INTO invoice_status_history (invoice_id, status, note, changed_by_user_id)
                VALUES (%s, %s, %s, %s)
                """,
                (invoice_id, status_value, note, user["id"]),
            )
        connection.commit()
    record_audit_event("invoice", invoice_id, "status.updated", {"status": status_value, "note": note}, user["id"])


async def bulk_update_invoice_status(user: dict, invoice_ids: list[str], status_value: str, note: str = "") -> dict:
    status_value = str(status_value or "").strip()
    if not status_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status is required.")
    invoice_ids = [str(invoice_id) for invoice_id in (invoice_ids or []) if invoice_id]
    if not invoice_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one invoice.")
    note = str(note or "").strip() or "Updated from ledger bulk actions."
    synced = 0
    failed = 0
    errors = []
    for invoice_id in invoice_ids:
        update_control_status(invoice_id, user, status_value, note)
        try:
            xero_note = await sync_invoice_status_to_xero(invoice_id, user, status_value, note)
            if xero_note.get("synced"):
                synced += 1
            else:
                failed += 1
                if xero_note.get("error"):
                    errors.append(xero_note["error"])
        except Exception as exc:
            failed += 1
            errors.append(_sync_error_message(exc))
    return {
        "updatedCount": len(invoice_ids),
        "xeroSyncedCount": synced,
        "xeroFailedCount": failed,
        "errors": errors[:5],
    }
