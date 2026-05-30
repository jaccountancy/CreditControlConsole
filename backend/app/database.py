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
    provider TEXT NOT NULL DEFAULT 'xero',
    code_verifier TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'xero';
ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS code_verifier TEXT;

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

CREATE TABLE IF NOT EXISTS bank_statement_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_client_id UUID NOT NULL REFERENCES bank_statement_clients(id) ON DELETE CASCADE,
    account_name TEXT NOT NULL,
    account_number TEXT NOT NULL,
    sort_code TEXT,
    currency_code TEXT NOT NULL DEFAULT 'GBP',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS extraction_client_id UUID REFERENCES bank_statement_clients(id) ON DELETE CASCADE;
ALTER TABLE bank_statement_accounts ADD COLUMN IF NOT EXISTS account_name TEXT NOT NULL DEFAULT 'Bank account';
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

CREATE TABLE IF NOT EXISTS me_report_clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    client_name TEXT NOT NULL DEFAULT '',
    internal_client_owner TEXT NOT NULL DEFAULT '',
    bookkeeping_frequency TEXT NOT NULL DEFAULT 'Monthly',
    report_recipient_email TEXT NOT NULL DEFAULT '',
    year_end_month INTEGER NOT NULL DEFAULT 3,
    xero_connection_id UUID REFERENCES xero_connections(id) ON DELETE SET NULL,
    xero_tenant_id TEXT,
    xero_tenant_name TEXT,
    xero_connection_status TEXT NOT NULL DEFAULT 'not_connected',
    status TEXT NOT NULL DEFAULT 'active',
    last_sync_at TIMESTAMPTZ,
    last_calculated_at TIMESTAMPTZ,
    last_report_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS me_report_clients_user_status_idx
ON me_report_clients (user_id, status, created_at DESC);

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
    report_html TEXT NOT NULL DEFAULT '',
    commentary TEXT NOT NULL DEFAULT '',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    sent_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS me_report_reports_client_idx
ON me_report_reports (client_id, created_at DESC);

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
