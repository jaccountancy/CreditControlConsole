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
    role TEXT NOT NULL DEFAULT 'admin',
    status TEXT NOT NULL DEFAULT 'active',
    auth_method TEXT NOT NULL DEFAULT 'xero_only',
    two_factor_method TEXT NOT NULL DEFAULT 'none',
    is_super_admin BOOLEAN NOT NULL DEFAULT FALSE,
    notes TEXT NOT NULL DEFAULT '',
    xero_user_id TEXT,
    auth_app_enrolled_at TIMESTAMPTZ,
    auth_app_last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    last_approved_login_at TIMESTAMPTZ
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'admin';
ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_method TEXT NOT NULL DEFAULT 'xero_only';
ALTER TABLE users ADD COLUMN IF NOT EXISTS two_factor_method TEXT NOT NULL DEFAULT 'none';
ALTER TABLE users ALTER COLUMN two_factor_method SET DEFAULT 'none';
UPDATE users
SET two_factor_method = 'none'
WHERE lower(COALESCE(two_factor_method, '')) IN ('', 'jenius_auth_app', 'jenius_app', 'app');
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_super_admin BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS xero_user_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_app_enrolled_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_app_last_seen_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_approved_login_at TIMESTAMPTZ;

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

CREATE TABLE IF NOT EXISTS esign_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    client_id TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    document_type TEXT NOT NULL DEFAULT '',
    document_title TEXT NOT NULL DEFAULT '',
    recipient_name TEXT NOT NULL DEFAULT '',
    recipient_email TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'sent',
    due_date DATE,
    sent_at TIMESTAMPTZ,
    viewed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    external_provider TEXT NOT NULL DEFAULT 'foxit_esign',
    external_request_id TEXT NOT NULL DEFAULT '',
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS document_type TEXT NOT NULL DEFAULT '';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS document_title TEXT NOT NULL DEFAULT '';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS recipient_name TEXT NOT NULL DEFAULT '';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS recipient_email TEXT NOT NULL DEFAULT '';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS message TEXT NOT NULL DEFAULT '';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'sent';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS due_date DATE;
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS viewed_at TIMESTAMPTZ;
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS external_provider TEXT NOT NULL DEFAULT 'foxit_esign';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS external_request_id TEXT NOT NULL DEFAULT '';
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE esign_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS esign_requests_status_created_idx
ON esign_requests (status, created_at DESC);

CREATE INDEX IF NOT EXISTS esign_requests_recipient_email_idx
ON esign_requests (recipient_email);

CREATE TABLE IF NOT EXISTS code_breaker_ch_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_number TEXT NOT NULL,
    as_at_date DATE NOT NULL,
    document_url TEXT NOT NULL DEFAULT '',
    document_content_type TEXT NOT NULL DEFAULT '',
    document_hash TEXT NOT NULL DEFAULT '',
    document_size INTEGER NOT NULL DEFAULT 0,
    document_bytes BYTEA,
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    extracted_net_assets NUMERIC(14, 2),
    extraction_engine TEXT NOT NULL DEFAULT '',
    extraction_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    activity_log JSONB NOT NULL DEFAULT '[]'::jsonb,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (company_number, as_at_date)
);

ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS document_url TEXT NOT NULL DEFAULT '';
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS document_content_type TEXT NOT NULL DEFAULT '';
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS document_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS document_size INTEGER NOT NULL DEFAULT 0;
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS document_bytes BYTEA;
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT '';
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT '';
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS extracted_net_assets NUMERIC(14, 2);
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS extraction_engine TEXT NOT NULL DEFAULT '';
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS extraction_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS activity_log JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE code_breaker_ch_documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS code_breaker_ch_documents_company_period_uidx
ON code_breaker_ch_documents (company_number, as_at_date);

CREATE INDEX IF NOT EXISTS code_breaker_ch_documents_company_updated_idx
ON code_breaker_ch_documents (company_number, updated_at DESC);

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
    pi_clearing_account_code TEXT NOT NULL DEFAULT 'PI Clearing Account',
    pi_clearing_account_locked BOOLEAN NOT NULL DEFAULT FALSE,
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS late_payment_charge_account_code TEXT NOT NULL DEFAULT '1222';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS late_payment_charge_account_name TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS late_payment_charge_tax_type TEXT NOT NULL DEFAULT 'OUTPUT2';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS bad_debt_write_off_account_code TEXT NOT NULL DEFAULT '402';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS bad_debt_write_off_account_name TEXT NOT NULL DEFAULT '';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS pi_clearing_account_code TEXT NOT NULL DEFAULT 'PI Clearing Account';
ALTER TABLE xero_posting_settings ADD COLUMN IF NOT EXISTS pi_clearing_account_locked BOOLEAN NOT NULL DEFAULT FALSE;
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

CREATE TABLE IF NOT EXISTS login_approval_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    approval_token TEXT NOT NULL UNIQUE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_from TEXT NOT NULL DEFAULT '',
    requested_ip TEXT NOT NULL DEFAULT '',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    approved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ,
    denied_at TIMESTAMPTZ,
    last_polled_at TIMESTAMPTZ
);

ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS approval_token TEXT;
ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS requested_from TEXT NOT NULL DEFAULT '';
ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS requested_ip TEXT NOT NULL DEFAULT '';
ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS approved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS denied_at TIMESTAMPTZ;
ALTER TABLE login_approval_attempts ADD COLUMN IF NOT EXISTS last_polled_at TIMESTAMPTZ;

UPDATE login_approval_attempts
SET approval_token = encode(gen_random_bytes(32), 'hex')
WHERE approval_token IS NULL OR approval_token = '';

UPDATE login_approval_attempts
SET expires_at = COALESCE(expires_at, requested_at + interval '60 seconds');

ALTER TABLE login_approval_attempts
    ALTER COLUMN approval_token SET NOT NULL;

ALTER TABLE login_approval_attempts
    ALTER COLUMN expires_at SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS login_approval_attempts_approval_token_uidx
ON login_approval_attempts (approval_token);

CREATE INDEX IF NOT EXISTS login_approval_attempts_user_requested_idx
ON login_approval_attempts (user_id, requested_at DESC);

CREATE INDEX IF NOT EXISTS login_approval_attempts_status_expires_idx
ON login_approval_attempts (status, expires_at);

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
ALTER TABLE customers ADD COLUMN IF NOT EXISTS client_profile JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS company_structure JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS company_structure_synced_at TIMESTAMPTZ;

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
    panel_category TEXT NOT NULL DEFAULT 'outstanding',
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
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS panel_category TEXT NOT NULL DEFAULT 'outstanding';
CREATE INDEX IF NOT EXISTS invoices_panel_category_idx ON invoices (panel_category);

CREATE OR REPLACE FUNCTION derive_invoice_panel_category(
    _control_status TEXT,
    _status TEXT,
    _amount_due NUMERIC,
    _due_date DATE
) RETURNS TEXT
LANGUAGE plpgsql
AS $$
DECLARE
    control_text TEXT := lower(COALESCE(_control_status, _status, ''));
BEGIN
    IF COALESCE(_amount_due, 0) <= 0 OR control_text LIKE '%paid%' THEN
        RETURN 'paid';
    END IF;
    IF control_text LIKE '%bad debt%' OR control_text LIKE '%bad-debt%' OR control_text LIKE '%bad_debt%' THEN
        RETURN 'bad-debt';
    END IF;
    IF control_text LIKE '%court%' OR control_text LIKE '%legal%' THEN
        RETURN 'court';
    END IF;
    IF control_text LIKE '%query%' OR control_text LIKE '%queried%' OR control_text LIKE '%dispute%' OR control_text LIKE '%disputed%' THEN
        RETURN 'query';
    END IF;
    IF COALESCE(_amount_due, 0) > 0 AND _due_date IS NOT NULL AND _due_date < CURRENT_DATE THEN
        RETURN 'overdue';
    END IF;
    RETURN 'outstanding';
END;
$$;

CREATE OR REPLACE FUNCTION invoices_panel_category_before_write()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.panel_category := derive_invoice_panel_category(
        NEW.control_status,
        NEW.status,
        NEW.amount_due,
        COALESCE(NEW.due_date, NEW.invoice_date)
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS invoices_panel_category_before_write ON invoices;
CREATE TRIGGER invoices_panel_category_before_write
BEFORE INSERT OR UPDATE OF control_status, status, amount_due, due_date, invoice_date
ON invoices
FOR EACH ROW
EXECUTE FUNCTION invoices_panel_category_before_write();

UPDATE invoices
SET panel_category = derive_invoice_panel_category(
    control_status,
    status,
    amount_due,
    COALESCE(due_date, invoice_date)
)
WHERE panel_category IS NULL
   OR panel_category = '';

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

CREATE TABLE IF NOT EXISTS barclays_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'connected',
    access_token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    token_type TEXT NOT NULL DEFAULT 'Bearer',
    scope TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ,
    consent_id TEXT,
    consent_status TEXT,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id)
);

ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'connected';
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS access_token TEXT NOT NULL DEFAULT '';
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS refresh_token TEXT NOT NULL DEFAULT '';
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS token_type TEXT NOT NULL DEFAULT 'Bearer';
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT '';
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS consent_id TEXT;
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS consent_status TEXT;
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE barclays_connections ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS barclays_connections_user_idx
ON barclays_connections (user_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS barclays_connections_user_unique_idx
ON barclays_connections (user_id);

CREATE TABLE IF NOT EXISTS supplier_payment_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    run_reference TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    method TEXT NOT NULL DEFAULT 'xero',
    tenant_id TEXT NOT NULL DEFAULT '',
    tenant_name TEXT NOT NULL DEFAULT '',
    payment_date DATE,
    xero_account_id TEXT NOT NULL DEFAULT '',
    reference_text TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    total_items INTEGER NOT NULL DEFAULT 0,
    total_amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    paid_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    UNIQUE (user_id, run_reference)
);

ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS run_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS method TEXT NOT NULL DEFAULT 'xero';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS tenant_name TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS payment_date DATE;
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS xero_account_id TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS reference_text TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS total_items INTEGER NOT NULL DEFAULT 0;
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS total_amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS paid_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE supplier_payment_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS supplier_payment_runs_user_idx
ON supplier_payment_runs (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS supplier_payment_run_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES supplier_payment_runs(id) ON DELETE CASCADE,
    invoice_id TEXT NOT NULL DEFAULT '',
    invoice_number TEXT NOT NULL DEFAULT '',
    supplier_name TEXT NOT NULL DEFAULT '',
    supplier_email TEXT NOT NULL DEFAULT '',
    currency_code TEXT NOT NULL DEFAULT 'GBP',
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    failure_reason TEXT NOT NULL DEFAULT '',
    xero_payment_id TEXT NOT NULL DEFAULT '',
    barclays_payment_id TEXT NOT NULL DEFAULT '',
    barclays_status TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS run_id UUID REFERENCES supplier_payment_runs(id) ON DELETE CASCADE;
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS invoice_id TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS invoice_number TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS supplier_name TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS supplier_email TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS currency_code TEXT NOT NULL DEFAULT 'GBP';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS xero_payment_id TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS barclays_payment_id TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS barclays_status TEXT NOT NULL DEFAULT '';
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE supplier_payment_run_rows ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS supplier_payment_run_rows_run_idx
ON supplier_payment_run_rows (run_id, created_at ASC);

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
    period_start DATE,
    period_end DATE,
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

ALTER TABLE me_report_sync_runs ADD COLUMN IF NOT EXISTS period_start DATE;
ALTER TABLE me_report_sync_runs ADD COLUMN IF NOT EXISTS period_end DATE;

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
    package_reference TEXT NOT NULL DEFAULT '',
    ch_guidance JSONB NOT NULL DEFAULT '{}'::jsonb,
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
ALTER TABLE ch_settings ADD COLUMN IF NOT EXISTS package_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_settings ADD COLUMN IF NOT EXISTS ch_guidance JSONB NOT NULL DEFAULT '{}'::jsonb;

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
ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS vat_number TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS contact_email TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS contact_phone TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS client_address TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS company_utr TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_code_register ADD COLUMN IF NOT EXISTS personal_utr TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS ch_auth_code_register_name_idx
ON ch_auth_code_register (normalised_name);

CREATE INDEX IF NOT EXISTS ch_auth_code_register_uploaded_at_idx
ON ch_auth_code_register (uploaded_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS ch_auth_register_client_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    register_row_id UUID NOT NULL UNIQUE REFERENCES ch_auth_code_register(id) ON DELETE CASCADE,
    services JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk_assessment JSONB NOT NULL DEFAULT '{}'::jsonb,
    companies_house JSONB NOT NULL DEFAULT '{}'::jsonb,
    juk_invoices JSONB NOT NULL DEFAULT '{}'::jsonb,
    timeline_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS services JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS risk_assessment JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS companies_house JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS juk_invoices JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS timeline_meta JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ch_auth_register_client_profiles ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS ch_auth_register_client_profiles_row_idx
ON ch_auth_register_client_profiles (register_row_id);

CREATE TABLE IF NOT EXISTS ch_auth_register_client_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    register_row_id UUID NOT NULL REFERENCES ch_auth_code_register(id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT '',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_by_name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE ch_auth_register_client_notes ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_register_client_notes ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE ch_auth_register_client_notes ADD COLUMN IF NOT EXISTS created_by_name TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_register_client_notes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS ch_auth_register_client_notes_row_idx
ON ch_auth_register_client_notes (register_row_id, created_at DESC);

CREATE TABLE IF NOT EXISTS contact_archive_contacts_cache (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    contact_status TEXT NOT NULL DEFAULT 'ACTIVE',
    is_customer BOOLEAN NOT NULL DEFAULT FALSE,
    xero_updated_at TIMESTAMPTZ,
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, tenant_id, contact_id)
);

ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS contact_name TEXT NOT NULL DEFAULT '';
ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT '';
ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS contact_status TEXT NOT NULL DEFAULT 'ACTIVE';
ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS is_customer BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS xero_updated_at TIMESTAMPTZ;
ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS raw JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE contact_archive_contacts_cache ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS contact_archive_contacts_cache_lookup_idx
ON contact_archive_contacts_cache (user_id, tenant_id, contact_status, is_customer, updated_at DESC);

CREATE TABLE IF NOT EXISTS contact_archive_register_matches (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    register_name TEXT NOT NULL DEFAULT '',
    match_source TEXT NOT NULL DEFAULT 'jenius_ai',
    match_reason TEXT NOT NULL DEFAULT '',
    confidence NUMERIC(6, 4) NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    matched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, tenant_id, contact_id)
);

ALTER TABLE contact_archive_register_matches ADD COLUMN IF NOT EXISTS register_name TEXT NOT NULL DEFAULT '';
ALTER TABLE contact_archive_register_matches ADD COLUMN IF NOT EXISTS match_source TEXT NOT NULL DEFAULT 'jenius_ai';
ALTER TABLE contact_archive_register_matches ADD COLUMN IF NOT EXISTS match_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE contact_archive_register_matches ADD COLUMN IF NOT EXISTS confidence NUMERIC(6, 4) NOT NULL DEFAULT 0;
ALTER TABLE contact_archive_register_matches ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE contact_archive_register_matches ADD COLUMN IF NOT EXISTS matched_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE contact_archive_register_matches ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS contact_archive_register_matches_lookup_idx
ON contact_archive_register_matches (user_id, tenant_id, active, updated_at DESC);

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
    sa_nino TEXT NOT NULL DEFAULT '',
    ct_utr TEXT NOT NULL DEFAULT '',
    postcode TEXT NOT NULL DEFAULT '',
    paye_reference TEXT NOT NULL DEFAULT '',
    tax_office_number TEXT NOT NULL DEFAULT '',
    tax_office_reference TEXT NOT NULL DEFAULT '',
    accounts_office_reference TEXT NOT NULL DEFAULT '',
    company_number TEXT NOT NULL DEFAULT '',
    include_sa BOOLEAN NOT NULL DEFAULT FALSE,
    include_paye BOOLEAN NOT NULL DEFAULT FALSE,
    include_ct BOOLEAN NOT NULL DEFAULT FALSE,
    include_vat_mtd BOOLEAN NOT NULL DEFAULT FALSE,
    include_sa_mtd BOOLEAN NOT NULL DEFAULT FALSE,
    include_cis BOOLEAN NOT NULL DEFAULT FALSE,
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
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS sa_nino TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS ct_utr TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS postcode TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS paye_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS tax_office_number TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS tax_office_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS accounts_office_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS company_number TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_sa BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_paye BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_ct BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_vat_mtd BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_sa_mtd BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE hmrc_64_8_requests ADD COLUMN IF NOT EXISTS include_cis BOOLEAN NOT NULL DEFAULT FALSE;
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

CREATE TABLE IF NOT EXISTS hmrc_mtd_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    access_token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id)
);

ALTER TABLE hmrc_mtd_connections ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE hmrc_mtd_connections ADD COLUMN IF NOT EXISTS access_token TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_connections ADD COLUMN IF NOT EXISTS refresh_token TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_connections ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_connections ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE hmrc_mtd_connections ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE hmrc_mtd_connections ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS hmrc_mtd_connections_user_unique_idx
ON hmrc_mtd_connections (user_id);

CREATE TABLE IF NOT EXISTS hmrc_mtd_vat_authorisations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gateway_client_id TEXT NOT NULL DEFAULT '',
    vrn TEXT NOT NULL DEFAULT '',
    agent_reference_number TEXT NOT NULL DEFAULT '',
    service TEXT NOT NULL DEFAULT 'mtd-vat',
    invitation_id TEXT NOT NULL DEFAULT '',
    authorisation_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    status_detail TEXT NOT NULL DEFAULT '',
    requested_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    raw_request JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_status JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS gateway_client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS vrn TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS agent_reference_number TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS service TEXT NOT NULL DEFAULT 'mtd-vat';
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS invitation_id TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS authorisation_url TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS status_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ;
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS raw_request JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS raw_status JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE hmrc_mtd_vat_authorisations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS hmrc_mtd_vat_authorisations_user_gateway_vrn_uidx
ON hmrc_mtd_vat_authorisations (user_id, gateway_client_id, vrn);

CREATE INDEX IF NOT EXISTS hmrc_mtd_vat_authorisations_status_idx
ON hmrc_mtd_vat_authorisations (status, updated_at DESC);

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

CREATE TABLE IF NOT EXISTS payroll_headcount_workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    tenant_name TEXT NOT NULL DEFAULT '',
    workspace_name TEXT NOT NULL DEFAULT '',
    wizard_completed BOOLEAN NOT NULL DEFAULT FALSE,
    ignition_plan_name TEXT NOT NULL DEFAULT '',
    ignition_client_name TEXT NOT NULL DEFAULT '',
    ignition_proposal_name TEXT NOT NULL DEFAULT '',
    ignition_matched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, tenant_id)
);

ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS tenant_name TEXT NOT NULL DEFAULT '';
ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS workspace_name TEXT NOT NULL DEFAULT '';
ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS wizard_completed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS ignition_plan_name TEXT NOT NULL DEFAULT '';
ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS ignition_client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS ignition_proposal_name TEXT NOT NULL DEFAULT '';
ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS ignition_matched_at TIMESTAMPTZ;
ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE payroll_headcount_workspaces ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS payroll_headcount_workspaces_user_idx
ON payroll_headcount_workspaces (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS payroll_headcount_monthly_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES payroll_headcount_workspaces(id) ON DELETE CASCADE,
    month_start DATE NOT NULL,
    headcount INTEGER NOT NULL DEFAULT 0,
    payroll_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'xero-payroll',
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, month_start)
);

ALTER TABLE payroll_headcount_monthly_snapshots ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'xero-payroll';
ALTER TABLE payroll_headcount_monthly_snapshots ADD COLUMN IF NOT EXISTS fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE payroll_headcount_monthly_snapshots ADD COLUMN IF NOT EXISTS raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE payroll_headcount_monthly_snapshots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE payroll_headcount_monthly_snapshots ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS payroll_headcount_monthly_snapshots_workspace_idx
ON payroll_headcount_monthly_snapshots (workspace_id, month_start DESC);

CREATE TABLE IF NOT EXISTS pi_clearing_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL DEFAULT '',
    batch_number TEXT NOT NULL DEFAULT '',
    month_start DATE NOT NULL,
    month_end DATE NOT NULL,
    account_code TEXT NOT NULL DEFAULT 'PI Clearing Account',
    status TEXT NOT NULL DEFAULT 'draft',
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    ai_analysis JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, tenant_id, month_start)
);

ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS batch_number TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS month_end DATE;
ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS account_code TEXT NOT NULL DEFAULT 'PI Clearing Account';
ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS summary JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS ai_analysis JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE pi_clearing_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS pi_clearing_runs_user_month_idx
ON pi_clearing_runs (user_id, month_start DESC);

CREATE UNIQUE INDEX IF NOT EXISTS pi_clearing_runs_user_tenant_batch_number_idx
ON pi_clearing_runs (user_id, tenant_id, batch_number)
WHERE batch_number <> '';

CREATE TABLE IF NOT EXISTS pi_clearing_run_rows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES pi_clearing_runs(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    month_start DATE NOT NULL,
    month_end DATE NOT NULL,
    row_type TEXT NOT NULL DEFAULT 'difference',
    match_key TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    xero_contact_id TEXT NOT NULL DEFAULT '',
    xero_payment_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    ignition_payment_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    currency_code TEXT NOT NULL DEFAULT 'GBP',
    xero_total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    ignition_total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    difference_total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    recommendation TEXT NOT NULL DEFAULT '',
    resolution_status TEXT NOT NULL DEFAULT 'pending',
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS month_start DATE;
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS month_end DATE;
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS row_type TEXT NOT NULL DEFAULT 'difference';
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS match_key TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS xero_contact_id TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS xero_payment_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS ignition_payment_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS currency_code TEXT NOT NULL DEFAULT 'GBP';
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS xero_total NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS ignition_total NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS difference_total NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS recommendation TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS resolution_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE pi_clearing_run_rows ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS pi_clearing_run_rows_run_idx
ON pi_clearing_run_rows (run_id, difference_total DESC, client_name);

CREATE TABLE IF NOT EXISTS pi_clearing_credit_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES pi_clearing_runs(id) ON DELETE CASCADE,
    run_row_id UUID REFERENCES pi_clearing_run_rows(id) ON DELETE SET NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    xero_contact_id TEXT NOT NULL DEFAULT '',
    xero_credit_note_id TEXT NOT NULL DEFAULT '',
    xero_credit_note_number TEXT NOT NULL DEFAULT '',
    credit_note_date DATE,
    amount NUMERIC(14, 2) NOT NULL DEFAULT 0,
    currency_code TEXT NOT NULL DEFAULT 'GBP',
    account_code TEXT NOT NULL DEFAULT 'PI Clearing Account',
    status TEXT NOT NULL DEFAULT 'created',
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS run_row_id UUID REFERENCES pi_clearing_run_rows(id) ON DELETE SET NULL;
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS xero_contact_id TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS xero_credit_note_id TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS xero_credit_note_number TEXT NOT NULL DEFAULT '';
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS credit_note_date DATE;
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS amount NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS currency_code TEXT NOT NULL DEFAULT 'GBP';
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS account_code TEXT NOT NULL DEFAULT 'PI Clearing Account';
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'created';
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE pi_clearing_credit_notes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS pi_clearing_credit_notes_run_idx
ON pi_clearing_credit_notes (run_id, created_at DESC);

CREATE TABLE IF NOT EXISTS juksib_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    batch_reference TEXT NOT NULL,
    source_tenant_id TEXT NOT NULL DEFAULT '',
    source_tenant_name TEXT NOT NULL DEFAULT '',
    invoice_date_from DATE,
    invoice_date_to DATE,
    mode TEXT NOT NULL DEFAULT 'test',
    status TEXT NOT NULL DEFAULT 'imported',
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, batch_reference)
);

ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS source_tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS source_tenant_name TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS invoice_date_from DATE;
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS invoice_date_to DATE;
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'test';
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'imported';
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS summary JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE juksib_batches ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS juksib_batches_user_created_idx
ON juksib_batches (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS juksib_batch_invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES juksib_batches(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    register_row_id UUID REFERENCES ch_auth_code_register(id) ON DELETE SET NULL,
    juk_xero_invoice_id TEXT NOT NULL,
    juk_invoice_number TEXT NOT NULL DEFAULT '',
    juk_contact_id TEXT NOT NULL DEFAULT '',
    juk_contact_name TEXT NOT NULL DEFAULT '',
    invoice_date DATE,
    due_date DATE,
    subtotal NUMERIC(14, 2) NOT NULL DEFAULT 0,
    vat_total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    amount_due NUMERIC(14, 2) NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'GBP',
    line_items JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_xero_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    pdf_file_reference TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'imported',
    duplicate_flag BOOLEAN NOT NULL DEFAULT FALSE,
    matched_client_id TEXT NOT NULL DEFAULT '',
    matched_xero_tenant_id TEXT NOT NULL DEFAULT '',
    matched_xero_tenant_name TEXT NOT NULL DEFAULT '',
    match_source TEXT NOT NULL DEFAULT '',
    match_confidence NUMERIC(6, 5) NOT NULL DEFAULT 0,
    match_reason TEXT NOT NULL DEFAULT '',
    alternatives JSONB NOT NULL DEFAULT '[]'::jsonb,
    approved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    approved_at TIMESTAMPTZ,
    published_bill_id TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMPTZ,
    error_message TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (batch_id, juk_xero_invoice_id)
);

ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS register_row_id UUID REFERENCES ch_auth_code_register(id) ON DELETE SET NULL;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS juk_contact_id TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS juk_contact_name TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS amount_due NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS line_items JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS raw_xero_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS pdf_file_reference TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS duplicate_flag BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS matched_client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS matched_xero_tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS matched_xero_tenant_name TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS match_source TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS match_confidence NUMERIC(6, 5) NOT NULL DEFAULT 0;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS match_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS alternatives JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS approved_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS published_bill_id TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS error_message TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE juksib_batch_invoices ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS juksib_batch_invoices_batch_status_idx
ON juksib_batch_invoices (batch_id, status, created_at);

CREATE INDEX IF NOT EXISTS juksib_batch_invoices_user_invoice_idx
ON juksib_batch_invoices (user_id, juk_xero_invoice_id);

CREATE INDEX IF NOT EXISTS juksib_batch_invoices_register_row_idx
ON juksib_batch_invoices (register_row_id, invoice_date DESC);

CREATE TABLE IF NOT EXISTS juksib_match_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    normalised_invoice_name TEXT NOT NULL,
    original_invoice_name TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    xero_tenant_id TEXT NOT NULL DEFAULT '',
    xero_tenant_name TEXT NOT NULL DEFAULT '',
    match_type TEXT NOT NULL DEFAULT 'manual_override',
    confidence_override NUMERIC(6, 5) NOT NULL DEFAULT 1,
    usage_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT NOT NULL DEFAULT '',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE juksib_match_rules ADD COLUMN IF NOT EXISTS xero_tenant_name TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_match_rules ADD COLUMN IF NOT EXISTS usage_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE juksib_match_rules ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
ALTER TABLE juksib_match_rules ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE juksib_match_rules ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_match_rules ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE juksib_match_rules ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE juksib_match_rules ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS juksib_match_rules_user_name_idx
ON juksib_match_rules (user_id, normalised_invoice_name, active, updated_at DESC);

CREATE TABLE IF NOT EXISTS juksib_sync_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    batch_id UUID REFERENCES juksib_batches(id) ON DELETE SET NULL,
    juk_xero_invoice_id TEXT NOT NULL,
    juk_invoice_number TEXT NOT NULL DEFAULT '',
    source_tenant_id TEXT NOT NULL DEFAULT '',
    destination_tenant_id TEXT NOT NULL DEFAULT '',
    destination_tenant_name TEXT NOT NULL DEFAULT '',
    destination_bill_id TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    sync_status TEXT NOT NULL DEFAULT 'published',
    pdf_attached BOOLEAN NOT NULL DEFAULT FALSE,
    error_message TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, source_tenant_id, juk_xero_invoice_id, destination_tenant_id)
);

ALTER TABLE juksib_sync_records ADD COLUMN IF NOT EXISTS destination_tenant_name TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_sync_records ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_sync_records ADD COLUMN IF NOT EXISTS sync_status TEXT NOT NULL DEFAULT 'published';
ALTER TABLE juksib_sync_records ADD COLUMN IF NOT EXISTS pdf_attached BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE juksib_sync_records ADD COLUMN IF NOT EXISTS error_message TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_sync_records ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE juksib_sync_records ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS juksib_sync_records_user_created_idx
ON juksib_sync_records (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ch_auth_register_client_juk_invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    register_row_id UUID NOT NULL REFERENCES ch_auth_code_register(id) ON DELETE CASCADE,
    batch_id UUID REFERENCES juksib_batches(id) ON DELETE SET NULL,
    juk_xero_invoice_id TEXT NOT NULL,
    juk_invoice_number TEXT NOT NULL DEFAULT '',
    juk_contact_name TEXT NOT NULL DEFAULT '',
    invoice_date DATE,
    due_date DATE,
    total NUMERIC(14, 2) NOT NULL DEFAULT 0,
    amount_due NUMERIC(14, 2) NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'GBP',
    payment_status TEXT NOT NULL DEFAULT 'unpaid',
    source_status TEXT NOT NULL DEFAULT 'imported',
    sync_status TEXT NOT NULL DEFAULT 'pending',
    destination_tenant_id TEXT NOT NULL DEFAULT '',
    destination_tenant_name TEXT NOT NULL DEFAULT '',
    destination_bill_id TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    source_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (register_row_id, juk_xero_invoice_id)
);

ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS batch_id UUID REFERENCES juksib_batches(id) ON DELETE SET NULL;
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS juk_invoice_number TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS juk_contact_name TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS invoice_date DATE;
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS due_date DATE;
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS total NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS amount_due NUMERIC(14, 2) NOT NULL DEFAULT 0;
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'GBP';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'unpaid';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS source_status TEXT NOT NULL DEFAULT 'imported';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS sync_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS destination_tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS destination_tenant_name TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS destination_bill_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS source_payload JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ch_auth_register_client_juk_invoices ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS ch_auth_register_client_juk_invoices_row_idx
ON ch_auth_register_client_juk_invoices (register_row_id, invoice_date DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS juksib_vat_lookup_cache (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    normalised_client_name TEXT NOT NULL,
    source_client_name TEXT NOT NULL DEFAULT '',
    register_id UUID,
    register_name TEXT NOT NULL DEFAULT '',
    vat_number TEXT NOT NULL DEFAULT '',
    vat_registered BOOLEAN NOT NULL DEFAULT FALSE,
    lookup_source TEXT NOT NULL DEFAULT 'register_exact',
    lookup_confidence NUMERIC(6, 5) NOT NULL DEFAULT 0,
    lookup_reason TEXT NOT NULL DEFAULT '',
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, normalised_client_name)
);

ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS source_client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS register_id UUID;
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS register_name TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS vat_number TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS vat_registered BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS lookup_source TEXT NOT NULL DEFAULT 'register_exact';
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS lookup_confidence NUMERIC(6, 5) NOT NULL DEFAULT 0;
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS lookup_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE juksib_vat_lookup_cache ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS juksib_vat_lookup_cache_user_checked_idx
ON juksib_vat_lookup_cache (user_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS juksib_audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    batch_id UUID REFERENCES juksib_batches(id) ON DELETE SET NULL,
    entity_type TEXT NOT NULL DEFAULT '',
    entity_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    old_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    new_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS juksib_audit_logs_user_batch_idx
ON juksib_audit_logs (user_id, batch_id, created_at DESC);

CREATE TABLE IF NOT EXISTS juksib_automation_settings (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    schedule_time TEXT NOT NULL DEFAULT '09:00',
    timezone TEXT NOT NULL DEFAULT 'Europe/London',
    recipient_emails JSONB NOT NULL DEFAULT '[]'::jsonb,
    include_paid_invoices BOOLEAN NOT NULL DEFAULT FALSE,
    auto_publish BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at TIMESTAMPTZ,
    last_run_local_date DATE,
    updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS schedule_time TEXT NOT NULL DEFAULT '09:00';
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Europe/London';
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS recipient_emails JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS include_paid_invoices BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS auto_publish BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS last_run_at TIMESTAMPTZ;
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS last_run_local_date DATE;
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS updated_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE juksib_automation_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE TABLE IF NOT EXISTS juksib_automation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trigger_type TEXT NOT NULL DEFAULT 'scheduled',
    status TEXT NOT NULL DEFAULT 'running',
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

ALTER TABLE juksib_automation_runs ADD COLUMN IF NOT EXISTS trigger_type TEXT NOT NULL DEFAULT 'scheduled';
ALTER TABLE juksib_automation_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'running';
ALTER TABLE juksib_automation_runs ADD COLUMN IF NOT EXISTS summary JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE juksib_automation_runs ADD COLUMN IF NOT EXISTS error_message TEXT NOT NULL DEFAULT '';
ALTER TABLE juksib_automation_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE juksib_automation_runs ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS juksib_automation_runs_user_started_idx
ON juksib_automation_runs (user_id, started_at DESC);

CREATE TABLE IF NOT EXISTS call_extension_directory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extension TEXT NOT NULL,
    staff_name TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (extension)
);

ALTER TABLE call_extension_directory ADD COLUMN IF NOT EXISTS extension TEXT NOT NULL DEFAULT '';
ALTER TABLE call_extension_directory ADD COLUMN IF NOT EXISTS staff_name TEXT NOT NULL DEFAULT '';
ALTER TABLE call_extension_directory ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE call_extension_directory ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
ALTER TABLE call_extension_directory ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE call_extension_directory ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS call_extension_directory_staff_idx
ON call_extension_directory (LOWER(staff_name));

CREATE TABLE IF NOT EXISTS call_import_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    uploaded_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    source_filename TEXT NOT NULL DEFAULT '',
    source_file_hash TEXT NOT NULL DEFAULT '',
    source_provider TEXT NOT NULL DEFAULT '',
    total_rows INTEGER NOT NULL DEFAULT 0,
    new_rows INTEGER NOT NULL DEFAULT 0,
    duplicate_rows INTEGER NOT NULL DEFAULT 0,
    invalid_rows INTEGER NOT NULL DEFAULT 0,
    matched_rows INTEGER NOT NULL DEFAULT 0,
    unmatched_rows INTEGER NOT NULL DEFAULT 0,
    mapping_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    import_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS uploaded_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS source_filename TEXT NOT NULL DEFAULT '';
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS source_file_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS source_provider TEXT NOT NULL DEFAULT '';
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS total_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS new_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS duplicate_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS invalid_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS matched_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS unmatched_rows INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS mapping_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS import_summary JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE call_import_files ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS call_import_files_created_idx
ON call_import_files (created_at DESC);

CREATE TABLE IF NOT EXISTS call_import_rows_raw (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    import_file_id UUID NOT NULL REFERENCES call_import_files(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL DEFAULT 0,
    original_row JSONB NOT NULL DEFAULT '{}'::jsonb,
    fingerprint TEXT NOT NULL DEFAULT '',
    is_duplicate BOOLEAN NOT NULL DEFAULT FALSE,
    is_valid BOOLEAN NOT NULL DEFAULT TRUE,
    validation_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    processed_call_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS import_file_id UUID REFERENCES call_import_files(id) ON DELETE CASCADE;
ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS row_number INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS original_row JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS fingerprint TEXT NOT NULL DEFAULT '';
ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS is_valid BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS validation_errors JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS processed_call_id UUID;
ALTER TABLE call_import_rows_raw ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS call_import_rows_raw_import_idx
ON call_import_rows_raw (import_file_id, row_number ASC);

CREATE INDEX IF NOT EXISTS call_import_rows_raw_fingerprint_idx
ON call_import_rows_raw (fingerprint);

CREATE TABLE IF NOT EXISTS call_records_processed (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    import_file_id UUID REFERENCES call_import_files(id) ON DELETE SET NULL,
    raw_row_id UUID REFERENCES call_import_rows_raw(id) ON DELETE SET NULL,
    call_fingerprint TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT '',
    call_datetime TIMESTAMPTZ,
    call_date DATE,
    call_time TEXT NOT NULL DEFAULT '',
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    cost NUMERIC(12, 4) NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT '',
    from_number TEXT NOT NULL DEFAULT '',
    to_number TEXT NOT NULL DEFAULT '',
    external_number TEXT NOT NULL DEFAULT '',
    internal_extension TEXT NOT NULL DEFAULT '',
    staff_member TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    client_name TEXT NOT NULL DEFAULT '',
    client_manager TEXT NOT NULL DEFAULT '',
    matched_status TEXT NOT NULL DEFAULT 'unmatched',
    match_source TEXT NOT NULL DEFAULT '',
    number_tag TEXT NOT NULL DEFAULT '',
    ignored BOOLEAN NOT NULL DEFAULT FALSE,
    last_resync_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (call_fingerprint)
);

ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS import_file_id UUID REFERENCES call_import_files(id) ON DELETE SET NULL;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS raw_row_id UUID REFERENCES call_import_rows_raw(id) ON DELETE SET NULL;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS call_fingerprint TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS call_datetime TIMESTAMPTZ;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS call_date DATE;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS call_time TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS duration_seconds INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS cost NUMERIC(12, 4) NOT NULL DEFAULT 0;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS from_number TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS to_number TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS external_number TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS internal_extension TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS staff_member TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS client_manager TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS matched_status TEXT NOT NULL DEFAULT 'unmatched';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS match_source TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS number_tag TEXT NOT NULL DEFAULT '';
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS ignored BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS last_resync_at TIMESTAMPTZ;
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE call_records_processed ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE UNIQUE INDEX IF NOT EXISTS call_records_processed_fingerprint_uidx
ON call_records_processed (call_fingerprint);

CREATE INDEX IF NOT EXISTS call_records_processed_date_idx
ON call_records_processed (call_date DESC, call_datetime DESC);

CREATE INDEX IF NOT EXISTS call_records_processed_match_idx
ON call_records_processed (matched_status, client_id);

CREATE INDEX IF NOT EXISTS call_records_processed_user_date_idx
ON call_records_processed (user_id, call_date DESC, call_datetime DESC);

CREATE INDEX IF NOT EXISTS call_records_processed_staff_idx
ON call_records_processed (LOWER(staff_member), call_date DESC);

CREATE INDEX IF NOT EXISTS call_records_processed_external_idx
ON call_records_processed (external_number, call_date DESC);

CREATE TABLE IF NOT EXISTS call_number_labels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    number_value TEXT NOT NULL,
    label_type TEXT NOT NULL DEFAULT '',
    assigned_client_id TEXT NOT NULL DEFAULT '',
    assigned_client_name TEXT NOT NULL DEFAULT '',
    assigned_client_manager TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (number_value)
);

ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS number_value TEXT NOT NULL DEFAULT '';
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS label_type TEXT NOT NULL DEFAULT '';
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS assigned_client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS assigned_client_name TEXT NOT NULL DEFAULT '';
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS assigned_client_manager TEXT NOT NULL DEFAULT '';
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS notes TEXT NOT NULL DEFAULT '';
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE call_number_labels ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS call_number_labels_type_idx
ON call_number_labels (label_type, updated_at DESC);

CREATE INDEX IF NOT EXISTS call_number_labels_user_idx
ON call_number_labels (user_id, number_value);

CREATE TABLE IF NOT EXISTS call_resync_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    triggered_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    trigger_source TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    scanned_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE call_resync_audit ADD COLUMN IF NOT EXISTS triggered_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE call_resync_audit ADD COLUMN IF NOT EXISTS trigger_source TEXT NOT NULL DEFAULT '';
ALTER TABLE call_resync_audit ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT '';
ALTER TABLE call_resync_audit ADD COLUMN IF NOT EXISTS scanned_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_resync_audit ADD COLUMN IF NOT EXISTS updated_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE call_resync_audit ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE call_resync_audit ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS call_resync_audit_created_idx
ON call_resync_audit (created_at DESC);

CREATE TABLE IF NOT EXISTS call_ai_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    report_scope TEXT NOT NULL DEFAULT 'practice',
    period_month TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    generated_by TEXT NOT NULL DEFAULT 'local',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE call_ai_reports ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE call_ai_reports ADD COLUMN IF NOT EXISTS report_scope TEXT NOT NULL DEFAULT 'practice';
ALTER TABLE call_ai_reports ADD COLUMN IF NOT EXISTS period_month TEXT NOT NULL DEFAULT '';
ALTER TABLE call_ai_reports ADD COLUMN IF NOT EXISTS client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE call_ai_reports ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';
ALTER TABLE call_ai_reports ADD COLUMN IF NOT EXISTS report_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE call_ai_reports ADD COLUMN IF NOT EXISTS generated_by TEXT NOT NULL DEFAULT 'local';
ALTER TABLE call_ai_reports ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS call_ai_reports_scope_period_idx
ON call_ai_reports (user_id, report_scope, period_month DESC, created_at DESC);

INSERT INTO call_extension_directory (extension, staff_name, notes)
VALUES
    ('200', 'Martha', ''),
    ('203', 'Tom', ''),
    ('204', 'Office Spare', ''),
    ('205', 'Jay', ''),
    ('206', 'T Room', ''),
    ('207', 'Dean H', ''),
    ('208', 'Hannah', ''),
    ('209', 'Lauren', ''),
    ('210', 'Boardroom', ''),
    ('211', 'Mia', ''),
    ('212', 'Gracie', ''),
    ('213', 'Amie', '')
ON CONFLICT (extension) DO NOTHING;

CREATE TABLE IF NOT EXISTS snack_products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'general',
    price_pence INTEGER NOT NULL DEFAULT 0,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 100,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS snack_products_active_sort_idx
ON snack_products (active, sort_order, name);

CREATE TABLE IF NOT EXISTS snack_customers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    auth_provider TEXT NOT NULL DEFAULT 'email',
    provider_user_id TEXT NOT NULL DEFAULT '',
    stripe_customer_id TEXT NOT NULL DEFAULT '',
    is_guest BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    total_orders INTEGER NOT NULL DEFAULT 0,
    total_cans INTEGER NOT NULL DEFAULT 0,
    lifetime_spend_pence INTEGER NOT NULL DEFAULT 0,
    lifetime_savings_pence INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS snack_customers_email_idx
ON snack_customers (email);

CREATE TABLE IF NOT EXISTS snack_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID REFERENCES snack_customers(id) ON DELETE CASCADE,
    session_token_hash TEXT NOT NULL UNIQUE,
    device_label TEXT NOT NULL DEFAULT 'mobile-web',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS snack_sessions_customer_idx
ON snack_sessions (customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS snack_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_number TEXT NOT NULL UNIQUE,
    customer_id UUID REFERENCES snack_customers(id) ON DELETE SET NULL,
    guest_email TEXT,
    stripe_customer_id TEXT NOT NULL DEFAULT '',
    stripe_payment_intent_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    subtotal_pence INTEGER NOT NULL DEFAULT 0,
    weekly_discount_pence INTEGER NOT NULL DEFAULT 0,
    milestone_discount_pence INTEGER NOT NULL DEFAULT 0,
    total_discount_pence INTEGER NOT NULL DEFAULT 0,
    total_paid_pence INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'gbp',
    week_start_date DATE NOT NULL,
    is_10th_order_reward BOOLEAN NOT NULL DEFAULT FALSE,
    double_reward_active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paid_at TIMESTAMPTZ,
    refunded_at TIMESTAMPTZ,
    admin_notes TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS snack_orders_customer_created_idx
ON snack_orders (customer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS snack_orders_status_created_idx
ON snack_orders (status, created_at DESC);

CREATE INDEX IF NOT EXISTS snack_orders_week_status_idx
ON snack_orders (week_start_date, status, created_at DESC);

CREATE TABLE IF NOT EXISTS snack_order_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES snack_orders(id) ON DELETE CASCADE,
    product_id UUID REFERENCES snack_products(id) ON DELETE SET NULL,
    product_sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price_pence INTEGER NOT NULL DEFAULT 0,
    full_price_quantity INTEGER NOT NULL DEFAULT 0,
    weekly_discount_quantity INTEGER NOT NULL DEFAULT 0,
    weekly_discount_pence INTEGER NOT NULL DEFAULT 0,
    milestone_discount_pence INTEGER NOT NULL DEFAULT 0,
    final_line_total_pence INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS snack_order_items_order_idx
ON snack_order_items (order_id, created_at ASC);

CREATE TABLE IF NOT EXISTS snack_loyalty_weeks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id UUID NOT NULL REFERENCES snack_customers(id) ON DELETE CASCADE,
    week_start_date DATE NOT NULL,
    cans_purchased_count INTEGER NOT NULL DEFAULT 0,
    weekly_discount_cans_count INTEGER NOT NULL DEFAULT 0,
    weekly_savings_pence INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (customer_id, week_start_date)
);

CREATE INDEX IF NOT EXISTS snack_loyalty_weeks_customer_week_idx
ON snack_loyalty_weeks (customer_id, week_start_date DESC);

CREATE TABLE IF NOT EXISTS snack_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL DEFAULT '',
    before_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS snack_audit_log_entity_idx
ON snack_audit_log (entity_type, entity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS snack_order_number_sequence (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY
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
            cursor.execute(
                """
                WITH updated AS (
                    UPDATE users
                    SET
                        email = 'JAY@JACCOUNTANCY.CO.UK',
                        full_name = 'Jay Wilson',
                        role = 'owner',
                        status = 'active',
                        auth_method = 'xero_only',
                        two_factor_method = 'none',
                        is_super_admin = TRUE,
                        updated_at = NOW()
                    WHERE lower(email) = lower('JAY@JACCOUNTANCY.CO.UK')
                    RETURNING id
                )
                INSERT INTO users (
                    email,
                    full_name,
                    role,
                    status,
                    auth_method,
                    two_factor_method,
                    is_super_admin,
                    notes
                )
                SELECT
                    'JAY@JACCOUNTANCY.CO.UK',
                    'Jay Wilson',
                    'owner',
                    'active',
                    'xero_only',
                    'none',
                    TRUE,
                    'Seeded owner account'
                WHERE NOT EXISTS (SELECT 1 FROM updated)
                """
            )
        connection.commit()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
