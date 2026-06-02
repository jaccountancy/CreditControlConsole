import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow
from .security import decrypt_secret, encrypt_secret, random_token

IGNITION_PAGE_LIMIT = 1000
IGNITION_FALLBACK_PAGE_LIMIT = 250
IGNITION_TIMEOUT_SECONDS = 90.0
IGNITION_DATASETS = (
    ("clients", "/reporting/clients"),
    ("contacts", "/reporting/contacts"),
    ("services", "/reporting/services"),
    ("proposals", "/reporting/proposals"),
    ("invoices", "/reporting/invoices"),
    ("payments", "/reporting/payments"),
    ("collections", "/reporting/collections"),
    ("deals", "/reporting/deals"),
    ("deal_stages", "/reporting/deal_stages"),
)


class IgnitionConfigurationError(RuntimeError):
    pass


def _settings_text(value: str | None) -> str:
    return str(value or "").strip()


def _ignition_client_id() -> str:
    return _settings_text(get_settings().ignition_client_id)


def _ignition_client_secret() -> str:
    return _settings_text(get_settings().ignition_client_secret)


def _ignition_scopes() -> str:
    return _settings_text(get_settings().ignition_scopes) or "reporting"


def ignition_redirect_uri() -> str:
    settings = get_settings()
    configured = _settings_text(settings.ignition_redirect_uri) or _settings_text(settings.ignition_redirect_url)
    return configured or f"{settings.base_url.rstrip('/')}/api/ignition/callback"


def ignition_oauth_configured() -> bool:
    placeholders = {"", "replace-me", "changeme", "change-me", "your-client-id", "your-client-secret"}
    client_id = _ignition_client_id()
    client_secret = _ignition_client_secret()
    return client_id.lower() not in placeholders and client_secret.lower() not in placeholders


def create_pkce_verifier() -> str:
    return random_token(48)[:96]


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def ignition_authorize_url(state_token: str, code_verifier: str) -> str:
    settings = get_settings()
    if not ignition_oauth_configured():
        raise IgnitionConfigurationError(
            "Ignition OAuth is not configured. Add real IGNITION_CLIENT_ID and IGNITION_CLIENT_SECRET values, "
            "then set IGNITION_REDIRECT_URI to the exact callback registered in Ignition Developer Hub."
        )
    authorize_url = _settings_text(settings.ignition_authorize_url)
    query = urlencode(
        {
            "client_id": _ignition_client_id(),
            "redirect_uri": ignition_redirect_uri(),
            "response_type": "code",
            "scope": _ignition_scopes(),
            "state": state_token,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{authorize_url}?{query}"


def _token_secret_label(user_id: str, name: str) -> str:
    return f"ignition:{user_id}:{name}"


def _raise_ignition_http_error(response: httpx.Response, action: str) -> None:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    provider_message = ""
    if isinstance(detail, dict):
        errors = detail.get("errors")
        if isinstance(errors, list) and errors:
            first_error = errors[0]
            if isinstance(first_error, dict):
                provider_message = str(first_error.get("detail") or first_error.get("message") or "")
            else:
                provider_message = str(first_error or "")
        provider_message = provider_message or str(
            detail.get("detail")
            or detail.get("error_description")
            or detail.get("message")
            or detail.get("error")
            or ""
        )
    elif detail:
        provider_message = str(detail)
    message = f"Ignition {action} failed."
    if provider_message:
        message = f"{message} {provider_message}"
    if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Ignition API rate limit reached. Wait until the hourly allowance resets, then sync again.",
                "status_code": response.status_code,
                "rate_limit_limit": response.headers.get("X-RateLimit-Limit", ""),
                "rate_limit_reset": response.headers.get("X-RateLimit-Reset", ""),
                "response": detail,
            },
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "message": message,
            "status_code": response.status_code,
            "response": detail,
        },
    )


async def exchange_ignition_code_for_tokens(code: str, code_verifier: str) -> dict:
    settings = get_settings()
    if not ignition_oauth_configured():
        raise IgnitionConfigurationError("Ignition OAuth is not configured.")
    token_url = _settings_text(settings.ignition_token_url)
    async with httpx.AsyncClient(timeout=IGNITION_TIMEOUT_SECONDS) as client:
        response = await client.post(
            token_url,
            data={
                "client_id": _ignition_client_id(),
                "client_secret": _ignition_client_secret(),
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": ignition_redirect_uri(),
            },
        )
    if response.is_error:
        _raise_ignition_http_error(response, "token exchange")
    return response.json()


def store_ignition_connection(user: dict, token_payload: dict, practice: dict | None = None) -> dict:
    required = [field for field in ("access_token", "refresh_token") if not token_payload.get(field)]
    if required:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Ignition token response missing: {', '.join(required)}")
    expires_in = int(token_payload.get("expires_in") or 3600)
    expires_at = utcnow() + timedelta(seconds=max(expires_in, 60))
    practice = practice or {}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ignition_connections (
                    user_id, practice_id, practice_name, access_token, refresh_token,
                    expires_at, scope, status, error_message, connected_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'connected', '', %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET practice_id = EXCLUDED.practice_id,
                    practice_name = EXCLUDED.practice_name,
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    expires_at = EXCLUDED.expires_at,
                    scope = EXCLUDED.scope,
                    status = 'connected',
                    error_message = '',
                    connected_at = EXCLUDED.connected_at,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    user["id"],
                    str(practice.get("id") or ""),
                    practice.get("name") or "",
                    encrypt_secret(token_payload["access_token"], _token_secret_label(user["id"], "access")),
                    encrypt_secret(token_payload["refresh_token"], _token_secret_label(user["id"], "refresh")),
                    expires_at,
                    token_payload.get("scope") or _ignition_scopes(),
                    utcnow(),
                    utcnow(),
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def get_ignition_connection_for_user(user_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM ignition_connections WHERE user_id = %s", (user_id,))
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ignition is not connected.")
    return row


async def refresh_ignition_connection(connection_row: dict) -> dict:
    user_id = str(connection_row["user_id"])
    latest = get_ignition_connection_for_user(user_id)
    if latest and latest.get("updated_at") != connection_row.get("updated_at"):
        connection_row = latest
    if connection_row["expires_at"] > utcnow() + timedelta(minutes=3):
        return {
            **connection_row,
            "access_token_plain": decrypt_secret(connection_row["access_token"], _token_secret_label(user_id, "access")),
            "refresh_token_plain": decrypt_secret(connection_row["refresh_token"], _token_secret_label(user_id, "refresh")),
        }
    settings = get_settings()
    refresh_token = decrypt_secret(connection_row["refresh_token"], _token_secret_label(user_id, "refresh"))
    token_url = _settings_text(settings.ignition_token_url)
    async with httpx.AsyncClient(timeout=IGNITION_TIMEOUT_SECONDS) as client:
        response = await client.post(
            token_url,
            data={
                "client_id": _ignition_client_id(),
                "client_secret": _ignition_client_secret(),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    if response.is_error:
        _raise_ignition_http_error(response, "token refresh")
    payload = response.json()
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh_token
    updated = store_ignition_connection({"id": user_id}, payload, {"id": connection_row.get("practice_id"), "name": connection_row.get("practice_name")})
    return {
        **updated,
        "access_token_plain": payload["access_token"],
        "refresh_token_plain": payload["refresh_token"],
    }


async def ignition_api_get(connection_row: dict, endpoint: str, params: dict | None = None) -> dict:
    settings = get_settings()
    connection_row = await refresh_ignition_connection(connection_row)
    url = f"{_settings_text(settings.ignition_api_base_url).rstrip('/')}{endpoint}"
    async with httpx.AsyncClient(timeout=IGNITION_TIMEOUT_SECONDS) as client:
        response = await client.get(
            url,
            params=params or {},
            headers={"Authorization": f"Bearer {connection_row['access_token_plain']}"},
        )
    if response.is_error:
        _raise_ignition_http_error(response, f"request to {endpoint}")
    return response.json()


def _ignition_modified_since_param(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def fetch_ignition_collection(connection_row: dict, endpoint: str, modified_since: datetime | None = None) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    cursor = None
    last_meta: dict = {}
    page_limit = IGNITION_PAGE_LIMIT
    page_count = 0
    max_pages = 2000
    empty_page_streak = 0
    seen_cursors: set[str] = set()
    while True:
        page_count += 1
        if page_count > max_pages:
            break
        params = {"limit": page_limit}
        if modified_since is not None:
            params["updated_since"] = _ignition_modified_since_param(modified_since)
        if cursor:
            params["cursor"] = cursor
        try:
            payload = await ignition_api_get(connection_row, endpoint, params)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            provider_status = int(detail.get("status_code") or 0)
            if page_limit != IGNITION_FALLBACK_PAGE_LIMIT and provider_status in (status.HTTP_400_BAD_REQUEST, status.HTTP_422_UNPROCESSABLE_ENTITY):
                page_limit = IGNITION_FALLBACK_PAGE_LIMIT
                params["limit"] = page_limit
                payload = await ignition_api_get(connection_row, endpoint, params)
            else:
                raise
        batch = payload.get("data") or []
        if not isinstance(batch, list):
            batch = []
        rows.extend(batch)
        empty_page_streak = empty_page_streak + 1 if not batch else 0
        last_meta = payload.get("meta") or {}
        pagination = last_meta.get("pagination") or {}
        has_more = bool(pagination.get("has_more"))
        if not has_more:
            break
        next_cursor = str(pagination.get("next_cursor") or "").strip()
        if not next_cursor:
            break
        if empty_page_streak >= 3:
            break
        if next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return rows, last_meta
