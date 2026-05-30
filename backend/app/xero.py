import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from uuid import uuid4

import httpx
from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
CONTACTS_URL = "https://api.xero.com/api.xro/2.0/Contacts"
INVOICES_URL = "https://api.xero.com/api.xro/2.0/Invoices"
CREDIT_NOTES_URL = "https://api.xero.com/api.xro/2.0/CreditNotes"
OVERPAYMENTS_URL = "https://api.xero.com/api.xro/2.0/Overpayments"
PAYMENTS_URL = "https://api.xero.com/api.xro/2.0/Payments"
USERINFO_URL = "https://identity.xero.com/connect/userinfo"
XERO_PAGE_SIZE = 100
XERO_PAGE_DELAY_SECONDS = 1.05
XERO_STANDARD_TIMEOUT_SECONDS = 90.0
XERO_API_REQUEST_TIMEOUT_SECONDS = 180.0
XERO_API_PAGE_TIMEOUT_SECONDS = 225
XERO_RATE_LIMIT_RETRIES = 3
XERO_RATE_LIMIT_FALLBACK_DELAY_SECONDS = 65
XERO_RATE_LIMIT_MAX_SLEEP_SECONDS = 360
XERO_HISTORY_SIGNATURE = "By Jenius AI"
XERO_PERMISSION_MESSAGE = (
    "Xero permissions need updating. Reconnect Xero to approve invoice, credit note, allocation, "
    "and contact note write-back access, then try again."
)


class XeroConfigurationError(RuntimeError):
    pass


def _format_delay_seconds(seconds: int) -> str:
    seconds = max(int(seconds or 0), 0)
    if seconds < 60:
        return f"{seconds} seconds"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _xero_validation_summary(detail) -> str:
    messages: list[str] = []
    ignored_messages = {"a validation exception occurred"}

    def add_message(value) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not text or text.lower() in ignored_messages or text in messages:
            return
        messages.append(text)

    def collect(value) -> None:
        if isinstance(value, dict):
            for key in ("Message", "Detail", "Error"):
                add_message(value.get(key))
            for validation_error in value.get("ValidationErrors") or []:
                collect(validation_error)
            for element in value.get("Elements") or []:
                collect(element)
            for key in ("Invoices", "CreditNotes", "Overpayments", "Payments", "Contacts"):
                children = value.get(key) or []
                if isinstance(children, dict):
                    collect(children)
                else:
                    for child in children:
                        collect(child)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(detail)
    if not messages:
        return ""
    return " ".join(messages[:4])


def _xero_rate_limit_headers(response: httpx.Response) -> dict:
    return {
        "retry_after": response.headers.get("Retry-After", ""),
        "x_daylimit_remaining": response.headers.get("X-DayLimit-Remaining", ""),
        "x_minlimit_remaining": response.headers.get("X-MinLimit-Remaining", ""),
        "x_applimit_remaining": response.headers.get("X-AppMinLimit-Remaining", ""),
        "x_rate_limit_problem": response.headers.get("X-Rate-Limit-Problem", ""),
    }


def _raise_xero_http_error(response: httpx.Response, action: str) -> None:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text

    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        rate_limit_headers = _xero_rate_limit_headers(response)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": f"Xero API quota reached while trying to complete {action}. Wait for Xero's limit to reset, then try again.",
                "status_code": response.status_code,
                "retry_after": rate_limit_headers["retry_after"],
                "rate_limit_headers": rate_limit_headers,
                "response": detail,
            },
        )

    auth_header = response.headers.get("WWW-Authenticate", "")
    detail_text = str(detail)
    scope_error_text = f"{auth_header} {detail_text}".lower()
    if response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN) and (
        "insufficient_scope" in scope_error_text
        or "insufficent_scope" in scope_error_text
        or "insufficient scope" in scope_error_text
    ):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": XERO_PERMISSION_MESSAGE,
                "status_code": response.status_code,
                "reconnect_required": True,
                "response": detail,
            },
        )

    message = f"Xero {action} failed."
    validation_summary = _xero_validation_summary(detail)
    if validation_summary:
        message = f"{message} {validation_summary}"

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "message": message,
            "status_code": response.status_code,
            "response": detail,
        },
    )


def _raise_xero_request_error(exc: httpx.RequestError, action: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "message": f"Xero {action} could not be reached.",
            "error": str(exc),
        },
    ) from exc


def _signed_history_note(details: str, limit: int = 4000) -> str:
    note = str(details or "").strip()
    signature = XERO_HISTORY_SIGNATURE
    if not note:
        return signature
    if note.lower().endswith(signature.lower()):
        signed = note
    else:
        signed = f"{note} {signature}"
    if len(signed) <= limit:
        return signed

    suffix = f" {signature}"
    body_limit = max(limit - len(suffix), 0)
    if body_limit <= 0:
        return signature[:limit]
    return f"{signed[:body_limit].rstrip()}{suffix}"


def _iso_to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.startswith("/Date("):
        match = re.search(r"/Date\((-?\d+)", value)
        if match is None:
            return None
        milliseconds = match.group(1)
        return datetime.fromtimestamp(int(milliseconds) / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _xero_date(value: str | None):
    date_value = _iso_to_datetime(value)
    return None if date_value is None else date_value.date()


async def exchange_code_for_tokens(code: str) -> dict:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.xero_redirect_uri,
                },
                auth=(settings.xero_client_id, settings.xero_client_secret),
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "token exchange")
        if response.is_error:
            _raise_xero_http_error(response, "token exchange")
        return response.json()


def _connection_refreshed_by_another_request(connection_id: str, previous_refresh_token: str) -> dict | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM xero_connections WHERE id = %s", (connection_id,))
            row = cursor.fetchone()
        connection.commit()

    if (
        row
        and row.get("refresh_token")
        and row["refresh_token"] != previous_refresh_token
        and row["expires_at"] > utcnow()
    ):
        return row
    return None


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
    previous_refresh_token = row["refresh_token"]
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": row["refresh_token"],
                },
                auth=(settings.xero_client_id, settings.xero_client_secret),
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "token refresh")
        if response.is_error:
            refreshed = _connection_refreshed_by_another_request(connection_id, previous_refresh_token)
            if refreshed is not None:
                return refreshed
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
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(
                USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "profile fetch")
        if response.is_error:
            _raise_xero_http_error(response, "profile fetch")
        return response.json()


async def fetch_connections(access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(
                CONNECTIONS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "organisation fetch")
        if response.is_error:
            _raise_xero_http_error(response, "organisation fetch")
        return response.json()


def _parse_xero_connection_timestamp(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = re.sub(r"(\.\d{6})\d+(?=(Z|[+-]\d\d:\d\d)?$)", r"\1", str(value))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _choose_xero_connection(connections: list[dict]) -> dict:
    def connection_rank(index_and_connection: tuple[int, dict]) -> tuple[int, datetime, datetime, int]:
        index, connection = index_and_connection
        tenant_type = str(connection.get("tenantType") or "").upper()
        accounting_tenant = 1 if tenant_type == "ORGANISATION" else 0
        updated_at = _parse_xero_connection_timestamp(connection.get("updatedDateUtc"))
        created_at = _parse_xero_connection_timestamp(connection.get("createdDateUtc"))
        return accounting_tenant, updated_at, created_at, -index

    return max(enumerate(connections), key=connection_rank)[1]


def store_login(profile: dict, token_payload: dict, connections: list[dict]) -> dict:
    if not connections:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No Xero organisations linked.")

    missing_token_fields = [
        field
        for field in ("access_token", "refresh_token", "expires_in")
        if not token_payload.get(field)
    ]
    if missing_token_fields:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Xero token response was missing required fields.",
                "missing": missing_token_fields,
            },
        )

    chosen = _choose_xero_connection(connections)
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
                   OR tenant_id = %s
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


def _modified_since_header_value(modified_since: datetime | None) -> str | None:
    if modified_since is None:
        return None
    if modified_since.tzinfo is None:
        modified_since = modified_since.replace(tzinfo=timezone.utc)
    return format_datetime(modified_since.astimezone(timezone.utc), usegmt=True)


def _retry_after_delay_seconds(retry_after: str | None) -> int | None:
    if not retry_after:
        return None
    try:
        return max(0, int(retry_after))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(retry_after)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds()))


async def xero_api_get(
    connection_row: dict,
    url: str,
    params: dict | None = None,
    modified_since: datetime | None = None,
    on_response=None,
) -> dict:
    connection_row = await refresh_connection(connection_row["id"])
    headers = {
        "Authorization": f'Bearer {connection_row["access_token"]}',
        "xero-tenant-id": connection_row["tenant_id"],
        "Accept": "application/json",
    }
    modified_since_header = _modified_since_header_value(modified_since)
    if modified_since_header:
        headers["If-Modified-Since"] = modified_since_header

    started = time.monotonic()
    async with httpx.AsyncClient(timeout=XERO_API_REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = await client.get(
                url,
                params=params,
                headers=headers,
            )
        except httpx.RequestError as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if on_response is not None:
                on_response({
                    "status_code": None,
                    "elapsed_ms": elapsed_ms,
                    "rate_limit_headers": {},
                    "error": exc.__class__.__name__,
                    "error_message": str(exc),
                })
            _raise_xero_request_error(exc, "API request")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        rate_limit_headers = _xero_rate_limit_headers(response)
        if on_response is not None:
            on_response({
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
                "rate_limit_headers": rate_limit_headers,
            })
        if response.is_error:
            _raise_xero_http_error(response, "API request")
        if response.status_code == status.HTTP_304_NOT_MODIFIED or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "Xero API request returned an invalid response. Try the sync again; if it repeats, reconnect Xero.",
                    "status_code": response.status_code,
                    "response": response.text[:1000],
                },
            ) from exc


async def create_history_record(connection_row: dict, resource: str, resource_id: str, details: str) -> dict:
    resource_urls = {
        "Contacts": CONTACTS_URL,
        "Invoices": INVOICES_URL,
    }
    base_url = resource_urls.get(resource)
    if base_url is None:
        raise ValueError(f"Unsupported Xero history resource: {resource}")

    connection_row = await refresh_connection(connection_row["id"])
    note_body = _signed_history_note(details)
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.put(
                f"{base_url}/{resource_id}/History",
                headers={
                    "Authorization": f'Bearer {connection_row["access_token"]}',
                    "xero-tenant-id": connection_row["tenant_id"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(uuid4()),
                },
                json={"HistoryRecords": [{"Details": note_body}]},
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "history note creation")
        if response.is_error:
            _raise_xero_http_error(response, "history note creation")
        if not response.content:
            return {}
        return response.json()


async def create_sales_invoice(connection_row: dict, invoice_payload: dict, idempotency_key: str | None = None) -> dict:
    connection_row = await refresh_connection(connection_row["id"])
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                INVOICES_URL,
                headers={
                    "Authorization": f'Bearer {connection_row["access_token"]}',
                    "xero-tenant-id": connection_row["tenant_id"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key or str(uuid4()),
                },
                json={"Invoices": [invoice_payload]},
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "invoice creation")
        if response.is_error:
            _raise_xero_http_error(response, "invoice creation")
        if not response.content:
            return {}
        return response.json()


async def create_credit_note(connection_row: dict, credit_note_payload: dict) -> dict:
    connection_row = await refresh_connection(connection_row["id"])
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.post(
                CREDIT_NOTES_URL,
                headers={
                    "Authorization": f'Bearer {connection_row["access_token"]}',
                    "xero-tenant-id": connection_row["tenant_id"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(uuid4()),
                },
                json={"CreditNotes": [credit_note_payload]},
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "credit note creation")
        if response.is_error:
            _raise_xero_http_error(response, "credit note creation")
        if not response.content:
            return {}
        return response.json()


async def allocate_credit_note(connection_row: dict, credit_note_id: str, allocation_payload: dict) -> dict:
    connection_row = await refresh_connection(connection_row["id"])
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.put(
                f"{CREDIT_NOTES_URL}/{credit_note_id}/Allocations",
                headers={
                    "Authorization": f'Bearer {connection_row["access_token"]}',
                    "xero-tenant-id": connection_row["tenant_id"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(uuid4()),
                },
                json={"Allocations": [allocation_payload]},
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "credit note allocation")
        if response.is_error:
            _raise_xero_http_error(response, "credit note allocation")
        if not response.content:
            return {}
        return response.json()


async def allocate_overpayment(connection_row: dict, overpayment_id: str, allocation_payload: dict) -> dict:
    connection_row = await refresh_connection(connection_row["id"])
    async with httpx.AsyncClient(timeout=XERO_STANDARD_TIMEOUT_SECONDS) as client:
        try:
            response = await client.put(
                f"{OVERPAYMENTS_URL}/{overpayment_id}/Allocations",
                headers={
                    "Authorization": f'Bearer {connection_row["access_token"]}',
                    "xero-tenant-id": connection_row["tenant_id"],
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Idempotency-Key": str(uuid4()),
                },
                json={"Allocations": [allocation_payload]},
            )
        except httpx.RequestError as exc:
            _raise_xero_request_error(exc, "overpayment allocation")
        if response.is_error:
            _raise_xero_http_error(response, "overpayment allocation")
        if not response.content:
            return {}
        return response.json()


async def fetch_paginated_collection(
    connection_row: dict,
    url: str,
    collection_key: str,
    params: dict | None = None,
    max_pages: int | None = None,
    start_page: int = 1,
    on_page=None,
    on_batch=None,
    on_retry=None,
    on_request=None,
    modified_since: datetime | None = None,
    collect_records: bool = True,
    initial_records: int = 0,
) -> list[dict]:
    records: list[dict] = []
    total_records = max(int(initial_records or 0), 0)
    page = max(int(start_page or 1), 1)
    rate_limit_retries = 0
    while True:
        if max_pages is not None and page > max_pages:
            return records

        page_started = time.monotonic()
        last_response: dict = {}

        def capture_response(info: dict) -> None:
            last_response.update(info)

        try:
            payload = await asyncio.wait_for(
                xero_api_get(
                    connection_row,
                    url,
                    params={**(params or {}), "page": page},
                    modified_since=modified_since,
                    on_response=capture_response,
                ),
                timeout=XERO_API_PAGE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            wall_ms = int((time.monotonic() - page_started) * 1000)
            if on_request is not None:
                on_request({
                    "collection": collection_key,
                    "url": url,
                    "page": page,
                    "outcome": "timeout",
                    "wall_ms": wall_ms,
                    "timeout_seconds": XERO_API_PAGE_TIMEOUT_SECONDS,
                    "records_so_far": total_records,
                    "retry_count": rate_limit_retries,
                    **last_response,
                })
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": (
                        f"Xero {collection_key} page {page} did not respond within "
                        f"{XERO_API_PAGE_TIMEOUT_SECONDS} seconds. Try syncing again; "
                        "if it repeats, reconnect Xero and check Xero service status."
                    ),
                    "timeout_seconds": XERO_API_PAGE_TIMEOUT_SECONDS,
                    "wall_ms": wall_ms,
                    "page": page,
                    "collection": collection_key,
                    "records_so_far": total_records,
                },
            ) from exc
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            wall_ms = int((time.monotonic() - page_started) * 1000)
            if detail.get("status_code") == status.HTTP_429_TOO_MANY_REQUESTS and rate_limit_retries < XERO_RATE_LIMIT_RETRIES:
                rate_limit_retries += 1
                retry_after = detail.get("retry_after")
                parsed_delay = _retry_after_delay_seconds(retry_after)
                delay_seconds = parsed_delay if parsed_delay is not None else XERO_RATE_LIMIT_FALLBACK_DELAY_SECONDS
                if on_request is not None:
                    on_request({
                        "collection": collection_key,
                        "url": url,
                        "page": page,
                        "outcome": "rate_limited",
                        "wall_ms": wall_ms,
                        "retry_after_seconds": delay_seconds,
                        "retry_count": rate_limit_retries,
                        "records_so_far": total_records,
                        **last_response,
                    })
                if delay_seconds > XERO_RATE_LIMIT_MAX_SLEEP_SECONDS:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail={
                            **detail,
                            "message": (
                                f"Xero rate-limited the {collection_key} request and asked the app to wait "
                                f"about {_format_delay_seconds(delay_seconds)}. Try syncing again after Xero's limit resets."
                            ),
                            "retry_after_seconds": delay_seconds,
                            "page": page,
                            "collection": collection_key,
                        },
                    ) from exc
                if on_retry is not None:
                    on_retry(page, total_records, delay_seconds, rate_limit_retries)
                await asyncio.sleep(max(delay_seconds, XERO_PAGE_DELAY_SECONDS))
                continue
            if on_request is not None:
                on_request({
                    "collection": collection_key,
                    "url": url,
                    "page": page,
                    "outcome": "error",
                    "wall_ms": wall_ms,
                    "retry_count": rate_limit_retries,
                    "records_so_far": total_records,
                    "error_status_code": detail.get("status_code"),
                    "error_message": detail.get("message"),
                    **last_response,
                })
            raise
        rate_limit_retries = 0
        batch = payload.get(collection_key, [])
        total_records += len(batch)
        if collect_records:
            records.extend(batch)
        wall_ms = int((time.monotonic() - page_started) * 1000)
        if on_request is not None:
            on_request({
                "collection": collection_key,
                "url": url,
                "page": page,
                "outcome": "ok",
                "wall_ms": wall_ms,
                "batch_size": len(batch),
                "records_so_far": total_records,
                **last_response,
            })
        if on_page is not None:
            on_page(page, total_records, len(batch))
        if on_batch is not None:
            on_batch(page, batch, total_records)
        if len(batch) < XERO_PAGE_SIZE:
            return records
        page += 1
        await asyncio.sleep(XERO_PAGE_DELAY_SECONDS)


async def fetch_contacts_and_invoices(connection_row: dict) -> tuple[list[dict], list[dict]]:
    contacts = await fetch_paginated_collection(connection_row, CONTACTS_URL, "Contacts")
    invoices = await fetch_paginated_collection(
        connection_row,
        INVOICES_URL,
        "Invoices",
        params={"where": 'Type=="ACCREC"&&Status!="VOIDED"&&Status!="DELETED"'},
    )
    return contacts, invoices


def _xero_money_value(value) -> float:
    try:
        return round(float(value if value is not None else 0), 2)
    except (TypeError, ValueError):
        return 0.0


def normalise_contact(contact: dict, tenant_id: str) -> dict:
    contact_people = []
    for person in contact.get("ContactPersons") or []:
        full_name = " ".join(
            part
            for part in [person.get("FirstName"), person.get("LastName")]
            if part
        ).strip()
        if not full_name and person.get("EmailAddress"):
            full_name = person["EmailAddress"]
        if not full_name:
            continue
        contact_people.append(
            {
                "name": full_name,
                "email": person.get("EmailAddress") or "",
                "includeInEmails": bool(person.get("IncludeInEmails")),
            }
        )

    primary_person = " ".join(
        part
        for part in [contact.get("FirstName"), contact.get("LastName")]
        if part
    ).strip()
    if not primary_person and contact_people:
        primary_person = contact_people[0]["name"]

    phone = ""
    for phone_item in contact.get("Phones") or []:
        phone_number = phone_item.get("PhoneNumber")
        if phone_number:
            area_code = phone_item.get("PhoneAreaCode")
            country_code = phone_item.get("PhoneCountryCode")
            phone = " ".join(part for part in [country_code, area_code, phone_number] if part)
            break

    addresses = []
    for address in contact.get("Addresses") or []:
        lines = [
            address.get("AddressLine1"),
            address.get("AddressLine2"),
            address.get("AddressLine3"),
            address.get("AddressLine4"),
            address.get("City"),
            address.get("Region"),
            address.get("PostalCode"),
            address.get("Country"),
        ]
        formatted = ", ".join(part for part in lines if part)
        if formatted:
            addresses.append(
                {
                    "type": address.get("AddressType") or "",
                    "address": formatted,
                }
            )

    receivable = ((contact.get("Balances") or {}).get("AccountsReceivable") or {})
    return {
        "tenant_id": tenant_id,
        "xero_contact_id": contact["ContactID"],
        "name": contact.get("Name", "Unknown"),
        "email": contact.get("EmailAddress"),
        "phone": phone,
        "account_number": contact.get("AccountNumber"),
        "primary_person": primary_person,
        "contact_people": contact_people,
        "addresses": addresses,
        "total_due": _xero_money_value(receivable.get("Outstanding")),
        "overdue_amount": _xero_money_value(receivable.get("Overdue")),
    }


def _normalise_invoice_line_items(invoice: dict) -> tuple[str, list[dict]]:
    line_items = []
    descriptions = []
    for item in invoice.get("LineItems") or []:
        description = str(item.get("Description") or item.get("Item", {}).get("Name") or "").strip()
        if description:
            descriptions.append(description)
        line_items.append(
            {
                "description": description,
                "quantity": item.get("Quantity"),
                "unitAmount": item.get("UnitAmount"),
                "lineAmount": item.get("LineAmount"),
                "accountCode": item.get("AccountCode"),
            }
        )
    return "\n".join(descriptions), line_items


def normalise_invoice(invoice: dict) -> dict:
    description, line_items = _normalise_invoice_line_items(invoice)
    return {
        "xero_invoice_id": invoice["InvoiceID"],
        "xero_contact_id": invoice.get("Contact", {}).get("ContactID"),
        "invoice_number": invoice.get("InvoiceNumber") or invoice["InvoiceID"],
        "status": invoice.get("Status", "UNKNOWN"),
        "due_date": _xero_date(invoice.get("DueDateString") or invoice.get("DueDate")),
        "invoice_date": _xero_date(invoice.get("DateString") or invoice.get("Date")),
        "description": description,
        "line_items": line_items,
        "currency_code": invoice.get("CurrencyCode"),
        "total": invoice.get("Total", 0),
        "amount_due": invoice.get("AmountDue", 0),
        "amount_paid": invoice.get("AmountPaid", 0),
        "xero_updated_at": _iso_to_datetime(invoice.get("UpdatedDateUTC")),
    }


def normalise_payment(payment: dict) -> dict:
    invoice = payment.get("Invoice") or {}
    contact = invoice.get("Contact") or payment.get("Contact") or {}
    account = payment.get("Account") or {}
    return {
        "xero_payment_id": payment.get("PaymentID"),
        "xero_contact_id": contact.get("ContactID"),
        "xero_invoice_id": invoice.get("InvoiceID"),
        "invoice_number": invoice.get("InvoiceNumber") or "",
        "invoice_type": invoice.get("Type") or "",
        "payment_date": _xero_date(payment.get("DateString") or payment.get("Date")),
        "amount": payment.get("Amount", 0),
        "currency_code": payment.get("CurrencyCode") or invoice.get("CurrencyCode"),
        "reference": payment.get("Reference") or "",
        "status": payment.get("Status") or "",
        "account_name": account.get("Name") or account.get("Code") or "",
        "raw": payment,
    }
