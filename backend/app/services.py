from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow
from .xero import fetch_contacts_and_invoices, normalise_contact, normalise_invoice


def record_audit_event(entity_type: str, entity_id: str, event_type: str, payload: dict, user_id: str | None) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (entity_type, entity_id, event_type, __import__("json").dumps(payload), user_id),
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


async def run_sync(user: dict) -> dict:
    connection_row = get_xero_connection_for_user(user["id"])
    contacts, invoices = await fetch_contacts_and_invoices(connection_row)
    now = utcnow()

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO sync_runs (provider, initiated_by_user_id, status, created_at)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                ("xero", user["id"], "running", now),
            )
            sync_run = cursor.fetchone()

            for raw_contact in contacts:
                contact = normalise_contact(raw_contact, connection_row["tenant_id"])
                cursor.execute(
                    """
                    INSERT INTO customers (
                        tenant_id, xero_contact_id, name, email, phone, account_number, last_sync_at, updated_at
                    )
                    VALUES (%(tenant_id)s, %(xero_contact_id)s, %(name)s, %(email)s, %(phone)s, %(account_number)s, %(last_sync_at)s, %(updated_at)s)
                    ON CONFLICT (xero_contact_id) DO UPDATE
                    SET name = EXCLUDED.name,
                        email = EXCLUDED.email,
                        phone = EXCLUDED.phone,
                        account_number = EXCLUDED.account_number,
                        last_sync_at = EXCLUDED.last_sync_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    {**contact, "last_sync_at": now, "updated_at": now},
                )

            synced_invoices = 0
            customer_totals: dict[str, dict[str, Decimal]] = {}
            for raw_invoice in invoices:
                invoice = normalise_invoice(raw_invoice)
                if not invoice["xero_contact_id"]:
                    continue

                cursor.execute(
                    "SELECT id, name FROM customers WHERE xero_contact_id = %s",
                    (invoice["xero_contact_id"],),
                )
                customer = cursor.fetchone()
                if customer is None:
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
                        "customer_id": customer["id"],
                        "synced_at": now,
                        "updated_at": now,
                    },
                )
                stored = cursor.fetchone()
                synced_invoices += 1

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

                totals = customer_totals.setdefault(
                    customer["id"],
                    {"total_due": Decimal("0"), "overdue_amount": Decimal("0")},
                )
                amount_due = Decimal(str(invoice["amount_due"]))
                totals["total_due"] += amount_due
                if invoice["due_date"] and invoice["due_date"] < now.date() and amount_due > 0:
                    totals["overdue_amount"] += amount_due

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
                    customers_synced = %s,
                    invoices_synced = %s,
                    summary = %s,
                    completed_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    "completed",
                    len(contacts),
                    synced_invoices,
                    f"Synced {len(contacts)} customers and {synced_invoices} invoices from Xero.",
                    now,
                    sync_run["id"],
                ),
            )
            completed = cursor.fetchone()
        connection.commit()

    record_audit_event("sync_run", str(completed["id"]), "sync.completed", {"summary": completed["summary"]}, user["id"])
    return completed


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

    for customer_row in list_customers():
        detail = customer_detail(customer_row["id"])
        invoices = []
        for invoice in detail["invoices"]:
            invoice_payload = _serialize_invoice(invoice, invoice_detail(invoice["id"]))
            invoices.append(invoice_payload)
            if selected_invoice is None:
                selected_invoice = invoice_payload

        open_invoices = sum(1 for invoice in detail["invoices"] if _float(invoice.get("amount_due")) > 0)
        customers.append(
            {
                "id": customer_row["id"],
                "xeroContactId": customer_row.get("xero_contact_id") or "",
                "name": customer_row.get("name") or "",
                "email": customer_row.get("email") or "",
                "phone": customer_row.get("phone") or "",
                "contact": customer_row.get("email") or customer_row.get("phone") or "",
                "status": customer_row.get("status") or ("Action needed" if _float(customer_row.get("overdue_amount")) > 0 else "Current"),
                "openInvoices": open_invoices,
                "totalDue": _float(customer_row.get("total_due")),
                "overdue": _float(customer_row.get("overdue_amount")),
                "invoices": invoices,
            }
        )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM audit_events
                ORDER BY created_at DESC
                LIMIT 30
                """
            )
            audit_rows = cursor.fetchall()
        connection.commit()

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
