# Credit Control Backend

This service is the deployable Railway backend for the native macOS app.

It is responsible for:

- authenticating to Xero with a Custom Connection
- pulling read-only accounts receivable data from Xero
- storing a normalized cache in PostgreSQL
- exposing a read-only dashboard API for the Swift app

## Stack

- FastAPI
- psycopg
- PostgreSQL on Railway
- Xero Custom Connection via OAuth 2.0 client credentials

## Local development

1. Create a virtual environment.
2. Install dependencies with `pip install -e .`.
3. Copy `.env.example` to `.env` and fill in the real values.
4. Run `uvicorn app.main:app --reload`.

## Railway deployment

Set these environment variables in Railway:

- `DATABASE_URL`
- `API_TOKEN`
- `XERO_CLIENT_ID`
- `XERO_CLIENT_SECRET`
- `XERO_SCOPES`

Optional:

- `APP_ENV`
- `PORT`
- `DASHBOARD_STALE_AFTER_MINUTES`

Use Railway cron or an external scheduler to call:

- `POST /api/xero/sync`

with:

- `Authorization: Bearer <API_TOKEN>`

The macOS app should call:

- `GET /api/dashboard`

with the same bearer token.

## Xero setup

This backend assumes a Xero Custom Connection rather than a desktop OAuth flow.

Current Xero references:

- https://developer.xero.com/custom-development
- https://developer.xero.com/faq/custom-integration
- https://developer.xero.com/documentation/guides/oauth2/client-credentials/
