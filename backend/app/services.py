import asyncio
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow
from .xero import (
    CONTACTS_URL,
    INVOICES_URL,
    create_history_record,
    fetch_paginated_collection,
    normalise_contact,
    normalise_invoice,
)

logger = logging.getLogger(__name__)
ACTIVE_SYNC_STATUSES = ("queued", "running")
OUTSTANDING_INVOICE_WHERE = 'Type=="ACCREC"&&Status!="VOIDED"&&Status!="DELETED"&&Status!="PAID"'
PAID_INVOICE_WHERE = 'Type=="ACCREC"&&Status=="PAID"'
OUTSTANDING_READY_STEP = "Backfilling paid invoices"
WORKING_DATA_STEPS = (OUTSTANDING_READY_STEP, "Fetching paid invoices from Xero", "Backfilling paid invoices")
DEFAULT_SYNC_SCOPE = "outstanding_only"
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
    year_summary = "No invoice years selected." if not invoice_years else f"Years: {', '.join(str(year) for year in invoice_years)}."
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


def _update_sync_run(sync_run_id: str, **fields) -> dict | None:
    if not fields:
        return None

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
    if not sync_options["invoice_years"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose at least one invoice year in Settings before importing from Xero.",
        )
    get_xero_connection_for_user(user["id"])

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
                  AND created_at < NOW() - INTERVAL '30 minutes'
                """,
                (
                    "failed",
                    "Sync timed out",
                    "A previous Xero sync stopped responding.",
                    "The previous Xero sync stopped responding. Start a fresh sync.",
                    utcnow(),
                    "xero",
                    user["id"],
                ),
            )
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
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                ),
            )
            sync_run = cursor.fetchone()
        connection.commit()

    return sync_run, True


def get_sync_run(user: dict, sync_run_id: str) -> dict:
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
    except Exception:
        logger.exception("Background Xero sync failed")


async def run_sync(user: dict, sync_run_id: str, sync_options: dict | None = None) -> dict:
    sync_options = normalise_sync_options(sync_options)
    connection_row = get_xero_connection_for_user(user["id"])
    now = utcnow()
    _update_sync_run(
        sync_run_id,
        status="running",
        current_step="Starting Xero sync",
        summary="Connecting to Xero.",
        started_at=now,
        error_message=None,
    )

    try:
        def contact_progress(_, total_records: int, __) -> None:
            _update_sync_run(
                sync_run_id,
                current_step="Fetching contacts from Xero",
                summary=f"Fetched {total_records} contacts from Xero.",
                customers_synced=total_records,
                contacts_total=total_records,
            )

        def outstanding_invoice_progress(_, total_records: int, __) -> None:
            _update_sync_run(
                sync_run_id,
                current_step="Fetching outstanding invoices from Xero",
                summary=f"Fetched {total_records} outstanding invoices from Xero.",
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

        contacts = await fetch_paginated_collection(
            connection_row,
            CONTACTS_URL,
            "Contacts",
            on_page=contact_progress,
        )
        _update_sync_run(
            sync_run_id,
            current_step="Fetching outstanding invoices from Xero",
            summary=f"Fetched {len(contacts)} contacts. Fetching outstanding invoices first.",
            contacts_total=len(contacts),
            customers_synced=len(contacts),
        )
        outstanding_invoices = await fetch_paginated_collection(
            connection_row,
            INVOICES_URL,
            "Invoices",
            params={"where": _with_invoice_year_filter(OUTSTANDING_INVOICE_WHERE, sync_options["invoice_years"])},
            on_page=outstanding_invoice_progress,
        )
        _update_sync_run(
            sync_run_id,
            current_step="Importing outstanding invoices",
            summary=f"Importing {len(contacts)} contacts and {len(outstanding_invoices)} outstanding invoices.",
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
                    current_step="Importing outstanding invoices",
                    summary=f"Imported {imported_contacts} contacts. Importing outstanding invoices.",
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
                                currency_code, total, amount_due, amount_paid, xero_updated_at, synced_at, updated_at
                            )
                            VALUES (
                                %(customer_id)s, %(xero_invoice_id)s, %(invoice_number)s, %(status)s, %(due_date)s, %(invoice_date)s,
                                %(currency_code)s, %(total)s, %(amount_due)s, %(amount_paid)s, %(xero_updated_at)s, %(synced_at)s, %(updated_at)s
                            )
                            ON CONFLICT (xero_invoice_id) DO UPDATE
                            SET customer_id = EXCLUDED.customer_id,
                                invoice_number = EXCLUDED.invoice_number,
                                status = EXCLUDED.status,
                                due_date = EXCLUDED.due_date,
                                invoice_date = EXCLUDED.invoice_date,
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

                outstanding_synced = import_invoice_batch(outstanding_invoices, "outstanding", True)

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
                        OUTSTANDING_READY_STEP,
                        len(contacts),
                        synced_invoices,
                        len(contacts) + len(outstanding_invoices),
                        len(contacts) + synced_invoices,
                        0,
                        len(contacts),
                        len(outstanding_invoices),
                        f"Outstanding invoices ready: synced {outstanding_synced} outstanding invoices. Backfilling paid invoices.",
                        None,
                        sync_run_id,
                    ),
                )
                outstanding_ready = cursor.fetchone()
            connection.commit()

        record_audit_event(
            "sync_run",
            str(outstanding_ready["id"]),
            "sync.outstanding_ready",
            {"summary": outstanding_ready["summary"]},
            user["id"],
        )

        paid_page_limit = sync_options["paid_page_limit"]
        if paid_page_limit == 0:
            completed = _update_sync_run(
                sync_run_id,
                status="completed",
                current_step="Outstanding sync complete",
                summary=(
                    f"Synced {len(contacts)} customers and {outstanding_synced} outstanding invoices. "
                    "Paid invoice history was left for a later staged sync."
                ),
                customers_synced=len(contacts),
                invoices_synced=synced_invoices,
                fetched_count=len(contacts) + len(outstanding_invoices),
                processed_count=len(contacts) + synced_invoices,
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
            summary=f"Outstanding invoices are ready. {sync_options['summary']}",
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
                            currency_code, total, amount_due, amount_paid, xero_updated_at, synced_at, updated_at
                        )
                        VALUES (
                            %(customer_id)s, %(xero_invoice_id)s, %(invoice_number)s, %(status)s, %(due_date)s, %(invoice_date)s,
                            %(currency_code)s, %(total)s, %(amount_due)s, %(amount_paid)s, %(xero_updated_at)s, %(synced_at)s, %(updated_at)s
                        )
                        ON CONFLICT (xero_invoice_id) DO UPDATE
                        SET customer_id = EXCLUDED.customer_id,
                            invoice_number = EXCLUDED.invoice_number,
                            status = EXCLUDED.status,
                            due_date = EXCLUDED.due_date,
                            invoice_date = EXCLUDED.invoice_date,
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
                        len(contacts) + len(outstanding_invoices) + len(paid_invoices),
                        len(contacts) + synced_invoices,
                        0,
                        len(contacts),
                        len(outstanding_invoices) + len(paid_invoices),
                        f"Synced {len(contacts)} customers, {outstanding_synced} outstanding invoices, and {paid_synced} paid invoices from Xero. Scope: {sync_options['label']}.",
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
                    COALESCE(SUM(amount_due), 0) AS total_receivables,
                    COALESCE(SUM(CASE WHEN due_date < CURRENT_DATE THEN amount_due ELSE 0 END), 0) AS total_overdue,
                    COALESCE(SUM(CASE WHEN due_date >= CURRENT_DATE - INTERVAL '30 days' AND due_date < CURRENT_DATE THEN amount_due ELSE 0 END), 0) AS overdue_1_30,
                    COALESCE(SUM(CASE WHEN due_date >= CURRENT_DATE - INTERVAL '60 days' AND due_date < CURRENT_DATE - INTERVAL '30 days' THEN amount_due ELSE 0 END), 0) AS overdue_31_60,
                    COALESCE(SUM(CASE WHEN due_date >= CURRENT_DATE - INTERVAL '90 days' AND due_date < CURRENT_DATE - INTERVAL '60 days' THEN amount_due ELSE 0 END), 0) AS overdue_61_90,
                    COALESCE(SUM(CASE WHEN due_date < CURRENT_DATE - INTERVAL '90 days' THEN amount_due ELSE 0 END), 0) AS overdue_90_plus,
                    COUNT(DISTINCT CASE WHEN due_date < CURRENT_DATE AND amount_due > 0 THEN customer_id END) AS accounts_needing_action
                FROM invoices
                """
            )
            summary = cursor.fetchone()
            cursor.execute(
                """
                SELECT customers.name, invoices.amount_due, invoices.due_date
                FROM invoices
                JOIN customers ON customers.id = invoices.customer_id
                WHERE invoices.amount_due > 0
                ORDER BY invoices.amount_due DESC, invoices.due_date ASC NULLS LAST
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
        "total_receivables": float(summary["total_receivables"] or 0),
        "total_overdue": float(summary["total_overdue"] or 0),
        "overdue_1_30": float(summary["overdue_1_30"] or 0),
        "overdue_31_60": float(summary["overdue_31_60"] or 0),
        "overdue_61_90": float(summary["overdue_61_90"] or 0),
        "overdue_90_plus": float(summary["overdue_90_plus"] or 0),
        "accounts_needing_action": summary["accounts_needing_action"] or 0,
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
        "currencyCode": invoice.get("currency_code") or "GBP",
        "total": _float(invoice.get("total")),
        "amountDue": _float(invoice.get("amount_due")),
        "amountPaid": _float(invoice.get("amount_paid")),
        "promisedDate": _iso(invoice.get("promised_date")),
        "promiseStatus": invoice.get("promise_status") or "",
        "lastChasedAt": _iso(invoice.get("last_chased_at")),
        "overdueDays": invoice.get("overdue_days") or 0,
        "latePayment": invoice.get("late_payment") or {"interest": 0, "court_cost": 35},
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
        connection.commit()

    invoices_by_customer: dict[str, list[dict]] = {}
    notes_by_customer: dict[str, list[dict]] = {}
    for note in customer_note_rows:
        notes_by_customer.setdefault(note["customer_id"], []).append(note)

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
            invoice_payload = _serialize_invoice(invoice)
            invoices.append(invoice_payload)
            if selected_invoice is None:
                selected_invoice = invoice_payload

        open_invoices = sum(1 for invoice in detail_invoices if _float(invoice.get("amount_due")) > 0)
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
    return f"Credit Control Console note from {author}: {body.strip()}"


async def sync_invoice_note_to_xero(invoice_id: str, user: dict, body: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, xero_invoice_id, invoice_number
                FROM invoices
                WHERE id = %s
                """,
                (invoice_id,),
            )
            invoice = cursor.fetchone()
        connection.commit()

    if invoice is None:
        return {"synced": False, "error": "Invoice not found."}
    if not invoice.get("xero_invoice_id"):
        return {"synced": False, "error": "Invoice is not linked to a Xero invoice."}

    try:
        connection_row = get_xero_connection_for_user(user["id"])
        await create_history_record(connection_row, "Invoices", invoice["xero_invoice_id"], _format_xero_note(user, body))
    except Exception as exc:
        error = _sync_error_message(exc)
        record_audit_event(
            "invoice",
            invoice_id,
            "xero.note.failed",
            {"error": error, "invoice_number": invoice.get("invoice_number"), "detail": _sync_error_payload(exc)},
            user["id"],
        )
        return {"synced": False, "error": error}

    record_audit_event(
        "invoice",
        invoice_id,
        "xero.note.synced",
        {"invoice_number": invoice.get("invoice_number"), "xero_invoice_id": invoice.get("xero_invoice_id")},
        user["id"],
    )
    return {"synced": True, "error": ""}


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
                    updated_at = %s
                WHERE id = %s
                """,
                (promised_date, "open", utcnow(), invoice_id),
            )
        connection.commit()
    record_audit_event(
        "invoice",
        invoice_id,
        "promise.created",
        {"promised_amount": promised_amount, "promised_date": promised_date, "note": note},
        user["id"],
    )


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
