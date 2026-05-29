import asyncio
import json
import logging
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import httpx
from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow
from .xero import (
    CREDIT_NOTES_URL,
    INVOICES_URL,
    OVERPAYMENTS_URL,
    PAYMENTS_URL,
    allocate_credit_note,
    allocate_overpayment,
    create_credit_note,
    create_history_record,
    create_sales_invoice,
    fetch_paginated_collection,
    normalise_contact,
    normalise_invoice,
    normalise_payment,
    xero_api_get,
)

logger = logging.getLogger(__name__)
ACTIVE_SYNC_STATUSES = ("queued", "running")
SYNC_STALE_AFTER = timedelta(minutes=30)
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
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User has not linked Xero yet.")
    return row


def disconnect_xero(user: dict) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM xero_connections
                WHERE user_id = %s
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
        UPDATE customers
        SET total_due = 0,
            overdue_amount = 0,
            updated_at = %s
        WHERE tenant_id = %s
        """,
        (updated_at, tenant_id),
    )
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
    on_page=None,
    on_store=None,
) -> int:
    raw_payments = await fetch_paginated_collection(
        connection_row,
        PAYMENTS_URL,
        "Payments",
        on_page=on_page,
        modified_since=modified_since,
    )
    if not raw_payments:
        return 0

    total_payments = len(raw_payments)
    if on_store is not None:
        on_store(0, total_payments, 0)

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

            synced = 0
            processed = 0
            for raw_payment in raw_payments:
                processed += 1
                payment = normalise_payment(raw_payment)
                if not payment.get("xero_payment_id"):
                    if on_store is not None and (processed % 100 == 0 or processed == total_payments):
                        on_store(processed, total_payments, synced)
                    continue
                if payment.get("invoice_type") and payment.get("invoice_type") != "ACCREC":
                    if on_store is not None and (processed % 100 == 0 or processed == total_payments):
                        on_store(processed, total_payments, synced)
                    continue
                invoice_match = invoice_lookup.get(payment.get("xero_invoice_id"))
                customer_id = customer_lookup.get(payment.get("xero_contact_id")) or (invoice_match or {}).get("customer_id")
                if customer_id is None:
                    if on_store is not None and (processed % 100 == 0 or processed == total_payments):
                        on_store(processed, total_payments, synced)
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
                synced += 1
                if on_store is not None and (processed % 100 == 0 or processed == total_payments):
                    on_store(processed, total_payments, synced)
        connection.commit()
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
            "credit_date": _parse_iso_date(source.get("date")),
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
    on_overpayment_page=None,
) -> int:
    credit_note_where = RECEIVABLE_CREDIT_NOTE_INCREMENTAL_WHERE if modified_since else RECEIVABLE_CREDIT_NOTE_WHERE
    raw_credit_notes = await fetch_paginated_collection(
        connection_row,
        CREDIT_NOTES_URL,
        "CreditNotes",
        params={"where": credit_note_where},
        modified_since=modified_since,
        on_page=on_credit_note_page,
    )
    raw_overpayments = await fetch_paginated_collection(
        connection_row,
        OVERPAYMENTS_URL,
        "Overpayments",
        params=None if modified_since else {"where": f'Status=="{AUTHORISED_CREDIT_STATUS}"'},
        modified_since=modified_since,
        on_page=on_overpayment_page,
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
    stale_before = utcnow() - SYNC_STALE_AFTER
    with get_connection() as connection:
        with connection.cursor() as cursor:
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
                  AND COALESCE(heartbeat_at, started_at, created_at) < %s
                """,
                (
                    "failed",
                    "Sync timed out",
                    "A previous Xero sync stopped responding.",
                    "The previous Xero sync stopped responding. Start a fresh sync.",
                    utcnow(),
                    "xero",
                    user_id,
                    stale_before,
                ),
            )
        connection.commit()


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
        "isActive": sync_run.get("status") in ACTIVE_SYNC_STATUSES,
    }


def sync_run_has_working_data(sync_run: dict) -> bool:
    return (
        sync_run.get("status") in ACTIVE_SYNC_STATUSES
        and sync_run.get("current_step") in WORKING_DATA_STEPS
        and int(sync_run.get("invoices_synced") or 0) > 0
    )


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
                      OR audit_events.entity_type = 'xero_connection'
                      OR audit_events.event_type LIKE 'sync.%%'
                      OR audit_events.event_type LIKE 'xero.%%'
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
            "syncRunId": row.get("entity_id") if row.get("entity_type") == "sync_run" else "",
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


async def run_sync(user: dict, sync_run_id: str, sync_options: dict | None = None) -> dict:
    sync_options = normalise_sync_options(sync_options)
    connection_row = get_xero_connection_for_user(user["id"])
    now = utcnow()
    candidate_modified_since = _incremental_modified_since(user["id"])
    years_are_already_imported = (
        _local_invoice_years_cover(connection_row["tenant_id"], sync_options["invoice_years"])
        if candidate_modified_since is not None
        else False
    )
    force_full_history_refresh = (
        sync_options["invoice_scope"] == "full_history"
        or sync_options["paid_page_limit"] is None
    )
    modified_since = None if force_full_history_refresh else candidate_modified_since if years_are_already_imported else None
    is_incremental_sync = modified_since is not None
    scope_already_imported = (
        _completed_sync_covers_scope(user["id"], sync_options["invoice_scope"])
        if is_incremental_sync
        else False
    )
    needs_paid_backfill = sync_options["paid_page_limit"] != 0 and not scope_already_imported
    contact_fetch_label = "changed customer records" if is_incremental_sync else "customer records"
    invoice_fetch_label = "changed invoices" if is_incremental_sync else "outstanding invoices"
    invoice_fetch_step = f"Fetching {invoice_fetch_label} from Xero"
    invoice_where = _with_invoice_year_filter(
        ACCREC_INVOICE_WHERE if is_incremental_sync else OUTSTANDING_INVOICE_WHERE,
        sync_options["invoice_years"],
    )
    if is_incremental_sync:
        sync_mode_summary = f"Incremental sync from {modified_since.isoformat()}."
    elif force_full_history_refresh:
        sync_mode_summary = "Full history refresh requested; fetching the selected years without incremental filters."
    elif candidate_modified_since is not None:
        sync_mode_summary = "Full sync for newly selected invoice years."
    else:
        sync_mode_summary = "First full sync for the selected scope."
    _update_sync_run(
        sync_run_id,
        status="running",
        current_step="Starting Xero sync",
        summary=f"Connecting to Xero. {sync_mode_summary}",
        started_at=now,
        error_message=None,
    )

    try:
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
                    f"Processed {processed_records} of {total_records} Xero payments; "
                    f"stored {stored_records} matching customer payments."
                ),
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
            _update_sync_run(
                sync_run_id,
                current_step="Fetching payments from Xero",
                summary="Pulling payments made against Xero invoices.",
            )
            try:
                payments_synced = await _sync_xero_payments(
                    connection_row,
                    utcnow(),
                    modified_since=modified_since if is_incremental_sync else None,
                    on_page=payment_progress,
                    on_store=payment_store_progress,
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
            _update_sync_run(
                sync_run_id,
                current_step="Fetching customer credits from Xero",
                summary="Pulling credit notes and overpayments that reduce debtor balances.",
            )
            return await _sync_xero_customer_credits(
                connection_row,
                utcnow(),
                modified_since=modified_since if is_incremental_sync else None,
                on_credit_note_page=credit_note_progress,
                on_overpayment_page=overpayment_progress,
            )

        _update_sync_run(
            sync_run_id,
            current_step=invoice_fetch_step,
            summary=f"Fetching {invoice_fetch_label}. Customer records will be built from invoice contacts.",
        )
        outstanding_invoices = await fetch_paginated_collection(
            connection_row,
            INVOICES_URL,
            "Invoices",
            params={"where": invoice_where},
            on_page=outstanding_invoice_progress,
            modified_since=modified_since,
        )
        contacts = _contacts_from_invoices(outstanding_invoices)
        _update_sync_run(
            sync_run_id,
            current_step=f"Importing {invoice_fetch_label}",
            summary=(
                f"Fetched {len(outstanding_invoices)} {invoice_fetch_label}. "
                f"Prepared {len(contacts)} customer records from invoice contacts."
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
                    contact = normalise_contact(raw_contact, connection_row["tenant_id"])
                    cursor.execute(
                        """
                        INSERT INTO customers (
                            tenant_id, xero_contact_id, name, email, phone, account_number,
                            primary_person, contact_people, addresses, last_sync_at, updated_at
                        )
                        VALUES (
                            %(tenant_id)s, %(xero_contact_id)s, %(name)s, %(email)s, %(phone)s, %(account_number)s,
                            %(primary_person)s, %(contact_people_json)s::jsonb, %(addresses_json)s::jsonb, %(last_sync_at)s, %(updated_at)s
                        )
                        ON CONFLICT (xero_contact_id) DO UPDATE
                        SET name = EXCLUDED.name,
                            email = EXCLUDED.email,
                            phone = EXCLUDED.phone,
                            account_number = EXCLUDED.account_number,
                            primary_person = EXCLUDED.primary_person,
                            contact_people = EXCLUDED.contact_people,
                            addresses = EXCLUDED.addresses,
                            last_sync_at = EXCLUDED.last_sync_at,
                            updated_at = EXCLUDED.updated_at
                        """,
                        {
                            **contact,
                            "contact_people_json": json.dumps(contact.get("contact_people") or [], default=_json_default),
                            "addresses_json": json.dumps(contact.get("addresses") or [], default=_json_default),
                            "last_sync_at": now,
                            "updated_at": now,
                        },
                    )
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

                cursor.execute(
                    """
                    UPDATE customers
                    SET total_due = 0,
                        overdue_amount = 0,
                        updated_at = %s
                    WHERE tenant_id = %s
                    """,
                    (now, connection_row["tenant_id"]),
                )

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

        _update_sync_run(
            sync_run_id,
            current_step="Fetching paid invoices from Xero",
            summary=(
                f"Changed invoices are ready. Backfilling {sync_options['label'].lower()}."
                if is_incremental_sync
                else f"Outstanding invoices are ready. {sync_options['summary']}"
            ),
        )
        paid_invoices = await fetch_paginated_collection(
            connection_row,
            INVOICES_URL,
            "Invoices",
            params={"where": _with_invoice_year_filter(PAID_INVOICE_WHERE, sync_options["invoice_years"])},
            max_pages=paid_page_limit,
            on_page=paid_invoice_progress,
        )
        paid_synced = 0

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, xero_contact_id FROM customers WHERE tenant_id = %s",
                    (connection_row["tenant_id"],),
                )
                customer_lookup = {
                    row["xero_contact_id"]: row["id"]
                    for row in cursor.fetchall()
                }

                _update_sync_run(
                    sync_run_id,
                    current_step="Backfilling paid invoices",
                    summary=f"Backfilling {len(paid_invoices)} paid invoices from Xero.",
                    invoices_total=len(outstanding_invoices) + len(paid_invoices),
                )
                for raw_invoice in paid_invoices:
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
                    paid_synced += 1

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

                    if paid_synced % 100 == 0:
                        _update_sync_run(
                            sync_run_id,
                            current_step="Backfilling paid invoices",
                            summary=f"Backfilled {paid_synced} of {len(paid_invoices)} paid invoices.",
                            customers_synced=len(contacts),
                            invoices_synced=synced_invoices,
                            processed_count=len(contacts) + synced_invoices,
                        )

                _refresh_customer_totals(cursor, connection_row["tenant_id"], now)
                completion_summary = (
                    f"Incremental sync complete: refreshed {len(contacts)} changed contacts, "
                    f"{outstanding_synced} changed invoices, and backfilled {paid_synced} paid invoices. "
                    f"Pulled through {payments_synced} payments and {credit_sources_synced} customer credits. Scope: {sync_options['label']}."
                    if is_incremental_sync
                    else f"Synced {len(contacts)} customers, {outstanding_synced} outstanding invoices, {paid_synced} paid invoices, {payments_synced} payments, and {credit_sources_synced} customer credits from Xero. Scope: {sync_options['label']}."
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
                        "completed",
                        "Sync complete",
                        len(contacts),
                        synced_invoices,
                        len(contacts) + len(outstanding_invoices) + len(paid_invoices) + payments_synced + credit_sources_synced,
                        len(contacts) + synced_invoices + payments_synced + credit_sources_synced,
                        0,
                        len(contacts),
                        len(outstanding_invoices) + len(paid_invoices),
                        completion_summary,
                        utcnow(),
                        sync_run_id,
                    ),
                )
                completed = cursor.fetchone()
            connection.commit()

        record_audit_event("sync_run", str(completed["id"]), "sync.completed", {"summary": completed["summary"]}, user["id"])
        return completed
    except Exception as exc:
        message = _sync_error_message(exc)
        _update_sync_run(
            sync_run_id,
            status="failed",
            current_step="Sync failed",
            summary="Xero sync failed.",
            error_message=message,
            failed_count=1,
            completed_at=utcnow(),
        )
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


def dashboard_payload() -> dict:
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
                """
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
                """
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
                GROUP BY customers.id, customers.name, customers.total_due
                ORDER BY customers.total_due DESC, due_date ASC NULLS LAST
                LIMIT 5
                """
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

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM customers
                ORDER BY overdue_amount DESC, total_due DESC, name ASC
                """
            )
            customer_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT *
                FROM invoices
                ORDER BY due_date ASC NULLS LAST, invoice_number ASC
                """
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
                LEFT JOIN users ON users.id = customer_notes.user_id
                ORDER BY customer_notes.created_at DESC
                """
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
                ORDER BY payment_date DESC NULLS LAST, created_at DESC
                """
            )
            payment_rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT customer_id,
                       COALESCE(SUM(remaining_credit), 0) AS credit_balance,
                       COUNT(*) AS credit_count
                FROM customer_credits
                WHERE remaining_credit > 0
                GROUP BY customer_id
                """
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

    xero_connected = False
    xero_connection = None
    if user and user.get("id"):
        try:
            xero_connection = get_xero_connection_for_user(user["id"])
            xero_connected = True
        except HTTPException:
            xero_connected = False

    dashboard = dashboard_payload()
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


def _parse_iso_date(value) -> date | None:
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
    due_date = _parse_iso_date(invoice.get("dueDate"))
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
            due_date = _parse_iso_date(invoice.get("dueDate"))
            invoice_date = _parse_iso_date(invoice.get("invoiceDate")) or due_date
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
        invoice_date = _parse_iso_date(invoice["invoiceDate"])
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
        async with httpx.AsyncClient(timeout=45) as client:
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
                       customers.xero_contact_id
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
        charge_base_amount = charge_selection_by_invoice_id[str(invoice["id"])]
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
                           customers.xero_contact_id
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

                charge_base_amount = charge_selection_by_invoice_id[str(locked_invoice["id"])]
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
            f"via jeNIUS AI WE HAVE WRITTEN OFF INVOICE '{invoice_number}'. "
            f"Credit note {credit_note_number or credit_note_id} was raised for {amount_label} to account code {account_code} "
            "Irrecoverable Receivables / Bad Debt Write Off and allocated to the invoice. "
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
            f"Invoice written off via Xero credit note {credit_note_number or credit_note_id}. "
            f"Amount: {amount_label}. Account code: {account_code} Irrecoverable Receivables / Bad Debt Write Off."
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
