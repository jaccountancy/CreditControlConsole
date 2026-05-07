from datetime import date, datetime
from urllib.parse import quote
from uuid import UUID

import httpx

from .config import get_settings

TOKEN_URL = "https://identity.xero.com/connect/token"
INVOICES_URL = "https://api.xero.com/api.xro/2.0/Invoices"


class XeroConfigurationError(RuntimeError):
    pass


def _parse_xero_date(raw_value: str | None) -> date | None:
    if not raw_value:
        return None

    if raw_value.startswith("/Date("):
        milliseconds = raw_value.removeprefix("/Date(").split(")")[0]
        return datetime.fromtimestamp(int(milliseconds) / 1000).date()

    return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).date()


def _parse_xero_datetime(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None

    if raw_value.startswith("/Date("):
        milliseconds = raw_value.removeprefix("/Date(").split(")")[0]
        return datetime.fromtimestamp(int(milliseconds) / 1000)

    return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))


async def fetch_access_token() -> str:
    settings = get_settings()

    if not settings.xero_client_id or not settings.xero_client_secret:
        raise XeroConfigurationError("Missing Xero client credentials.")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "scope": settings.xero_scopes,
            },
            auth=(settings.xero_client_id, settings.xero_client_secret),
        )
        response.raise_for_status()
        payload = response.json()
        return payload["access_token"]


async def fetch_accounts_receivable_invoices() -> list[dict]:
    token = await fetch_access_token()
    where_clause = quote('Type=="ACCREC"&&AmountDue>0')

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            f"{INVOICES_URL}?where={where_clause}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()

    invoices = payload.get("Invoices", [])
    return [
        {
            "invoice_id": UUID(invoice["InvoiceID"]),
            "invoice_number": invoice.get("InvoiceNumber"),
            "contact_name": invoice.get("Contact", {}).get("Name", "Unknown"),
            "status": invoice.get("Status", "UNKNOWN"),
            "currency_code": invoice.get("CurrencyCode"),
            "due_date": _parse_xero_date(invoice.get("DueDateString") or invoice.get("DueDate")),
            "amount_due": invoice.get("AmountDue", 0),
            "amount_paid": invoice.get("AmountPaid", 0),
            "total": invoice.get("Total", 0),
            "updated_date_utc": _parse_xero_datetime(invoice.get("UpdatedDateUTC")),
        }
        for invoice in invoices
    ]
