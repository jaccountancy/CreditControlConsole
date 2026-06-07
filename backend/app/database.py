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
    UNIQUE (tenant_id)
);

ALTER TABLE xero_connections DROP CONSTRAINT IF EXISTS xero_connections_user_id_key;
CREATE INDEX IF NOT EXISTS xero_connections_user_idx ON xero_connections (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS xero_tenant_company_mappings (
    tenant_id TEXT PRIMARY KEY,
    company_number TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE xero_tenant_company_mappings ADD COLUMN IF NOT EXISTS company_number TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_tenant_company_mappings ADD COLUMN IF NOT EXISTS client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_tenant_company_mappings ADD COLUMN IF NOT EXISTS company_name TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_tenant_company_mappings ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_tenant_company_mappings ADD COLUMN IF NOT EXISTS updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE xero_tenant_company_mappings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE xero_tenant_company_mappings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS xero_tenant_company_mappings_company_number_idx ON xero_tenant_company_mappings (company_number);

CREATE TABLE IF NOT EXISTS xero_lock_date_snapshots (
    tenant_id TEXT PRIMARY KEY,
    period_lock_date DATE,
    end_of_year_lock_date DATE,
    base_currency TEXT NOT NULL DEFAULT '',
    xero_error TEXT NOT NULL DEFAULT '',
    last_synced_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE xero_lock_date_snapshots ADD COLUMN IF NOT EXISTS period_lock_date DATE;
ALTER TABLE xero_lock_date_snapshots ADD COLUMN IF NOT EXISTS end_of_year_lock_date DATE;
ALTER TABLE xero_lock_date_snapshots ADD COLUMN IF NOT EXISTS base_currency TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_lock_date_snapshots ADD COLUMN IF NOT EXISTS xero_error TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_lock_date_snapshots ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;
ALTER TABLE xero_lock_date_snapshots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS xero_posting_settings (
    tenant_id TEXT PRIMARY KEY,
    late_payment_charge_account_code TEXT NOT NULL DEFAULT '1222',
    late_payment_charge_account_name TEXT NOT NULL DEFAULT '',
    late_payment_charge_tax_type TEXT NOT NULL DEFAULT 'OUTPUT2',
    bad_debt_write_off_account_code TEXT NOT NULL DEFAULT '402',
    bad_debt_write_off_account_name TEXT NOT NULL DEFAULT '',
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS late_payment_charge_account_code TEXT NOT NULL DEFAULT '1222';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS late_payment_charge_account_name TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS late_payment_charge_tax_type TEXT NOT NULL DEFAULT 'OUTPUT2';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS bad_debt_write_off_account_code TEXT NOT NULL DEFAULT '402';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS bad_debt_write_off_account_name TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS oauth_states (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    state_token TEXT NOT NULL UNIQUE,
    redirect_to TEXT,
    device_code TEXT,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    provider TEXT NOT NULL DEFAULT 'xero',
    code_verifier TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'xero';
ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS code_verifier TEXT;
ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;

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

CREATE TABLE IF NOT EXISTS vat_return_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    transaction_id TEXT NOT NULL,
    line_index INTEGER NOT NULL DEFAULT 0,
    transaction_date DATE,
    reference TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    net_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    tax_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    gross_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    tax_code TEXT NOT NULL DEFAULT '',
    account_code TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    transaction_type TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL DEFAULT '',
    xero_invoice_id TEXT NOT NULL DEFAULT '',
    xero_updated_at TIMESTAMPTZ,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, tenant_id, customer_id, period_end, transaction_id)
);

ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id) ON DELETE CASCADE;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS period_start DATE;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS period_end DATE;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS transaction_id TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS line_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS transaction_date DATE;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS reference TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS net_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS tax_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS gross_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS tax_code TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS account_code TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS transaction_type TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS document_type TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS xero_invoice_id TEXT NOT NULL DEFAULT '';
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS xero_updated_at TIMESTAMPTZ;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS raw JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE vat_return_transactions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS vat_return_transactions_unique_period_tx_idx
ON vat_return_transactions (user_id, tenant_id, customer_id, period_end, transaction_id);

CREATE INDEX IF NOT EXISTS vat_return_transactions_period_idx
ON vat_return_transactions (user_id, tenant_id, customer_id, period_end, transaction_date DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS vat_return_transactions_invoice_idx
ON vat_return_transactions (user_id, tenant_id, customer_id, period_end, xero_invoice_id, updated_at DESC);

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
CREATE INDEX IF NOT EXISTS invoice_status_history_invoice_created_idx ON invoice_status_history (invoice_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS notes_invoice_created_idx ON notes (invoice_id, created_at DESC);

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
CREATE INDEX IF NOT EXISTS payment_promises_invoice_created_idx ON payment_promises (invoice_id, created_at DESC);

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
    tenant_id TEXT,
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
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT;
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
SET tenant_id = xero_connections.tenant_id
FROM xero_connections
WHERE sync_runs.provider = 'xero'
  AND sync_runs.tenant_id IS NULL
  AND sync_runs.initiated_by_user_id = xero_connections.user_id;
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

CREATE INDEX IF NOT EXISTS sync_runs_provider_user_tenant_idx
ON sync_runs (provider, initiated_by_user_id, tenant_id, created_at DESC);

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

CREATE TABLE IF NOT EXISTS operation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_type TEXT NOT NULL,
    initiated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    total_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    succeeded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    current_step TEXT,
    summary TEXT,
    error_message TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS operation_type TEXT;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS initiated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS total_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS processed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS succeeded_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS current_step TEXT;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS result JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE operation_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS operation_runs_user_status_idx
ON operation_runs (initiated_by_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS xero_pending_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT,
    action_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    invoice_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS action_type TEXT NOT NULL DEFAULT 'late_payment_charges';
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS invoice_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS result JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE xero_pending_actions ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS xero_pending_actions_user_status_idx
ON xero_pending_actions (created_by_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS jashflow_loans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    principal_amount NUMERIC(14, 2) NOT NULL,
    arrangement_fee NUMERIC(14, 2) NOT NULL DEFAULT 0,
    annual_interest_rate NUMERIC(9, 6) NOT NULL DEFAULT 0,
    duration_months INTEGER NOT NULL,
    start_date DATE NOT NULL,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id) ON DELETE CASCADE;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS principal_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS arrangement_fee NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS annual_interest_rate NUMERIC(9, 6) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS duration_months INTEGER NOT NULL DEFAULT 1;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS start_date DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE jashflow_loans ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS jashflow_loans_tenant_status_idx
ON jashflow_loans (tenant_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS jashflow_loans_customer_idx
ON jashflow_loans (customer_id);

CREATE TABLE IF NOT EXISTS jashflow_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    loan_id UUID NOT NULL REFERENCES jashflow_loans(id) ON DELETE CASCADE,
    transaction_date DATE NOT NULL,
    transaction_type TEXT NOT NULL,
    amount NUMERIC(14, 2) NOT NULL,
    description TEXT,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE jashflow_transactions ADD COLUMN IF NOT EXISTS loan_id UUID REFERENCES jashflow_loans(id) ON DELETE CASCADE;
ALTER TABLE jashflow_transactions ADD COLUMN IF NOT EXISTS transaction_date DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE jashflow_transactions ADD COLUMN IF NOT EXISTS transaction_type TEXT NOT NULL DEFAULT 'adjustment';
ALTER TABLE jashflow_transactions ADD COLUMN IF NOT EXISTS amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_transactions ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE jashflow_transactions ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE jashflow_transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS jashflow_transactions_loan_date_idx
ON jashflow_transactions (loan_id, transaction_date, created_at);

CREATE TABLE IF NOT EXISTS jashflow_settings (
    tenant_id TEXT PRIMARY KEY,
    invoice_contact_id TEXT,
    invoice_contact_name TEXT,
    interest_account_code TEXT,
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE jashflow_settings ADD COLUMN IF NOT EXISTS invoice_contact_id TEXT;
ALTER TABLE jashflow_settings ADD COLUMN IF NOT EXISTS invoice_contact_name TEXT;
ALTER TABLE jashflow_settings ADD COLUMN IF NOT EXISTS interest_account_code TEXT;
ALTER TABLE jashflow_settings ADD COLUMN IF NOT EXISTS updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE jashflow_settings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE jashflow_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS jashflow_interest_post_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    xero_invoice_id TEXT,
    xero_invoice_number TEXT,
    invoice_contact_id TEXT NOT NULL,
    invoice_contact_name TEXT,
    interest_account_code TEXT NOT NULL,
    period_end_date DATE NOT NULL,
    total_interest_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    attachment_filename TEXT,
    error_message TEXT,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed';
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS xero_invoice_id TEXT;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS xero_invoice_number TEXT;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS invoice_contact_id TEXT;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS invoice_contact_name TEXT;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS interest_account_code TEXT;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS period_end_date DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS total_interest_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS attachment_filename TEXT;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE jashflow_interest_post_batches ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS jashflow_interest_batches_tenant_idx
ON jashflow_interest_post_batches (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS jashflow_interest_post_lines (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES jashflow_interest_post_batches(id) ON DELETE CASCADE,
    loan_id UUID NOT NULL REFERENCES jashflow_loans(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    period_end_date DATE NOT NULL,
    accrued_interest_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    previously_posted_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    interest_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    balance_after_interest NUMERIC(14, 2) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS batch_id UUID REFERENCES jashflow_interest_post_batches(id) ON DELETE CASCADE;
ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS loan_id UUID REFERENCES jashflow_loans(id) ON DELETE CASCADE;
ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id) ON DELETE CASCADE;
ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS period_end_date DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS accrued_interest_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS previously_posted_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS interest_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS balance_after_interest NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE jashflow_interest_post_lines ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS jashflow_interest_lines_loan_idx
ON jashflow_interest_post_lines (loan_id, created_at DESC);

CREATE TABLE IF NOT EXISTS bank_statement_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, customer_id)
);

ALTER TABLE bank_statement_clients ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE bank_statement_clients ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id) ON DELETE CASCADE;
ALTER TABLE bank_statement_clients ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE bank_statement_clients ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE bank_statement_clients ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE bank_statement_clients ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS bank_statement_clients_tenant_idx
ON bank_statement_clients (tenant_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS bank_statement_clients_unique_customer_idx
ON bank_statement_clients (tenant_id, customer_id);

CREATE TABLE IF NOT EXISTS supplier_reconciliation_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    xero_contact_id TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, xero_contact_id)
);

ALTER TABLE supplier_reconciliation_clients ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE supplier_reconciliation_clients ADD COLUMN IF NOT EXISTS xero_contact_id TEXT;
ALTER TABLE supplier_reconciliation_clients ADD COLUMN IF NOT EXISTS contact_name TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_reconciliation_clients ADD COLUMN IF NOT EXISTS contact_email TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_reconciliation_clients ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE supplier_reconciliation_clients ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE supplier_reconciliation_clients ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE supplier_reconciliation_clients ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS supplier_reconciliation_clients_tenant_idx
ON supplier_reconciliation_clients (tenant_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS supplier_reconciliation_clients_unique_contact_idx
ON supplier_reconciliation_clients (tenant_id, xero_contact_id);

CREATE TABLE IF NOT EXISTS bank_statement_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_client_id UUID NOT NULL REFERENCES bank_statement_clients(id) ON DELETE CASCADE,
    account_name TEXT NOT NULL,
    nickname TEXT,
    bank_provider TEXT NOT NULL DEFAULT '',
    account_number TEXT NOT NULL,
    sort_code TEXT,
    currency_code TEXT NOT NULL DEFAULT 'GBP',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS extraction_client_id UUID REFERENCES bank_statement_clients(id) ON DELETE CASCADE;
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT 'Bank account';
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS nickname TEXT;
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS bank_provider TEXT NOT NULL DEFAULT '';
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS account_number TEXT NOT NULL DEFAULT '';
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS sort_code TEXT;
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS currency_code TEXT NOT NULL DEFAULT 'GBP';
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS bank_statement_accounts_client_idx
ON bank_statement_accounts (extraction_client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS bank_statement_uploads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bank_account_id UUID NOT NULL REFERENCES bank_statement_accounts(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error_message TEXT,
    source_file BYTEA,
    source_file_size INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    activity_log JSONB NOT NULL DEFAULT '[]'::jsonb,
    statement_start_date DATE,
    statement_end_date DATE,
    opening_balance NUMERIC(14, 2),
    closing_balance NUMERIC(14, 2),
    extracted_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS bank_account_id UUID REFERENCES bank_statement_accounts(id) ON DELETE CASCADE;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS filename TEXT NOT NULL DEFAULT '';
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS content_type TEXT;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS source_file BYTEA;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS source_file_size INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS activity_log JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS statement_start_date DATE;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS statement_end_date DATE;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS opening_balance NUMERIC(14, 2);
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS closing_balance NUMERIC(14, 2);
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS extracted_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS inserted_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS duplicate_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE bank_statement_uploads ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS bank_statement_uploads_account_idx
ON bank_statement_uploads (bank_account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS bank_statement_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bank_account_id UUID NOT NULL REFERENCES bank_statement_accounts(id) ON DELETE CASCADE,
    upload_id UUID REFERENCES bank_statement_uploads(id) ON DELETE SET NULL,
    transaction_date DATE NOT NULL,
    description TEXT NOT NULL,
    amount NUMERIC(14, 2) NOT NULL,
    balance NUMERIC(14, 2),
    transaction_type TEXT,
    source_hash TEXT NOT NULL,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    manual_amount NUMERIC(14, 2),
    manual_balance NUMERIC(14, 2),
    manual_override_note TEXT,
    manual_override_at TIMESTAMPTZ,
    manual_override_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    ai_category_code TEXT,
    ai_category_name TEXT,
    ai_category_tag TEXT,
    ai_category_confidence INTEGER NOT NULL DEFAULT 0,
    ai_category_reason TEXT,
    ai_category_source TEXT,
    ai_category_applied_at TIMESTAMPTZ,
    ai_category_applied_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (bank_account_id, source_hash)
);

ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS bank_account_id UUID REFERENCES bank_statement_accounts(id) ON DELETE CASCADE;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS upload_id UUID REFERENCES bank_statement_uploads(id) ON DELETE SET NULL;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS transaction_date DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS balance NUMERIC(14, 2);
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS transaction_type TEXT;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS source_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS raw JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS manual_amount NUMERIC(14, 2);
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS manual_balance NUMERIC(14, 2);
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS manual_override_note TEXT;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS manual_override_at TIMESTAMPTZ;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS manual_override_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS ai_category_code TEXT;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS ai_category_name TEXT;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS ai_category_tag TEXT;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS ai_category_confidence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS ai_category_reason TEXT;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS ai_category_source TEXT;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS ai_category_applied_at TIMESTAMPTZ;
ALTER TABLE bank_statement_transactions ADD COLUMN IF NOT EXISTS ai_category_applied_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS bank_statement_transactions_account_date_idx
ON bank_statement_transactions (bank_account_id, transaction_date, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS bank_statement_transactions_unique_hash_idx
ON bank_statement_transactions (bank_account_id, source_hash);

CREATE TABLE IF NOT EXISTS ignition_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    practice_id TEXT,
    practice_name TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    scope TEXT NOT NULL DEFAULT 'reporting',
    status TEXT NOT NULL DEFAULT 'connected',
    error_message TEXT,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_sync_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id)
);

ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS practice_id TEXT;
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS practice_name TEXT;
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS access_token TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS refresh_token TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'reporting';
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'connected';
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS last_sync_at TIMESTAMPTZ;
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_connections ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS ignition_connections_user_idx
ON ignition_connections (user_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS ignition_connections_user_unique_idx
ON ignition_connections (user_id);

CREATE TABLE IF NOT EXISTS ignition_sync_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    current_step TEXT,
    summary TEXT,
    error_message TEXT,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    datasets_synced JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS current_step TEXT;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS fetched_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS processed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS datasets_synced JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
ALTER TABLE ignition_sync_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ignition_sync_runs_user_status_idx
ON ignition_sync_runs (user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS ignition_reporting_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    practice_id TEXT,
    dataset TEXT NOT NULL,
    external_id TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_created_at TIMESTAMPTZ,
    source_updated_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, dataset, external_id)
);

ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS practice_id TEXT;
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS dataset TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS external_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS source_created_at TIMESTAMPTZ;
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_reporting_records ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS ignition_reporting_records_unique_idx
ON ignition_reporting_records (user_id, dataset, external_id);

CREATE INDEX IF NOT EXISTS ignition_reporting_records_dataset_idx
ON ignition_reporting_records (user_id, dataset, synced_at DESC);

CREATE TABLE IF NOT EXISTS ignition_view_cache (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cache_key TEXT NOT NULL,
    source_signature TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, cache_key)
);

ALTER TABLE ignition_view_cache ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ignition_view_cache ADD COLUMN IF NOT EXISTS cache_key TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_view_cache ADD COLUMN IF NOT EXISTS source_signature TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_view_cache ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ignition_view_cache ADD COLUMN IF NOT EXISTS generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_view_cache ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_view_cache ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS ignition_view_cache_user_key_idx
ON ignition_view_cache (user_id, cache_key);

CREATE INDEX IF NOT EXISTS ignition_view_cache_user_updated_idx
ON ignition_view_cache (user_id, updated_at DESC);

CREATE OR REPLACE VIEW ignition_reporting_clients AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'clients';

CREATE OR REPLACE VIEW ignition_reporting_contacts AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'contacts';

CREATE OR REPLACE VIEW ignition_reporting_services AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'services';

CREATE OR REPLACE VIEW ignition_reporting_proposals AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'proposals';

CREATE OR REPLACE VIEW ignition_reporting_invoices AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'invoices';

CREATE OR REPLACE VIEW ignition_reporting_payments AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'payments';

CREATE OR REPLACE VIEW ignition_reporting_collections AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'collections';

CREATE OR REPLACE VIEW ignition_reporting_deals AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'deals';

CREATE OR REPLACE VIEW ignition_reporting_deal_stages AS
SELECT * FROM ignition_reporting_records WHERE dataset = 'deal_stages';

CREATE TABLE IF NOT EXISTS ignition_renewal_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'draft',
    window_start DATE NOT NULL,
    window_end DATE NOT NULL,
    picked_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    batch_reference_number INTEGER,
    total_current_monthly NUMERIC(12, 2) NOT NULL DEFAULT 0,
    total_new_monthly NUMERIC(12, 2) NOT NULL DEFAULT 0,
    email_sent_at TIMESTAMPTZ,
    finalised_at TIMESTAMPTZ,
    zapier_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS window_start DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS window_end DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS picked_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS skipped_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS batch_reference_number INTEGER;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS total_current_monthly NUMERIC(12, 2) NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS total_new_monthly NUMERIC(12, 2) NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS email_sent_at TIMESTAMPTZ;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS finalised_at TIMESTAMPTZ;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS client_comms_completed_at TIMESTAMPTZ;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS client_comms_state JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS zapier_response JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS error_message TEXT;
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_renewal_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS ignition_renewal_runs_user_created_idx
ON ignition_renewal_runs (user_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ignition_renewal_runs_user_batch_reference_idx
ON ignition_renewal_runs (user_id, batch_reference_number)
WHERE batch_reference_number IS NOT NULL;

CREATE TABLE IF NOT EXISTS ignition_renewal_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES ignition_renewal_runs(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    proposal_external_id TEXT NOT NULL,
    proposal_name TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    client_manager TEXT NOT NULL DEFAULT '',
    service_name TEXT NOT NULL DEFAULT '',
    plan_name TEXT NOT NULL DEFAULT '',
    renewal_date DATE NOT NULL,
    current_monthly_fee NUMERIC(12, 2) NOT NULL DEFAULT 0,
    new_monthly_fee NUMERIC(12, 2) NOT NULL DEFAULT 0,
    variance NUMERIC(12, 2) NOT NULL DEFAULT 0,
    variance_percent NUMERIC(9, 4) NOT NULL DEFAULT 0,
    recommended_increase_percent NUMERIC(9, 4) NOT NULL DEFAULT 0,
    recommendation_reason TEXT NOT NULL DEFAULT '',
    recommendation_engine TEXT NOT NULL DEFAULT 'rule',
    recommendation_history_sample_size INTEGER NOT NULL DEFAULT 0,
    recommendation_context JSONB NOT NULL DEFAULT '{}'::jsonb,
    comments TEXT NOT NULL DEFAULT '',
    proposal_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    zapier_sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, proposal_external_id)
);

ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES ignition_renewal_runs(id) ON DELETE CASCADE;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS proposal_external_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS proposal_name TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS client_manager TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS service_name TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS plan_name TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS renewal_date DATE NOT NULL DEFAULT CURRENT_DATE;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS current_monthly_fee NUMERIC(12, 2) NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS new_monthly_fee NUMERIC(12, 2) NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS variance NUMERIC(12, 2) NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS variance_percent NUMERIC(9, 4) NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS recommended_increase_percent NUMERIC(9, 4) NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS recommendation_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS recommendation_engine TEXT NOT NULL DEFAULT 'rule';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS recommendation_history_sample_size INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS recommendation_context JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS comments TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS proposal_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS zapier_sent_at TIMESTAMPTZ;
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_renewal_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS ignition_renewal_items_user_proposal_idx
ON ignition_renewal_items (user_id, proposal_external_id);

CREATE INDEX IF NOT EXISTS ignition_renewal_items_run_idx
ON ignition_renewal_items (run_id, renewal_date ASC);

CREATE TABLE IF NOT EXISTS ignition_renewal_ineligible_proposals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    proposal_external_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'user-marked-ineligible',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, proposal_external_id)
);

ALTER TABLE ignition_renewal_ineligible_proposals ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ignition_renewal_ineligible_proposals ADD COLUMN IF NOT EXISTS proposal_external_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_ineligible_proposals ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT 'user-marked-ineligible';
ALTER TABLE ignition_renewal_ineligible_proposals ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_renewal_ineligible_proposals ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS ignition_renewal_ineligible_user_proposal_idx
ON ignition_renewal_ineligible_proposals (user_id, proposal_external_id);

CREATE INDEX IF NOT EXISTS ignition_renewal_ineligible_user_created_idx
ON ignition_renewal_ineligible_proposals (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ignition_renewal_price_recommendations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_key TEXT NOT NULL,
    plan_key TEXT NOT NULL,
    history_hash TEXT NOT NULL,
    recommended_increase_percent NUMERIC(9, 4) NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT 'rule',
    history_sample_size INTEGER NOT NULL DEFAULT 0,
    context_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, client_key, plan_key, history_hash)
);

ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS client_key TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS plan_key TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS history_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS recommended_increase_percent NUMERIC(9, 4) NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT '';
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS engine TEXT NOT NULL DEFAULT 'rule';
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS history_sample_size INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS context_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ignition_renewal_price_recommendations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS ignition_renewal_price_recommendations_unique_idx
ON ignition_renewal_price_recommendations (user_id, client_key, plan_key, history_hash);

CREATE INDEX IF NOT EXISTS ignition_renewal_price_recommendations_user_updated_idx
ON ignition_renewal_price_recommendations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS me_report_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    client_name TEXT NOT NULL DEFAULT '',
    internal_client_owner TEXT NOT NULL DEFAULT '',
    bookkeeping_frequency TEXT NOT NULL DEFAULT 'Monthly',
    report_recipient_email TEXT NOT NULL DEFAULT '',
    year_end_month INTEGER NOT NULL DEFAULT 3,
    brought_forward_trading_loss NUMERIC(14, 2) NOT NULL DEFAULT 0,
    brought_forward_trading_loss_updated_at TIMESTAMPTZ,
    xero_contact_id TEXT,
    xero_contact_name TEXT NOT NULL DEFAULT '',
    xero_contact_email TEXT NOT NULL DEFAULT '',
    xero_connection_id UUID REFERENCES xero_connections(id) ON DELETE SET NULL,
    xero_tenant_id TEXT,
    xero_tenant_name TEXT,
    xero_connection_status TEXT NOT NULL DEFAULT 'not_connected',
    vat_registered_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    vat_registered_confirmed_at TIMESTAMPTZ,
    dismissed_warning_keys JSONB NOT NULL DEFAULT '[]'::jsonb,
    tax_adjustment_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    transfer_classification_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
    director_loan_account_overrides JSONB NOT NULL DEFAULT '{"include":[],"exclude":[]}'::jsonb,
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TIMESTAMPTZ,
    last_calculated_at TIMESTAMPTZ,
    last_report_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS xero_contact_id TEXT;
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS xero_contact_name TEXT NOT NULL DEFAULT '';
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS xero_contact_email TEXT NOT NULL DEFAULT '';
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS brought_forward_trading_loss NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS brought_forward_trading_loss_updated_at TIMESTAMPTZ;
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS vat_registered_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS vat_registered_confirmed_at TIMESTAMPTZ;
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS dismissed_warning_keys JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS tax_adjustment_overrides JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS transfer_classification_overrides JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE me_report_clients ADD COLUMN IF NOT EXISTS director_loan_account_overrides JSONB NOT NULL DEFAULT '{"include":[],"exclude":[]}'::jsonb;

CREATE INDEX IF NOT EXISTS me_report_clients_user_status_idx
ON me_report_clients (user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS me_report_clients_xero_contact_idx
ON me_report_clients (xero_contact_id);

CREATE TABLE IF NOT EXISTS me_report_account_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES me_report_clients(id) ON DELETE CASCADE,
    xero_account_id TEXT,
    account_code TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL DEFAULT '',
    suggested_treatment TEXT NOT NULL DEFAULT '',
    tax_treatment TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    confidence INTEGER NOT NULL DEFAULT 0,
    review_required BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'suggested',
    note TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS me_report_account_mappings_unique_idx
ON me_report_account_mappings (client_id, account_code);

CREATE INDEX IF NOT EXISTS me_report_account_mappings_client_status_idx
ON me_report_account_mappings (client_id, status, confidence);

CREATE TABLE IF NOT EXISTS me_report_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES me_report_clients(id) ON DELETE CASCADE,
    period_start DATE NOT NULL DEFAULT CURRENT_DATE,
    period_end DATE NOT NULL DEFAULT CURRENT_DATE,
    status TEXT NOT NULL DEFAULT 'calculated',
    traffic_light TEXT NOT NULL DEFAULT 'amber',
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS me_report_reviews_client_period_idx
ON me_report_reviews (client_id, period_end DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS me_report_exceptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES me_report_clients(id) ON DELETE CASCADE,
    review_id UUID REFERENCES me_report_reviews(id) ON DELETE CASCADE,
    severity TEXT NOT NULL DEFAULT 'amber',
    title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    suggested_action TEXT NOT NULL DEFAULT '',
    action_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE me_report_exceptions ADD COLUMN IF NOT EXISTS action_payload JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS me_report_exceptions_client_status_idx
ON me_report_exceptions (client_id, status, severity, created_at DESC);

CREATE TABLE IF NOT EXISTS me_report_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES me_report_clients(id) ON DELETE CASCADE,
    review_id UUID REFERENCES me_report_reviews(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    recipient_email TEXT NOT NULL DEFAULT '',
    email_subject TEXT NOT NULL DEFAULT '',
    email_body TEXT NOT NULL DEFAULT '',
    bcc_email TEXT NOT NULL DEFAULT '',
    report_html TEXT NOT NULL DEFAULT '',
    commentary TEXT NOT NULL DEFAULT '',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    sent_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    xero_history_note_status TEXT NOT NULL DEFAULT 'not_sent',
    xero_history_note_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ
);

ALTER TABLE me_report_reports ADD COLUMN IF NOT EXISTS email_subject TEXT NOT NULL DEFAULT '';
ALTER TABLE me_report_reports ADD COLUMN IF NOT EXISTS email_body TEXT NOT NULL DEFAULT '';
ALTER TABLE me_report_reports ADD COLUMN IF NOT EXISTS bcc_email TEXT NOT NULL DEFAULT '';
ALTER TABLE me_report_reports ADD COLUMN IF NOT EXISTS sent_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE me_report_reports ADD COLUMN IF NOT EXISTS xero_history_note_status TEXT NOT NULL DEFAULT 'not_sent';
ALTER TABLE me_report_reports ADD COLUMN IF NOT EXISTS xero_history_note_error TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS me_report_reports_client_idx
ON me_report_reports (client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS me_report_settings (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    email_provider TEXT NOT NULL DEFAULT 'smtp',
    email_subject_template TEXT NOT NULL DEFAULT 'Month-end bookkeeping snapshot for {{client_name}}',
    email_body_template TEXT NOT NULL DEFAULT '',
    bcc_email TEXT NOT NULL DEFAULT 'fmfhdkgaptpyubgms@accountancymanager.co.uk',
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE me_report_settings ADD COLUMN IF NOT EXISTS email_provider TEXT NOT NULL DEFAULT 'smtp';
ALTER TABLE me_report_settings ADD COLUMN IF NOT EXISTS email_subject_template TEXT NOT NULL DEFAULT 'Month-end bookkeeping snapshot for {{client_name}}';
ALTER TABLE me_report_settings ADD COLUMN IF NOT EXISTS email_body_template TEXT NOT NULL DEFAULT '';
ALTER TABLE me_report_settings ADD COLUMN IF NOT EXISTS bcc_email TEXT NOT NULL DEFAULT 'fmfhdkgaptpyubgms@accountancymanager.co.uk';
ALTER TABLE me_report_settings ADD COLUMN IF NOT EXISTS updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE me_report_settings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE me_report_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS gmail_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    gmail_email TEXT NOT NULL DEFAULT '',
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    token_expires_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'connected',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE gmail_connections ADD COLUMN IF NOT EXISTS gmail_email TEXT NOT NULL DEFAULT '';
ALTER TABLE gmail_connections ADD COLUMN IF NOT EXISTS access_token TEXT NOT NULL DEFAULT '';
ALTER TABLE gmail_connections ADD COLUMN IF NOT EXISTS refresh_token TEXT NOT NULL DEFAULT '';
ALTER TABLE gmail_connections ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT '';
ALTER TABLE gmail_connections ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMPTZ;
ALTER TABLE gmail_connections ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'connected';
ALTER TABLE gmail_connections ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE gmail_connections ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS me_report_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES me_report_clients(id) ON DELETE CASCADE,
    filename TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'processing',
    error_message TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    extracted_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    estimated_corporation_tax NUMERIC NOT NULL DEFAULT 0,
    dividend_capacity NUMERIC NOT NULL DEFAULT 0,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS me_report_submissions_client_idx
ON me_report_submissions (client_id, created_at DESC);

CREATE TABLE IF NOT EXISTS me_report_sync_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL REFERENCES me_report_clients(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    current_step TEXT NOT NULL DEFAULT 'Queued',
    summary TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    progress INTEGER NOT NULL DEFAULT 0,
    records_synced INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS me_report_sync_runs_client_status_idx
ON me_report_sync_runs (client_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS audit_events_entity_idx
ON audit_events (entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_created_idx
ON audit_events (created_at DESC);
CREATE INDEX IF NOT EXISTS audit_events_user_created_idx
ON audit_events (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS usage_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    tenant_id TEXT NOT NULL DEFAULT '',
    feature TEXT NOT NULL DEFAULT '',
    page TEXT NOT NULL DEFAULT '',
    operation TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    request_units INTEGER NOT NULL DEFAULT 1,
    request_bytes BIGINT NOT NULL DEFAULT 0,
    response_bytes BIGINT NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd NUMERIC(14, 6) NOT NULL DEFAULT 0,
    status_code INTEGER,
    success BOOLEAN NOT NULL DEFAULT FALSE,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS feature TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS page TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS operation TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS endpoint TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS request_units INTEGER NOT NULL DEFAULT 1;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS request_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS response_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS input_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS output_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS total_tokens INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS estimated_cost_usd NUMERIC(14, 6) NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS status_code INTEGER;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS success BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS error_code TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS error_message TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS duration_ms INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS usage_events_created_idx
ON usage_events (created_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_provider_created_idx
ON usage_events (provider, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_user_created_idx
ON usage_events (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_events_provider_feature_idx
ON usage_events (provider, feature, created_at DESC);

CREATE TABLE IF NOT EXISTS ch_settings (
    singleton_id INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
    environment TEXT NOT NULL DEFAULT 'sandbox',
    api_key_encrypted TEXT NOT NULL DEFAULT '',
    api_key_hint TEXT NOT NULL DEFAULT '',
    presenter_id TEXT NOT NULL DEFAULT '',
    presenter_auth_encrypted TEXT NOT NULL DEFAULT '',
    presenter_auth_hint TEXT NOT NULL DEFAULT '',
    credit_account_number TEXT NOT NULL DEFAULT '',
    xero_invoice_account_code TEXT NOT NULL DEFAULT '',
    xero_invoice_item_code TEXT NOT NULL DEFAULT '',
    xero_invoice_description TEXT NOT NULL DEFAULT 'Companies House confirmation statement filing',
    xero_invoice_unit_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    xero_invoice_tax_type TEXT NOT NULL DEFAULT 'NONE',
    notify_email TEXT NOT NULL DEFAULT '',
    auto_sync_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ch_companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_number TEXT NOT NULL UNIQUE,
    company_name TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    client_address TEXT NOT NULL DEFAULT '',
    assigned_staff_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    assigned_staff_name TEXT NOT NULL DEFAULT '',
    registered_office TEXT NOT NULL DEFAULT '',
    company_status TEXT NOT NULL DEFAULT '',
    incorporation_date DATE,
    sic_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    officers JSONB NOT NULL DEFAULT '[]'::jsonb,
    pscs JSONB NOT NULL DEFAULT '[]'::jsonb,
    share_capital JSONB NOT NULL DEFAULT '{}'::jsonb,
    next_made_up_to_date DATE,
    next_due_date DATE,
    last_filed_date DATE,
    filing_history JSONB NOT NULL DEFAULT '[]'::jsonb,
    workflow_review JSONB NOT NULL DEFAULT '{}'::jsonb,
    internal_status TEXT NOT NULL DEFAULT 'active',
    filing_authority_status TEXT NOT NULL DEFAULT 'authorised',
    filing_authority_reference TEXT NOT NULL DEFAULT '',
    filing_authority_received_at TIMESTAMPTZ,
    filing_authority_expires_at TIMESTAMPTZ,
    notes TEXT NOT NULL DEFAULT '',
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ch_companies_client_idx ON ch_companies (client_id);
CREATE INDEX IF NOT EXISTS ch_companies_due_idx ON ch_companies (next_due_date);
CREATE INDEX IF NOT EXISTS ch_companies_status_idx ON ch_companies (internal_status);

ALTER TABLE ch_companies ADD COLUMN IF NOT EXISTS filing_authority_status TEXT NOT NULL DEFAULT 'authorised';
ALTER TABLE ch_companies ALTER COLUMN filing_authority_status SET DEFAULT 'authorised';
ALTER TABLE ch_companies ADD COLUMN IF NOT EXISTS filing_authority_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_companies ADD COLUMN IF NOT EXISTS filing_authority_received_at TIMESTAMPTZ;
ALTER TABLE ch_companies ADD COLUMN IF NOT EXISTS filing_authority_expires_at TIMESTAMPTZ;
ALTER TABLE ch_companies ADD COLUMN IF NOT EXISTS workflow_review JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_companies ADD COLUMN IF NOT EXISTS client_address TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS ch_companies_filing_authority_idx ON ch_companies (filing_authority_status, filing_authority_expires_at);

CREATE TABLE IF NOT EXISTS ch_auth_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL UNIQUE REFERENCES ch_companies(id) ON DELETE CASCADE,
    code_encrypted TEXT NOT NULL,
    code_hint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    uploaded_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE ch_auth_codes ADD COLUMN IF NOT EXISTS code_hint TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_codes ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE ch_auth_codes ADD COLUMN IF NOT EXISTS uploaded_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE ch_auth_codes ADD COLUMN IF NOT EXISTS uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ch_auth_codes ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
ALTER TABLE ch_auth_codes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
CREATE UNIQUE INDEX IF NOT EXISTS ch_auth_codes_company_unique_idx ON ch_auth_codes (company_id);

CREATE TABLE IF NOT EXISTS ch_auth_code_register (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_number TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    client_manager TEXT NOT NULL DEFAULT '',
    normalised_name TEXT NOT NULL DEFAULT '',
    code_encrypted TEXT NOT NULL,
    code_hint TEXT NOT NULL DEFAULT '',
    source_filename TEXT NOT NULL DEFAULT '',
    uploaded_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ch_auth_code_register_company_number_idx
ON ch_auth_code_register (company_number);

ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS client_manager TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS client_type TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS ch_auth_code_register_name_idx
ON ch_auth_code_register (normalised_name);

CREATE TABLE IF NOT EXISTS ch_bm_tasks_state (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    filename TEXT NOT NULL DEFAULT '',
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    rows JSONB NOT NULL DEFAULT '[]'::jsonb,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE ch_bm_tasks_state ADD COLUMN IF NOT EXISTS filename TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_bm_tasks_state ADD COLUMN IF NOT EXISTS summary JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_bm_tasks_state ADD COLUMN IF NOT EXISTS rows JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE ch_bm_tasks_state ADD COLUMN IF NOT EXISTS uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ch_bm_tasks_state ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS ch_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES ch_companies(id) ON DELETE CASCADE,
    made_up_to_date DATE,
    snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_results JSONB NOT NULL DEFAULT '{}'::jsonb,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'draft',
    prepared_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    prepared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ch_drafts_company_idx ON ch_drafts (company_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ch_secretarial_filings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID REFERENCES ch_companies(id) ON DELETE SET NULL,
    company_number TEXT NOT NULL DEFAULT '',
    company_name TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    filing_type TEXT NOT NULL DEFAULT '',
    filing_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'DRAFT',
    risk TEXT NOT NULL DEFAULT 'medium',
    mode TEXT NOT NULL DEFAULT 'manual',
    due_date DATE,
    effective_date DATE,
    client_approval_required BOOLEAN NOT NULL DEFAULT FALSE,
    client_approval_status TEXT NOT NULL DEFAULT 'not_required',
    internal_approval_required BOOLEAN NOT NULL DEFAULT FALSE,
    internal_approval_status TEXT NOT NULL DEFAULT 'not_required',
    evidence_attached BOOLEAN NOT NULL DEFAULT FALSE,
    submitted_at TIMESTAMPTZ,
    companies_house_status TEXT NOT NULL DEFAULT 'Not submitted',
    companies_house_ref TEXT NOT NULL DEFAULT '',
    fee_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    assignee TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    client_email TEXT NOT NULL DEFAULT '',
    client_phone TEXT NOT NULL DEFAULT '',
    client_address TEXT NOT NULL DEFAULT '',
    auth_code_hint TEXT NOT NULL DEFAULT '',
    source_filename TEXT NOT NULL DEFAULT '',
    uploaded_at TIMESTAMPTZ,
    form_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    prepared_submission JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_messages JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ch_secretarial_filings_company_number_idx
ON ch_secretarial_filings (company_number);
CREATE INDEX IF NOT EXISTS ch_secretarial_filings_status_due_idx
ON ch_secretarial_filings (status, due_date);
CREATE INDEX IF NOT EXISTS ch_secretarial_filings_updated_idx
ON ch_secretarial_filings (updated_at DESC);

CREATE TABLE IF NOT EXISTS ch_submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES ch_companies(id) ON DELETE CASCADE,
    draft_id UUID REFERENCES ch_drafts(id) ON DELETE SET NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    attempt_type TEXT NOT NULL DEFAULT 'submit',
    submission_reference TEXT NOT NULL DEFAULT '',
    transaction_id TEXT NOT NULL DEFAULT '',
    fee_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    payment_reference TEXT NOT NULL DEFAULT '',
    payment_confirmed BOOLEAN,
    payment_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    retry_count INTEGER NOT NULL DEFAULT 0,
    dead_letter BOOLEAN NOT NULL DEFAULT FALSE,
    dead_letter_reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'submitted',
    rejection_reason TEXT NOT NULL DEFAULT '',
    response_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    xero_invoice_id TEXT NOT NULL DEFAULT '',
    submitted_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ch_submissions_company_idx ON ch_submissions (company_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS ch_submissions_status_idx ON ch_submissions (status, submitted_at DESC);

ALTER TABLE ch_submissions ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_submissions ADD COLUMN IF NOT EXISTS attempt_type TEXT NOT NULL DEFAULT 'submit';
ALTER TABLE ch_submissions ADD COLUMN IF NOT EXISTS payment_confirmed BOOLEAN;
ALTER TABLE ch_submissions ADD COLUMN IF NOT EXISTS payment_evidence JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_submissions ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ch_submissions ADD COLUMN IF NOT EXISTS dead_letter BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE ch_submissions ADD COLUMN IF NOT EXISTS dead_letter_reason TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS ch_submissions_idempotency_idx
ON ch_submissions (idempotency_key)
WHERE idempotency_key <> '';
CREATE UNIQUE INDEX IF NOT EXISTS ch_submissions_submission_reference_unique_idx
ON ch_submissions (submission_reference)
WHERE submission_reference <> '';

CREATE TABLE IF NOT EXISTS ch_dead_letters (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submission_id UUID REFERENCES ch_submissions(id) ON DELETE SET NULL,
    company_id UUID REFERENCES ch_companies(id) ON DELETE SET NULL,
    workflow TEXT NOT NULL DEFAULT 'confirmation_statement_bulk',
    stage TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ch_dead_letters_created_idx ON ch_dead_letters (created_at DESC);

CREATE TABLE IF NOT EXISTS ch_bulk_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    job_type TEXT NOT NULL DEFAULT 'confirmation_statement_bulk',
    status TEXT NOT NULL DEFAULT 'queued',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ch_bulk_jobs_created_idx ON ch_bulk_jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS ch_bulk_jobs_user_idx ON ch_bulk_jobs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ch_bulk_jobs_status_idx ON ch_bulk_jobs (status, created_at DESC);

CREATE TABLE IF NOT EXISTS ch_imports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_type TEXT NOT NULL DEFAULT 'clients',
    filename TEXT NOT NULL DEFAULT '',
    total_rows INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'completed',
    uploaded_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ch_imports_created_idx ON ch_imports (created_at DESC);

CREATE TABLE IF NOT EXISTS hmrc_64_8_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    client_id TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    client_manager TEXT NOT NULL DEFAULT '',
    client_contact_name TEXT NOT NULL DEFAULT '',
    client_contact_email TEXT NOT NULL DEFAULT '',
    client_contact_phone TEXT NOT NULL DEFAULT '',
    postal_address TEXT NOT NULL DEFAULT '',
    sa_utr TEXT NOT NULL DEFAULT '',
    ct_utr TEXT NOT NULL DEFAULT '',
    paye_reference TEXT NOT NULL DEFAULT '',
    company_number TEXT NOT NULL DEFAULT '',
    include_sa BOOLEAN NOT NULL DEFAULT FALSE,
    include_paye BOOLEAN NOT NULL DEFAULT FALSE,
    include_ct BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'draft',
    submission_channel TEXT NOT NULL DEFAULT 'online',
    hmrc_submission_reference TEXT NOT NULL DEFAULT '',
    submitted_at TIMESTAMPTZ,
    expected_code_by DATE,
    reminder_count INTEGER NOT NULL DEFAULT 0,
    last_reminder_at TIMESTAMPTZ,
    authority_code TEXT NOT NULL DEFAULT '',
    authority_code_received_at TIMESTAMPTZ,
    authority_activated_at TIMESTAMPTZ,
    notes TEXT NOT NULL DEFAULT '',
    evidence_links JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS client_manager TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS client_contact_name TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS client_contact_email TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS client_contact_phone TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS postal_address TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS sa_utr TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS ct_utr TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS paye_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS company_number TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_sa BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_paye BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_ct BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS submission_channel TEXT NOT NULL DEFAULT 'online';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS hmrc_submission_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS expected_code_by DATE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS reminder_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS last_reminder_at TIMESTAMPTZ;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS authority_code TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS authority_code_received_at TIMESTAMPTZ;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS authority_activated_at TIMESTAMPTZ;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS evidence_links JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS hmrc_64_8_requests_user_idx
ON hmrc_64_8_requests (created_by_user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS hmrc_64_8_requests_status_idx
ON hmrc_64_8_requests (status, submitted_at DESC);

CREATE TABLE IF NOT EXISTS release_updates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    details JSONB NOT NULL DEFAULT '[]'::jsonb,
    deployment_id TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';
ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT '';
ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS deployment_id TEXT NOT NULL DEFAULT '';
ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS commit_sha TEXT NOT NULL DEFAULT '';
ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE release_updates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS release_updates_deployment_id_unique_idx
ON release_updates (deployment_id)
WHERE deployment_id <> '';

CREATE INDEX IF NOT EXISTS release_updates_created_idx
ON release_updates (created_at DESC);

CREATE TABLE IF NOT EXISTS release_ideas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idea_text TEXT NOT NULL DEFAULT '',
    context TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    submitted_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE release_ideas ADD COLUMN IF NOT EXISTS idea_text TEXT NOT NULL DEFAULT '';
ALTER TABLE release_ideas ADD COLUMN IF NOT EXISTS context TEXT NOT NULL DEFAULT '';
ALTER TABLE release_ideas ADD COLUMN IF NOT EXISTS contact_name TEXT NOT NULL DEFAULT '';
ALTER TABLE release_ideas ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new';
ALTER TABLE release_ideas ADD COLUMN IF NOT EXISTS submitted_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE release_ideas ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE release_ideas ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS release_ideas_status_created_idx
ON release_ideas (status, created_at DESC);
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
