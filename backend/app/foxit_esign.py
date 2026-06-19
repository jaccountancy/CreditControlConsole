from __future__ import annotations

import base64
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status

from .config import get_settings

FOXIT_TIMEOUT_SECONDS = 60.0


class FoxitESignConfigurationError(RuntimeError):
    pass


def _settings_text(value: str | None) -> str:
    return str(value or "").strip()


def _is_placeholder(value: str) -> bool:
    placeholders = {
        "",
        "replace-me",
        "changeme",
        "change-me",
        "your-client-id",
        "your-client-secret",
        "your-foxit-client-id",
        "your-foxit-client-secret",
    }
    return value.lower() in placeholders


def foxit_esign_configured() -> bool:
    settings = get_settings()
    base_url = _settings_text(settings.foxit_base_url)
    client_id = _settings_text(settings.foxit_client_id)
    client_secret = _settings_text(settings.foxit_client_secret)
    return not _is_placeholder(base_url) and not _is_placeholder(client_id) and not _is_placeholder(client_secret)


def _authorization_header() -> str:
    settings = get_settings()
    client_id = _settings_text(settings.foxit_client_id)
    client_secret = _settings_text(settings.foxit_client_secret)
    token = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _join_url(base_url: str, path: str, params: dict | None = None) -> str:
    clean_base = _settings_text(base_url).rstrip("/")
    clean_path = _settings_text(path)
    if clean_path.startswith("http://") or clean_path.startswith("https://"):
        target = clean_path
    elif clean_path.startswith("/"):
        target = f"{clean_base}{clean_path}"
    else:
        target = f"{clean_base}/{clean_path}"
    if params:
        query = urlencode({key: value for key, value in params.items() if value is not None and value != ""})
        if query:
            separator = "&" if "?" in target else "?"
            target = f"{target}{separator}{query}"
    return target


def _raise_foxit_http_error(response: httpx.Response, action: str) -> None:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    provider_message = ""
    if isinstance(detail, dict):
        provider_message = str(
            detail.get("message")
            or detail.get("detail")
            or detail.get("error_description")
            or detail.get("error")
            or ""
        )
    elif detail:
        provider_message = str(detail)
    message = f"Foxit eSign {action} failed."
    if provider_message:
        message = f"{message} {provider_message}"
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "message": message,
            "status_code": response.status_code,
            "response": detail,
        },
    )


def _extract_external_request_id(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = [
        payload.get("requestId"),
        payload.get("request_id"),
        payload.get("documentId"),
        payload.get("document_id"),
        payload.get("id"),
        payload.get("data", {}).get("id") if isinstance(payload.get("data"), dict) else None,
        payload.get("data", {}).get("requestId") if isinstance(payload.get("data"), dict) else None,
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


async def foxit_esign_send_request(payload: dict) -> dict:
    settings = get_settings()
    if not foxit_esign_configured():
        raise FoxitESignConfigurationError(
            "Foxit eSign is not configured. Add FOXIT_BASE_URL, FOXIT_CLIENT_ID, and FOXIT_CLIENT_SECRET."
        )
    url = _join_url(settings.foxit_base_url or "", settings.foxit_esign_send_path)
    async with httpx.AsyncClient(timeout=FOXIT_TIMEOUT_SECONDS) as client:
        response = await client.post(
            url,
            json=payload,
            headers={
                "Authorization": _authorization_header(),
                "Content-Type": "application/json",
            },
        )
    if response.is_error:
        _raise_foxit_http_error(response, "create request")
    response_payload = response.json() if response.headers.get("content-type", "").lower().startswith("application/json") else {}
    return {
        "raw": response_payload,
        "externalRequestId": _extract_external_request_id(response_payload),
    }


async def foxit_esign_resend_request(request_id: str) -> dict:
    settings = get_settings()
    if not foxit_esign_configured():
        raise FoxitESignConfigurationError(
            "Foxit eSign is not configured. Add FOXIT_BASE_URL, FOXIT_CLIENT_ID, and FOXIT_CLIENT_SECRET."
        )
    path = (settings.foxit_esign_resend_path or "").replace(":requestId", request_id)
    url = _join_url(settings.foxit_base_url or "", path)
    async with httpx.AsyncClient(timeout=FOXIT_TIMEOUT_SECONDS) as client:
        response = await client.post(
            url,
            json={},
            headers={
                "Authorization": _authorization_header(),
                "Content-Type": "application/json",
            },
        )
    if response.is_error:
        _raise_foxit_http_error(response, "resend")
    return response.json() if response.headers.get("content-type", "").lower().startswith("application/json") else {}


async def foxit_esign_cancel_request(request_id: str) -> dict:
    settings = get_settings()
    if not foxit_esign_configured():
        raise FoxitESignConfigurationError(
            "Foxit eSign is not configured. Add FOXIT_BASE_URL, FOXIT_CLIENT_ID, and FOXIT_CLIENT_SECRET."
        )
    path = (settings.foxit_esign_cancel_path or "").replace(":requestId", request_id)
    url = _join_url(settings.foxit_base_url or "", path)
    async with httpx.AsyncClient(timeout=FOXIT_TIMEOUT_SECONDS) as client:
        response = await client.post(
            url,
            json={},
            headers={
                "Authorization": _authorization_header(),
                "Content-Type": "application/json",
            },
        )
    if response.is_error:
        _raise_foxit_http_error(response, "cancel")
    return response.json() if response.headers.get("content-type", "").lower().startswith("application/json") else {}


async def foxit_esign_status_request(request_id: str) -> dict:
    settings = get_settings()
    if not foxit_esign_configured():
        raise FoxitESignConfigurationError(
            "Foxit eSign is not configured. Add FOXIT_BASE_URL, FOXIT_CLIENT_ID, and FOXIT_CLIENT_SECRET."
        )
    path = (settings.foxit_esign_status_path or "").replace(":requestId", request_id)
    url = _join_url(settings.foxit_base_url or "", path)
    async with httpx.AsyncClient(timeout=FOXIT_TIMEOUT_SECONDS) as client:
        response = await client.get(
            url,
            headers={
                "Authorization": _authorization_header(),
                "Content-Type": "application/json",
            },
        )
    if response.is_error:
        _raise_foxit_http_error(response, "status check")
    return response.json() if response.headers.get("content-type", "").lower().startswith("application/json") else {}

