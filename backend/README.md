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
- `XERO_SCOPES=openid profile email offline_access accounting.invoices.read accounting.contacts.read`

`PORT` is injected by Railway and should not be hard-coded.

The backend intentionally rejects `localhost` and `127.0.0.1` connection values. Xero must also have the same Railway callback URL registered as an allowed redirect URI.

## Main routes

- `/` web dashboard
- `/auth/xero/start` login with Xero
- `/auth/xero/callback` OAuth callback
- `/customers` customer list
- `/customers/{customer_id}` customer detail
- `/invoices/{invoice_id}` invoice detail
- `POST /sync/run` manual Xero resync
- `GET /api/device/start` device login bootstrap for the macOS app
- `GET /api/device/poll` device login polling for the macOS app
- `GET /api/dashboard` headline metrics API for the macOS app
