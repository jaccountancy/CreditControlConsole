from contextlib import contextmanager
from datetime import datetime, timezone

from psycopg import connect
from psycopg.rows import dict_row

from .config import get_settings


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS xero_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    xero_user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    tenant_name TEXT NOT NULL,
    tenant_type TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    scope TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id),
    UNIQUE (tenant_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS oauth_states (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    state_token TEXT NOT NULL UNIQUE,
    redirect_to TEXT,
    device_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS device_logins (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_code TEXT NOT NULL UNIQUE,
    verification_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
    session_token TEXT,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

ALTER TABLE device_logins
ADD COLUMN IF NOT EXISTS session_token TEXT;

CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    xero_contact_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    account_number TEXT,
    primary_person TEXT,
    contact_people JSONB NOT NULL DEFAULT '[]'::jsonb,
    addresses JSONB NOT NULL DEFAULT '[]'::jsonb,
    late_payment_charge_base_amount NUMERIC(14, 2),
    status TEXT NOT NULL DEFAULT 'active',
    total_due NUMERIC(14, 2) NOT NULL DEFAULT 0,
    overdue_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    last_sync_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE customers ADD COLUMN IF NOT EXISTS primary_person TEXT;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS contact_people JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS addresses JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS late_payment_charge_base_amount NUMERIC(14, 2);

CREATE INDEX IF NOT EXISTS customers_name_idx ON customers (name);

CREATE TABLE IF NOT EXISTS invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    xero_invoice_id TEXT NOT NULL UNIQUE,
    invoice_number TEXT NOT NULL,
    status TEXT NOT NULL,
    due_date DATE,
    invoice_date DATE,
    description TEXT,
    line_items JSONB NOT NULL DEFAULT '[]'::jsonb,
    currency_code TEXT,
    total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    amount_due NUMERIC(14, 2) NOT NULL DEFAULT 0,
    amount_paid NUMERIC(14, 2) NOT NULL DEFAULT 0,
    promised_date DATE,
    promise_status TEXT,
    control_status TEXT NOT NULL DEFAULT 'new',
    last_chased_at TIMESTAMPTZ,
    notes_summary TEXT,
    late_payment_charge_raised_at TIMESTAMPTZ,
    late_payment_charge_invoice_id TEXT,
    late_payment_charge_invoice_number TEXT,
    late_payment_charge_amount NUMERIC(14, 2),
    bad_debt_write_off_at TIMESTAMPTZ,
    bad_debt_credit_note_id TEXT,
    bad_debt_credit_note_number TEXT,
    bad_debt_credit_note_amount NUMERIC(14, 2),
    xero_updated_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS invoices_customer_idx ON invoices (customer_id);
CREATE INDEX IF NOT EXISTS invoices_due_date_idx ON invoices (due_date);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS line_items JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS late_payment_charge_raised_at TIMESTAMPTZ;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS late_payment_charge_invoice_id TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS late_payment_charge_invoice_number TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS late_payment_charge_amount NUMERIC(14, 2);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS bad_debt_write_off_at TIMESTAMPTZ;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS bad_debt_credit_note_id TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS bad_debt_credit_note_number TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS bad_debt_credit_note_amount NUMERIC(14, 2);

UPDATE customers
SET late_payment_charge_base_amount = historical.base_amount
FROM (
    SELECT DISTINCT ON (customer_id)
           customer_id,
           CASE ROUND(late_payment_charge_amount, 2)
               WHEN 24.00 THEN 20.00
               WHEN 36.00 THEN 30.00
               WHEN 60.00 THEN 50.00
           END AS base_amount
    FROM invoices
    WHERE late_payment_charge_amount IS NOT NULL
      AND ROUND(late_payment_charge_amount, 2) IN (24.00, 36.00, 60.00)
    ORDER BY customer_id, late_payment_charge_raised_at DESC NULLS LAST, updated_at DESC
) AS historical
WHERE customers.id = historical.customer_id
  AND customers.late_payment_charge_base_amount IS NULL;

CREATE TABLE IF NOT EXISTS invoice_status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    note TEXT,
    changed_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customer_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS customer_notes_customer_idx ON customer_notes (customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS payment_promises (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    promised_amount NUMERIC(14, 2) NOT NULL,
    promised_date DATE NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    customer_id UUID REFERENCES customers(id) ON DELETE CASCADE,
    invoice_id UUID REFERENCES invoices(id) ON DELETE SET NULL,
    xero_payment_id TEXT NOT NULL UNIQUE,
    xero_invoice_id TEXT,
    invoice_number TEXT,
    payment_date DATE,
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    currency_code TEXT,
    reference TEXT,
    status TEXT,
    account_name TEXT,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS payments_customer_idx ON payments (customer_id);
CREATE INDEX IF NOT EXISTS payments_invoice_idx ON payments (invoice_id);
CREATE INDEX IF NOT EXISTS payments_date_idx ON payments (payment_date);

CREATE TABLE IF NOT EXISTS customer_credits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    xero_credit_id TEXT NOT NULL,
    number TEXT,
    reference TEXT,
    status TEXT,
    transaction_type TEXT,
    credit_date DATE,
    currency_code TEXT,
    total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    remaining_credit NUMERIC(14, 2) NOT NULL DEFAULT 0,
    applied_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    line_items JSONB NOT NULL DEFAULT '[]'::jsonb,
    allocations JSONB NOT NULL DEFAULT '[]'::jsonb,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_type, xero_credit_id)
);

CREATE INDEX IF NOT EXISTS customer_credits_tenant_idx ON customer_credits (tenant_id);
CREATE INDEX IF NOT EXISTS customer_credits_customer_idx ON customer_credits (customer_id);
CREATE INDEX IF NOT EXISTS customer_credits_remaining_idx ON customer_credits (remaining_credit);

CREATE TABLE IF NOT EXISTS sync_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    initiated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    customers_synced INTEGER NOT NULL DEFAULT 0,
    invoices_synced INTEGER NOT NULL DEFAULT 0,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS provider TEXT DEFAULT 'xero';
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS initiated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'queued';
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS customers_synced INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS invoices_synced INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS fetched_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS processed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS current_step TEXT;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS contacts_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS invoices_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS rate_limit_until TIMESTAMPTZ;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS retry_after_seconds INTEGER NOT NULL DEFAULT 0;
UPDATE sync_runs
SET customers_synced = COALESCE(customers_synced, 0),
    invoices_synced = COALESCE(invoices_synced, 0),
    fetched_count = COALESCE(fetched_count, 0),
    processed_count = COALESCE(processed_count, 0),
    failed_count = COALESCE(failed_count, 0),
    heartbeat_at = COALESCE(heartbeat_at, started_at, created_at),
    contacts_total = COALESCE(contacts_total, 0),
    invoices_total = COALESCE(invoices_total, 0),
    retry_after_seconds = COALESCE(retry_after_seconds, 0);
ALTER TABLE sync_runs ALTER COLUMN customers_synced SET DEFAULT 0;
ALTER TABLE sync_runs ALTER COLUMN invoices_synced SET DEFAULT 0;
ALTER TABLE sync_runs ALTER COLUMN fetched_count SET DEFAULT 0;
ALTER TABLE sync_runs ALTER COLUMN processed_count SET DEFAULT 0;
ALTER TABLE sync_runs ALTER COLUMN failed_count SET DEFAULT 0;
ALTER TABLE sync_runs ALTER COLUMN contacts_total SET DEFAULT 0;
ALTER TABLE sync_runs ALTER COLUMN invoices_total SET DEFAULT 0;
ALTER TABLE sync_runs ALTER COLUMN retry_after_seconds SET DEFAULT 0;
ALTER TABLE sync_runs ALTER COLUMN customers_synced SET NOT NULL;
ALTER TABLE sync_runs ALTER COLUMN invoices_synced SET NOT NULL;
ALTER TABLE sync_runs ALTER COLUMN fetched_count SET NOT NULL;
ALTER TABLE sync_runs ALTER COLUMN processed_count SET NOT NULL;
ALTER TABLE sync_runs ALTER COLUMN failed_count SET NOT NULL;
ALTER TABLE sync_runs ALTER COLUMN contacts_total SET NOT NULL;
ALTER TABLE sync_runs ALTER COLUMN invoices_total SET NOT NULL;
ALTER TABLE sync_runs ALTER COLUMN retry_after_seconds SET NOT NULL;
DO $$
DECLARE
    counter_column RECORD;
BEGIN
    FOR counter_column IN
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'sync_runs'
          AND column_name LIKE '%\\_count' ESCAPE '\\'
          AND data_type IN ('smallint', 'integer', 'bigint', 'numeric')
    LOOP
        EXECUTE format('UPDATE sync_runs SET %I = 0 WHERE %I IS NULL', counter_column.column_name, counter_column.column_name);
        EXECUTE format('ALTER TABLE sync_runs ALTER COLUMN %I SET DEFAULT 0', counter_column.column_name);
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS sync_checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sync_run_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'xero',
    initiated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    tenant_id TEXT NOT NULL,
    sync_signature TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    page_number INTEGER NOT NULL DEFAULT 0,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_stored INTEGER NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    UNIQUE (sync_run_id, phase)
);

ALTER TABLE sync_checkpoints DROP CONSTRAINT IF EXISTS sync_checkpoints_sync_run_id_fkey;
ALTER TABLE sync_checkpoints ALTER COLUMN sync_run_id TYPE TEXT USING sync_run_id::text;

CREATE INDEX IF NOT EXISTS sync_checkpoints_resume_idx
ON sync_checkpoints (initiated_by_user_id, tenant_id, sync_signature, updated_at DESC);

CREATE INDEX IF NOT EXISTS sync_checkpoints_phase_idx
ON sync_checkpoints (sync_run_id, phase);

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
