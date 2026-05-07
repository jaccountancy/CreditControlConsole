from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone

from psycopg import connect
from psycopg.rows import dict_row

from .config import get_settings


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS xero_invoices (
    invoice_id UUID PRIMARY KEY,
    invoice_number TEXT,
    contact_name TEXT NOT NULL,
    status TEXT NOT NULL,
    currency_code TEXT,
    due_date DATE,
    amount_due NUMERIC(14, 2) NOT NULL,
    amount_paid NUMERIC(14, 2) NOT NULL,
    total NUMERIC(14, 2) NOT NULL,
    updated_date_utc TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS xero_invoices_due_date_idx
ON xero_invoices (due_date);

CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    fetched_count INTEGER NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@contextmanager
def get_connection():
    settings = get_settings()
    with connect(settings.database_url, row_factory=dict_row) as connection:
        yield connection


def ensure_schema() -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)
        connection.commit()


def upsert_invoices(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0

    query = """
    INSERT INTO xero_invoices (
        invoice_id,
        invoice_number,
        contact_name,
        status,
        currency_code,
        due_date,
        amount_due,
        amount_paid,
        total,
        updated_date_utc,
        synced_at
    )
    VALUES (
        %(invoice_id)s,
        %(invoice_number)s,
        %(contact_name)s,
        %(status)s,
        %(currency_code)s,
        %(due_date)s,
        %(amount_due)s,
        %(amount_paid)s,
        %(total)s,
        %(updated_date_utc)s,
        %(synced_at)s
    )
    ON CONFLICT (invoice_id) DO UPDATE SET
        invoice_number = EXCLUDED.invoice_number,
        contact_name = EXCLUDED.contact_name,
        status = EXCLUDED.status,
        currency_code = EXCLUDED.currency_code,
        due_date = EXCLUDED.due_date,
        amount_due = EXCLUDED.amount_due,
        amount_paid = EXCLUDED.amount_paid,
        total = EXCLUDED.total,
        updated_date_utc = EXCLUDED.updated_date_utc,
        synced_at = EXCLUDED.synced_at
    """

    now = datetime.now(timezone.utc)
    payload = [{**row, "synced_at": now} for row in rows]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(query, payload)
            cursor.execute(
                """
                INSERT INTO sync_runs (provider, fetched_count, synced_at)
                VALUES (%s, %s, %s)
                """,
                ("xero", len(payload), now),
            )
        connection.commit()

    return len(payload)
