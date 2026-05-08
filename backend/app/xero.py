from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
CONTACTS_URL = "https://api.xero.com/api.xro/2.0/Contacts"
INVOICES_URL = "https://api.xero.com/api.xro/2.0/Invoices"
USERINFO_URL = "https://identity.xero.com/connect/userinfo"


class XeroConfigurationError(RuntimeError):
    pass


def _raise_xero_http_error(response: httpx.Response, action: str) -> None:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "message": f"Xero {action} failed.",
            "status_code": response.status_code,
            "response": detail,
        },
    )


def _iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.startswith("/Date("):
        milliseconds = value.removeprefix("/Date(").split(")")[0]
        return datetime.fromtimestamp(int(milliseconds) / 1000, tz=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _xero_date(value: str | None):
    date_value = _iso_to_datetime(value)
    return None if date_value is None else date_value.date()


async def exchange_code_for_tokens(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.xero_redirect_uri,
            },
            auth=(settings.xero_client_id, settings.xero_client_secret),
        )
        if response.is_error:
            _raise_xero_http_error(response, "token exchange")
        return response.json()


async def refresh_connection(connection_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM xero_connections WHERE id = %s", (connection_id,))
            row = cursor.fetchone()
        connection.commit()

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Missing Xero connection.")

    if row["expires_at"] > utcnow() + timedelta(minutes=2):
        return row

    settings = get_settings()
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": row["refresh_token"],
            },
            auth=(settings.xero_client_id, settings.xero_client_secret),
        )
        if response.is_error:
            _raise_xero_http_error(response, "token refresh")
        payload = response.json()

    expires_at = utcnow() + timedelta(seconds=payload["expires_in"])
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE xero_connections
                SET access_token = %s,
                    refresh_token = %s,
                    expires_at = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    payload["access_token"],
                    payload["refresh_token"],
                    expires_at,
                    utcnow(),
                    connection_id,
                ),
            )
            updated = cursor.fetchone()
        connection.commit()

    return updated


async def fetch_user_profile(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.is_error:
            _raise_xero_http_error(response, "profile fetch")
        return response.json()


async def fetch_connections(access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            CONNECTIONS_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.is_error:
            _raise_xero_http_error(response, "organisation fetch")
        return response.json()


def store_login(profile: dict, token_payload: dict, connections: list[dict]) -> dict:
    if not connections:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No Xero organisations linked.")

    chosen = connections[0]
    tenant_id = chosen["tenantId"]
    expires_at = utcnow() + timedelta(seconds=token_payload["expires_in"])
    email = profile.get("email") or f'{profile.get("sub")}@xero.local'
    full_name = (
        profile.get("name")
        or " ".join(part for part in [profile.get("given_name"), profile.get("family_name")] if part)
        or email
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (email, full_name, last_login_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE
                SET full_name = EXCLUDED.full_name,
                    last_login_at = EXCLUDED.last_login_at,
                    updated_at = %s
                RETURNING *
                """,
                (email, full_name, utcnow(), utcnow()),
            )
            user = cursor.fetchone()

            cursor.execute(
                """
                DELETE FROM xero_connections
                WHERE user_id = %s
                  AND tenant_id <> %s
                """,
                (user["id"], tenant_id),
            )
            cursor.execute(
                """
                INSERT INTO xero_connections (
                    user_id,
                    xero_user_id,
                    tenant_id,
                    tenant_name,
                    tenant_type,
                    access_token,
                    refresh_token,
                    expires_at,
                    scope,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id) DO UPDATE
                SET xero_user_id = EXCLUDED.xero_user_id,
                    user_id = EXCLUDED.user_id,
                    tenant_id = EXCLUDED.tenant_id,
                    tenant_name = EXCLUDED.tenant_name,
                    tenant_type = EXCLUDED.tenant_type,
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    expires_at = EXCLUDED.expires_at,
                    scope = EXCLUDED.scope,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    user["id"],
                    profile.get("sub", ""),
                    tenant_id,
                    chosen.get("tenantName", "Xero Organisation"),
                    chosen.get("tenantType"),
                    token_payload["access_token"],
                    token_payload["refresh_token"],
                    expires_at,
                    token_payload.get("scope", ""),
                    utcnow(),
                ),
            )
            xero_connection = cursor.fetchone()
        connection.commit()

    return {"user": user, "connection": xero_connection}


async def xero_api_get(connection_row: dict, url: str, params: dict | None = None) -> dict:
    connection_row = await refresh_connection(connection_row["id"])

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(
            url,
            params=params,
            headers={
                "Authorization": f'Bearer {connection_row["access_token"]}',
                "xero-tenant-id": connection_row["tenant_id"],
                "Accept": "application/json",
            },
        )
        if response.is_error:
            _raise_xero_http_error(response, "API request")
        return response.json()


async def fetch_contacts_and_invoices(connection_row: dict) -> tuple[list[dict], list[dict]]:
    contacts_payload = await xero_api_get(connection_row, CONTACTS_URL)
    invoices_payload = await xero_api_get(connection_row, INVOICES_URL, params={"where": 'Type=="ACCREC"'})
    return contacts_payload.get("Contacts", []), invoices_payload.get("Invoices", [])


def normalise_contact(contact: dict, tenant_id: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "xero_contact_id": contact["ContactID"],
        "name": contact.get("Name", "Unknown"),
        "email": contact.get("EmailAddress"),
        "phone": (contact.get("Phones") or [{}])[0].get("PhoneNumber"),
        "account_number": contact.get("AccountNumber"),
    }


def normalise_invoice(invoice: dict) -> dict:
    return {
        "xero_invoice_id": invoice["InvoiceID"],
        "xero_contact_id": invoice.get("Contact", {}).get("ContactID"),
        "invoice_number": invoice.get("InvoiceNumber") or invoice["InvoiceID"],
        "status": invoice.get("Status", "UNKNOWN"),
        "due_date": _xero_date(invoice.get("DueDateString") or invoice.get("DueDate")),
        "invoice_date": _xero_date(invoice.get("DateString") or invoice.get("Date")),
        "currency_code": invoice.get("CurrencyCode"),
        "total": invoice.get("Total", 0),
        "amount_due": invoice.get("AmountDue", 0),
        "amount_paid": invoice.get("AmountPaid", 0),
        "xero_updated_at": _iso_to_datetime(invoice.get("UpdatedDateUTC")),
    }
