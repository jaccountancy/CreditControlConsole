# Credit Control Backend

This backend now owns:

- Xero OAuth login and callback
- token storage and refresh handling
- PostgreSQL persistence
- sync jobs for customers and invoices
- dashboard metrics for the macOS app
- the full web-based credit control panel

## Railway

Recommended Railway settings:

- `Root Directory`: `backend`
- `Build Command`: `pip install -e .`
- `Start Command`: `sh -c 'python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}'`

Required variables:

- `BASE_URL=https://creditcontrolconsole-production.up.railway.app`
- `DATABASE_URL`
- `APP_SECRET`
- `WIDGET_TOKEN`
- `XERO_CLIENT_ID`
- `XERO_CLIENT_SECRET`
- `XERO_REDIRECT_URI=https://creditcontrolconsole-production.up.railway.app/auth/xero/callback`
- `XERO_SCOPES=openid profile email offline_access accounting.invoices accounting.payments accounting.contacts accounting.settings.read accounting.attachments accounting.reports.read accounting.transactions.read accounting.journals.read assets`
- `IGNITION_CLIENT_ID`
- `IGNITION_CLIENT_SECRET`
- `IGNITION_REDIRECT_URI=https://<your-api-domain>/api/ignition/callback`
- `IGNITION_REDIRECT_URL=https://<your-api-domain>/api/ignition/callback` is also accepted as a legacy alias if Railway already has that name.
- `IGNITION_SCOPES=reporting`
- `IGNITION_RENEWALS_RECIPIENT_EMAIL=amie@jaccountancy.co.uk`
- `IGNITION_RENEWALS_ZAPIER_WEBHOOK_URL`
- `PANEL_ALLOWED_ORIGINS=https://www.team.jaccountancy.co.uk,https://team.jaccountancy.co.uk,https://my.jaccountancy.co.uk`
- `LATE_PAYMENT_CHARGE_ACCOUNT_CODE=1222`
- `LATE_PAYMENT_CHARGE_TAX_TYPE=OUTPUT2`
- `BAD_DEBT_WRITE_OFF_ACCOUNT_CODE=402`
- `OPENAI_API_KEY`
- `OPENAI_MODEL=gpt-4.1-mini`

`PORT` is injected by Railway and should not be hard-coded.

The backend intentionally rejects `localhost` and `127.0.0.1` connection values. Xero must also have the same Railway callback URL registered as an allowed redirect URI.

The late-charge, allocation, write-off, and Jashflow interest workflows create and allocate Xero invoices, credit notes, overpayments, and invoice attachments, so the Xero connection needs the `accounting.invoices`, `accounting.payments`, `accounting.contacts`, `accounting.settings.read`, and `accounting.attachments` scopes. `LATE_PAYMENT_CHARGE_ACCOUNT_CODE` should be a Xero revenue account that can be used on sales invoice line items. `LATE_PAYMENT_CHARGE_TAX_TYPE` defaults to `OUTPUT2` for UK 20% VAT on income. Reconnect Xero once after changing scopes so the refreshed token includes those permissions.

Bank statement extraction uses the OpenAI Responses API for PDF transaction extraction. Set `OPENAI_API_KEY` before uploading statements in the Bank Statements screen.

Ignition integration uses Ignition's Reporting API OAuth application from Developer Hub. The Ignition account must have Reporting API access, and the callback sent by Jenius must exactly match one Redirect URI registered in Ignition Developer Hub. For example, if Railway has `IGNITION_REDIRECT_URI=https://my.jaccountancy.co.uk/api/ignition/callback`, Ignition must register that exact URL, including scheme, host, path, and no trailing slash unless Railway also has it. Jenius stores encrypted Ignition tokens, syncs Reporting API datasets into PostgreSQL tables/views, and serves the Ignition dashboard from stored data rather than live dashboard API calls.

## Main routes

- `/` web dashboard
- `/auth/xero/start` login with Xero
- `/auth/xero/callback` OAuth callback
- `/auth/ignition/start` login with Ignition
- `/api/ignition/connect` authenticated Ignition OAuth start endpoint
- `/api/ignition/callback` Ignition OAuth callback
- `/auth/ignition/callback` legacy Ignition OAuth callback alias
- `/customers` customer list
- `/customers/{customer_id}` customer detail
- `/invoices/{invoice_id}` invoice detail
- `POST /sync/run` manual Xero resync
- `GET /api/device/start` device login bootstrap for the macOS app
- `GET /api/device/poll` device login polling for the macOS app
- `GET /api/dashboard` headline metrics API for the macOS app
