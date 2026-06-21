# Jenius Platform Backend

Jenius started as a Credit Control Console and has rapidly evolved into Jaccountancy's all-in-one operating platform.

Today this backend powers a broader system across practice operations, including:

- credit control and debtor management
- client, invoice, cash, and document workflows
- Xero integration (accounting + payroll)
- HMRC integration
- Companies House integration
- AI-assisted extraction, matching, drafting, and workflow support using OpenAI
- the web-based Jenius console and the macOS companion app

Jenius is built to replace fragmented point tools with one connected platform, including workflows previously spread across products such as Ignition, Dext, Adobe, BrightManager, and separate client portals.

## Railway

Recommended Railway settings:

- `Root Directory`: `backend`
- `Build Command`: `pip install -e .`
- `Start Command`: `sh -c 'python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}'`

Required variables:

- `BASE_URL=https://jenius.jaccountancy.co.uk`
- `DATABASE_URL`
- `APP_SECRET`
- `WIDGET_TOKEN`
- `XERO_CLIENT_ID`
- `XERO_CLIENT_SECRET`
- `XERO_REDIRECT_URI=https://jenius.jaccountancy.co.uk/auth/xero/callback`
- `XERO_SCOPES=openid profile email offline_access accounting.invoices accounting.payments accounting.banktransactions accounting.manualjournals accounting.contacts accounting.settings accounting.attachments accounting.reports.banksummary.read accounting.reports.balancesheet.read accounting.reports.profitandloss.read accounting.reports.trialbalance.read payroll.employees payroll.payruns`
- `XERO_ENABLE_PAYROLL_SCOPES=true` when payroll access is required in your app
- Use granular scopes for Web/PKCE flows. Avoid deprecated broad scopes (`accounting.transactions`, `accounting.reports.read`) unless your Xero app specifically requires legacy compatibility.
- `IGNITION_CLIENT_ID`
- `IGNITION_CLIENT_SECRET`
- `IGNITION_REDIRECT_URI=https://<your-api-domain>/api/ignition/callback`
- `IGNITION_REDIRECT_URL=https://<your-api-domain>/api/ignition/callback` is also accepted as a legacy alias if Railway already has that name.
- `IGNITION_SCOPES=reporting`
- `IGNITION_RENEWALS_RECIPIENT_EMAIL=amie@jaccountancy.co.uk`
- `PANEL_ALLOWED_ORIGINS=https://jenius.jaccountancy.co.uk`
- `IGNITION_STATE_TTL_SECONDS=3600`
- `LATE_PAYMENT_CHARGE_ACCOUNT_CODE=1222`
- `LATE_PAYMENT_CHARGE_TAX_TYPE=OUTPUT2`
- `BAD_DEBT_WRITE_OFF_ACCOUNT_CODE=402`
- `OPENAI_API_KEY`
- `OPENAI_MODEL=gpt-4.1-mini`

`PORT` is injected by Railway and should not be hard-coded.

The backend intentionally rejects `localhost` and `127.0.0.1` connection values. Xero must also have the same Railway callback URL registered as an allowed redirect URI.

The late-charge, allocation, write-off, and Jashflow interest workflows create and allocate Xero invoices, credit notes, overpayments, and invoice attachments, so the Xero connection needs the `accounting.invoices`, `accounting.payments`, `accounting.contacts`, `accounting.settings.read`, and `accounting.attachments` scopes. `LATE_PAYMENT_CHARGE_ACCOUNT_CODE` should be a Xero revenue account that can be used on sales invoice line items. `LATE_PAYMENT_CHARGE_TAX_TYPE` defaults to `OUTPUT2` for UK 20% VAT on income. Reconnect Xero once after changing scopes so the refreshed token includes those permissions.

Bank statement extraction uses the OpenAI Responses API for PDF transaction extraction. Set `OPENAI_API_KEY` before uploading statements in the Bank Statements screen.

Ignition integration uses Ignition's Reporting API OAuth application from Developer Hub. The Ignition account must have Reporting API access, and the callback sent by Jenius must exactly match one Redirect URI registered in Ignition Developer Hub. For example, if Railway has `IGNITION_REDIRECT_URI=https://jenius.jaccountancy.co.uk/api/ignition/callback`, Ignition must register that exact URL, including scheme, host, path, and no trailing slash unless Railway also has it. Jenius stores encrypted Ignition tokens, syncs Reporting API datasets into PostgreSQL tables/views, and serves the Ignition dashboard from stored data rather than live dashboard API calls.

## Main routes

- `/` web dashboard
- `/auth/xero/start` login with Xero
- `/auth/xero/callback` OAuth callback
- `/auth/ignition/start` login with Ignition
- `/api/ignition/connect` authenticated Ignition OAuth start endpoint
- `/api/ignition/callback` Ignition OAuth callback
- `/auth/ignition/callback` legacy Ignition OAuth callback alias
- `POST /api/hmrc/*` HMRC workflows (authorisation, profile, VAT/filing data depending on endpoint)
- `POST /api/companies-house/sync` refreshes Companies House company snapshots (profile, officers, PSCs, filing history) into PostgreSQL
- `POST /api/companies-house/submissions/reconcile` polls live CS01 submission statuses and updates accepted/rejected outcomes
- `/customers` customer list
- `/customers/{customer_id}` customer detail
- `/invoices/{invoice_id}` invoice detail
- `POST /sync/run` manual Xero resync
- `GET /api/device/start` device login bootstrap for the macOS app
- `GET /api/device/poll` device login polling for the macOS app
- `GET /api/dashboard` headline metrics API for the macOS app

## Companies House Go-Live Checklist

- Configure Companies House settings in the Confirmation Statements settings tab:
  - environment (`sandbox` first, then `production`)
  - API key
  - Presenter ID
  - Presenter authentication code
  - credit account number
  - Xero invoice defaults (account code, unit amount, description, tax type)
- Import client list and auth codes via the Import tab.
- Run **Sync from Companies House** to populate company status, officers, PSCs, filing history, and due dates.
- Confirm live bulk submission, status reconciliation, and bulk invoice run are working with sandbox data.
- Switch environment to production only after a successful end-to-end sandbox cycle.

## Companies House Production Runbook

### Cutover plan

1. Freeze deployment window and announce filing cutover.
2. Verify `COMPANIES_HOUSE_*` credentials are set in Railway and Settings tab shows expected hints.
3. Run **Test CH connection** in settings and capture screenshot/evidence.
4. Run sandbox smoke cycle:
   - sync
   - bulk submit test company
   - reconcile status
   - raise invoice
   - confirm audit entries and attempts export
5. Switch environment to `production`.
6. Submit one pilot client with known-good authority and auth code.
7. Reconcile submission status and verify payment evidence fields populated.
8. Open full client cohort for filing.

### Rollback plan

1. Switch environment back to `sandbox`.
2. Disable auto-sync in settings.
3. Pause bulk submission jobs.
4. Review dead-letter queue and submission attempts.
5. Re-run only approved client filings after root-cause fix.

### Credentials rotation

1. Rotate API key and presenter authentication code via settings.
2. Re-run **Test CH connection**.
3. Record rotation timestamp in internal ops log.
4. Validate one sandbox submission after rotation.

### Acceptance criteria

- Connection test passes in target environment.
- No unresolved dead-letter items for pilot batch.
- Submission attempts report shows expected statuses.
- Payment evidence present for accepted filings where gateway returns payment metadata.
- Duplicate submissions prevented by idempotency key.
- Filing authority status set to `authorised` for all filed clients.

## Compliance and authorization policy

- A company must have `filing_authority_status = authorised` before CS01 submission is allowed.
- Record `filing_authority_reference`, `filing_authority_received_at`, and `filing_authority_expires_at` for each client authority.
- If authority expires or is revoked, submissions are blocked and logged as skipped with audit evidence.
- Authentication codes are encrypted at rest. Legacy ciphertext remains decryptable for backward compatibility and is replaced on next save.
- Use the submissions attempts export for client-facing evidence and internal compliance archive.
