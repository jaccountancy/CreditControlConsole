# Credit Control Backend

This backend now owns:

- Xero OAuth login and callback
- token storage and refresh handling
- PostgreSQL persistence
- sync jobs for customers and invoices
- dashboard metrics for the macOS app
- the full web-based credit control panel

## Local run

1. Create `.env` from `.env.example`
2. Install dependencies:
   `pip install -e .`
3. Start the app:
   `python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`

## Railway

Recommended Railway settings:

- `Root Directory`: `backend`
- `Build Command`: `pip install -e .`
- `Start Command`: `pip install -e . && python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT`

Required variables:

- `BASE_URL`
- `DATABASE_URL`
- `APP_SECRET`
- `WIDGET_TOKEN`
- `XERO_CLIENT_ID`
- `XERO_CLIENT_SECRET`
- `XERO_REDIRECT_URI`
- `XERO_SCOPES`

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
