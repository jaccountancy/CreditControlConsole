import base64
import csv
import hashlib
import io
import json
import logging
import re
import secrets
import string
import smtplib
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import formataddr
from functools import lru_cache
from uuid import UUID, uuid4
from xml.etree import ElementTree as ET

import httpx
from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow
from .security import decrypt_secret, encrypt_secret
from .services import get_xero_connection_for_user, gmail_connection_for_user, refresh_gmail_connection
from .usage_metrics import estimate_openai_cost_usd, infer_openai_feature_page, parse_openai_usage_tokens, record_usage_event
from .xero import create_sales_invoice, fetch_invoice_pdf

try:
    from lxml import etree as LET
except Exception:  # pragma: no cover - optional dependency guard
    LET = None

CH_API_KEY_LABEL = "ch:api_key"
CH_PRESENTER_AUTH_LABEL = "ch:presenter_auth"
CH_COMPANY_AUTH_LABEL = "ch:company_auth"
VALID_ENVIRONMENTS = {"sandbox", "production"}
MASK_VISIBLE_CHARS = 4

VALID_INTERNAL_STATUSES = {
    "active",
    "inactive",
    "paused",
    "do_not_file",
    "missing_information",
    "ready_to_file",
}
VALID_FILING_AUTHORITY_STATUSES = {"pending", "authorised", "expired", "revoked"}
CH_WORKFLOW_REVIEW_SECTIONS = (
    "company",
    "people",
    "address",
    "sic",
    "capital",
    "authority",
)

CLIENT_IMPORT_HEADER_ALIASES = {
    "client_name": {"client name", "client", "customer", "customer name"},
    "client_id": {"client id", "client reference", "client ref", "reference", "ref", "bm id", "bm client id"},
    "company_name": {"company name", "company", "registered name", "limited company", "ltd name"},
    "company_number": {"company number", "company no", "company no.", "crn", "registration number", "companies house number", "ch number"},
    "auth_code": {
        "authentication code",
        "auth code",
        "auth",
        "ch auth code",
        "ch authentication code",
        "company authentication code",
        "companies house authentication code",
    },
    "contact_email": {"contact email", "email", "primary email", "email address", "client email", "client e-mail"},
    "contact_phone": {
        "contact phone",
        "phone",
        "telephone",
        "phone number",
        "client telephone",
        "client telephone number",
        "client phone",
        "client phone number",
    },
    "client_address": {"client address", "address", "postal address", "client postal address"},
    "assigned_staff": {"assigned staff", "assigned staff member", "staff", "owner", "manager", "account manager"},
    "notes": {"notes", "note", "internal notes", "comment"},
    "company_type": {"company type", "type", "legal type", "entity type"},
    "period_end": {"confirmation statement period end", "statement period end", "made up to", "period end", "confirmation period end"},
    "period_start": {"confirmation statement period start", "statement period start", "period start"},
    "due_date": {"due date", "next due date", "confirmation due date", "next confirmation due"},
    "manager_reference": {"client manager", "manager reference", "relationship manager", "manager ref", "portfolio manager"},
}
CLIENT_IMPORT_PERIOD_END_COLUMN_INDEX = 273  # Excel column JN
CLIENT_IMPORT_DEADLINE_COLUMN_INDEX = 274  # Excel column JO

COMPANY_NUMBER_RE = re.compile(r"^[A-Z0-9]{1,2}\d{6,}$|^\d{8}$|^[A-Z]{2}\d{6}$")
MAX_COMPANIES_HOUSE_SYNC_BATCH = 500
AUTO_SYNC_INTERVAL_SECONDS = 30 * 60
AUTO_SYNC_MIN_GAP = timedelta(hours=24)
CH_WORKFLOW_DEFAULT_BCC_EMAIL = "fmfhdkgaptpyubgms@accountancymanager.co.uk"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
COMPANIES_HOUSE_XML_GATEWAY_URL = "https://xmlgw.companieshouse.gov.uk/v1-0/xmlgw/Gateway"
GOVTALK_NS = "http://www.govtalk.gov.uk/CM/envelope"
CH_HEADER_NS = "http://xmlgw.companieshouse.gov.uk/Header"
CH_FORMS_NS = "http://xmlgw.companieshouse.gov.uk"
CH_GATEWAY_MAX_ATTEMPTS = 4
CH_GATEWAY_BACKOFF_SECONDS = 1.5
CH_GATEWAY_REQUEST_TIMEOUT_SECONDS = 25.0
CH_GATEWAY_MAX_ELAPSED_SECONDS = 90.0
CH_XSD_VALIDATION_ENABLED = True
CH_FORM_SUBMISSION_XSD_URL = "http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/FormSubmission-v2-11.xsd"
CH_COMPANY_SNAPSHOT_CACHE_TTL = timedelta(hours=12)

logger = logging.getLogger(__name__)
_CH_SYNC_LOCK = threading.Lock()
_CH_AUTO_SYNC_THREAD: threading.Thread | None = None
_CH_AUTO_SYNC_STOP = threading.Event()


def _mask(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= MASK_VISIBLE_CHARS:
        return "•" * len(text)
    return "•" * (len(text) - MASK_VISIBLE_CHARS) + text[-MASK_VISIBLE_CHARS:]


def _ch_md5_auth_value(value: str) -> str:
    return hashlib.md5((value or "").strip().encode("utf-8")).hexdigest().upper()


def _ch_auth_value(method: str, presenter_auth: str) -> str:
    if (method or "").lower() == "clear":
        return (presenter_auth or "").strip()
    return _ch_md5_auth_value(presenter_auth)


def _coerce_decimal(value, field: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{field}' must be a number.",
        ) from exc


def _coerce_settings_amount(value, field: str) -> Decimal:
    try:
        return _coerce_decimal(value, field).quantize(Decimal("0.01"))
    except HTTPException:
        raise
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{field}' in Companies House settings must be a number.",
        ) from exc


def _compact_credential(value: object) -> str:
    """Preserve full credential content; trim only leading/trailing whitespace."""
    return str(value or "").strip()


def _decrypt_setting_secret(encrypted_value: object, label: str) -> str:
    encrypted_text = _xml_text(encrypted_value)
    if not encrypted_text:
        return ""
    try:
        return decrypt_secret(encrypted_text, label)
    except Exception:
        logger.exception("Unable to decrypt Companies House secret for label %s", label)
        return ""


def configured_presenter_id(settings_row: dict | None = None) -> str:
    env_value = _compact_credential(get_settings().companies_house_presenter_id)
    if env_value:
        return env_value
    row = settings_row if isinstance(settings_row, dict) else _ensure_settings_row()
    return _compact_credential(row.get("presenter_id"))


def configured_presenter_auth(settings_row: dict | None = None) -> str:
    env_value = _compact_credential(get_settings().companies_house_presenter_auth)
    if env_value:
        return env_value
    row = settings_row if isinstance(settings_row, dict) else _ensure_settings_row()
    return _compact_credential(_decrypt_setting_secret(row.get("presenter_auth_encrypted"), CH_PRESENTER_AUTH_LABEL))


def configured_api_key(settings_row: dict | None = None) -> str:
    env_value = _validated_companies_house_api_key(_compact_credential(get_settings().companies_house_api_key))
    if env_value:
        return env_value
    row = settings_row if isinstance(settings_row, dict) else _ensure_settings_row()
    decrypted = _compact_credential(_decrypt_setting_secret(row.get("api_key_encrypted"), CH_API_KEY_LABEL))
    return _validated_companies_house_api_key(decrypted)


def configured_package_reference() -> str:
    return _compact_credential(get_settings().companies_house_package_reference)


def _ch_auth_method() -> str:
    method = (get_settings().companies_house_auth_method or "").strip()
    if method.upper() in {"MD5", "CHMD5"}:
        return method.upper()
    if method.lower() == "clear":
        return "clear"
    return "MD5"


def _load_settings_row() -> dict | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM ch_settings WHERE singleton_id = 1")
            row = cursor.fetchone()
        connection.commit()
    return row


def _ensure_settings_row() -> dict:
    row = _load_settings_row()
    if row is not None:
        return row
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ch_settings (singleton_id)
                VALUES (1)
                ON CONFLICT (singleton_id) DO NOTHING
                RETURNING *
                """
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute("SELECT * FROM ch_settings WHERE singleton_id = 1")
                row = cursor.fetchone()
        connection.commit()
    return row


def _serialise(row: dict) -> dict:
    settings = get_settings()
    environment = row.get("environment") or settings.companies_house_environment or "sandbox"
    api_base = (
        settings.companies_house_production_api_base
        if environment == "production"
        else settings.companies_house_sandbox_api_base
    )
    api_key = configured_api_key(row)
    presenter_id = configured_presenter_id(row)
    presenter_auth = configured_presenter_auth(row)
    return {
        "environment": environment,
        "apiBaseUrl": api_base,
        "apiKey": "",
        "apiKeyHint": _mask(api_key) if api_key else "Not configured",
        "apiKeyConfigured": bool(api_key),
        "presenterId": presenter_id,
        "presenterAuth": "",
        "presenterAuthHint": _mask(presenter_auth) if presenter_auth else "Not configured",
        "presenterAuthConfigured": bool(presenter_auth),
        "creditAccountNumber": row.get("credit_account_number") or "",
        "xeroInvoiceAccountCode": row.get("xero_invoice_account_code") or "",
        "xeroInvoiceItemCode": row.get("xero_invoice_item_code") or "",
        "xeroInvoiceDescription": row.get("xero_invoice_description") or "",
        "xeroInvoiceUnitAmount": float(row.get("xero_invoice_unit_amount") or 0),
        "xeroInvoiceTaxType": row.get("xero_invoice_tax_type") or "NONE",
        "notifyEmail": row.get("notify_email") or "",
        "autoSyncEnabled": bool(row.get("auto_sync_enabled")) if row.get("auto_sync_enabled") is not None else True,
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


def get_companies_house_settings() -> dict:
    return _serialise(_ensure_settings_row())


def get_submission_raw_response(submission_reference: str) -> dict:
    reference = (submission_reference or "").strip()
    if not reference:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Submission reference is required.",
        )
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, company_id, submission_reference, transaction_id, status,
                       rejection_reason, response_payload, created_at, updated_at, completed_at
                FROM ch_submissions
                WHERE submission_reference = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (reference,),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No submission found with reference {reference}.",
        )
    payload = row.get("response_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {"rawResponse": payload}
    raw_response = ""
    if isinstance(payload, dict):
        raw_response = _xml_text(payload.get("rawResponse"))
    return {
        "submissionId": str(row.get("id")),
        "submissionReference": _xml_text(row.get("submission_reference")),
        "transactionId": _xml_text(row.get("transaction_id")),
        "companyId": _xml_text(row.get("company_id")),
        "status": _xml_text(row.get("status")),
        "rejectionReason": _xml_text(row.get("rejection_reason")),
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        "completedAt": row.get("completed_at").isoformat() if row.get("completed_at") else None,
        "rawResponse": raw_response,
        "responsePayload": payload if isinstance(payload, dict) else {},
    }


def _connection_test_probe_company_number(overrides: dict) -> str:
    override_number = _xml_text(overrides.get("probeCompanyNumber"))
    if override_number:
        _, number_digits = _ch_split_company_number(override_number)
        return override_number if number_digits else ""
    # Keep default connection tests independent of per-company filing authority.
    # A random "latest" company can trigger intermittent authorisation failures
    # even when presenter credentials and gateway connectivity are valid.
    return ""


def test_companies_house_connection(payload: dict | None = None) -> dict:
    overrides = payload or {}
    settings_row = _ensure_settings_row()
    environment = str(overrides.get("environment") or settings_row.get("environment") or "sandbox").strip().lower()
    if environment not in VALID_ENVIRONMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Environment must be 'sandbox' or 'production'.",
        )

    api_key = _validated_companies_house_api_key(configured_api_key(settings_row))
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configure a Companies House API key before running a connection test.",
        )

    base_url = _companies_house_api_base(environment)
    endpoint = f"{base_url}/company/00000000"
    rest_started = utcnow()
    with _companies_house_http_client(api_key) as client:
        response = client.get(endpoint)
    rest_duration_ms = int((utcnow() - rest_started).total_seconds() * 1000)

    if response.status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN}:
        # If credentials fail in the selected environment, probe the opposite environment once.
        # This gives the user a concrete hint when a live key is tested in sandbox (or vice versa).
        alternate_environment = "production" if environment == "sandbox" else "sandbox"
        alternate_base_url = _companies_house_api_base(alternate_environment)
        alternate_endpoint = f"{alternate_base_url}/company/00000000"
        alternate_status_code: int | None = None
        with _companies_house_http_client(api_key) as alternate_client:
            try:
                alternate_response = alternate_client.get(alternate_endpoint)
                alternate_status_code = alternate_response.status_code
            except Exception:
                alternate_status_code = None
        if alternate_status_code is not None and alternate_status_code not in {
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        }:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Companies House credentials were rejected in {environment}, "
                    f"but accepted in {alternate_environment}. Switch environment to "
                    f"'{alternate_environment}' and retry."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Companies House rejected the API credentials. Use the REST API key only "
                "(not Streaming key, Client ID, or Client Secret), then confirm the selected "
                "environment matches where that key was created."
            ),
        )
    if response.is_error and response.status_code not in {
        status.HTTP_400_BAD_REQUEST,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_429_TOO_MANY_REQUESTS,
    }:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Companies House REST connection test failed ({response.status_code}).",
        )

    sample_count = 0
    if not response.is_error and response.status_code != status.HTTP_404_NOT_FOUND:
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Companies House returned invalid JSON during the REST connection test.",
            ) from exc
        items = payload.get("items")
        sample_count = len(items) if isinstance(items, list) else 0

    presenter_id = configured_presenter_id(settings_row)
    presenter_auth = configured_presenter_auth(settings_row)
    gateway_attempted = bool(presenter_id and presenter_auth)
    gateway_connected = False
    gateway_errors: list[str] = []
    gateway_error_message = ""
    gateway_request_debug = ""
    gateway_request_xml = ""
    gateway_response_xml = ""
    gateway_response_bytes = 0
    gateway_duration_ms = 0
    if gateway_attempted:
        gateway_started = utcnow()
        try:
            probe_company_number = _connection_test_probe_company_number(overrides)
            if probe_company_number:
                gateway_request = _build_ch_status_xml(
                    presenter_id=presenter_id,
                    presenter_auth=presenter_auth,
                    environment=environment,
                    transaction_id=_ch_txn_id(),
                    company_number=probe_company_number,
                )
                gateway_request_debug = (
                    f"Sent presenterId={_xml_text(presenter_id)}, presenterAuth={_xml_text(presenter_auth)}, "
                    f"gatewayTest={_ch_gateway_test_flag(environment)}, class=GetSubmissionStatus, "
                    f"companyNumber={_xml_text(probe_company_number)}."
                )
            else:
                gateway_request = _build_ch_status_xml(
                    presenter_id=presenter_id,
                    presenter_auth=presenter_auth,
                    environment=environment,
                    transaction_id=_ch_txn_id(),
                    submission_number="ZZZZZZ",
                )
                gateway_request_debug = (
                    f"Sent presenterId={_xml_text(presenter_id)}, presenterAuth={_xml_text(presenter_auth)}, "
                    f"gatewayTest={_ch_gateway_test_flag(environment)}, class=GetSubmissionStatus, submissionNumber=ZZZZZZ."
                )
            gateway_request_xml = gateway_request.decode("utf-8", errors="replace")
            gateway_response_text, gateway_response_root = _post_ch_gateway(gateway_request)
            gateway_response_xml = gateway_response_text
            gateway_duration_ms = int((utcnow() - gateway_started).total_seconds() * 1000)
            gateway_response_bytes = len(gateway_response_text.encode("utf-8"))
            gateway_errors = _ch_gateway_errors(gateway_response_root)
            gateway_error_text = " | ".join(gateway_errors).lower()
            if any(token in gateway_error_text for token in ("authorisation", "authorization", "authentication", "senderid", "sender id")):
                gateway_error_message = (
                    "Companies House XML gateway rejected presenter credentials or filing authority. "
                    "Check Presenter ID/auth code and account permissions."
                )
                if gateway_errors:
                    gateway_error_message = f"{gateway_error_message} Gateway detail: {_xml_text(gateway_errors[0])[:220]}"
                if gateway_request_debug:
                    gateway_error_message = f"{gateway_error_message} {gateway_request_debug}"
            else:
                gateway_connected = True
        except HTTPException as exc:
            gateway_duration_ms = int((utcnow() - gateway_started).total_seconds() * 1000)
            gateway_error_message = str(exc.detail or "Companies House XML gateway connection test failed.")
            if gateway_request_debug:
                gateway_error_message = f"{gateway_error_message} {gateway_request_debug}"
    else:
        gateway_error_message = (
            "XML gateway test skipped because Presenter ID/auth code are not configured. "
            "REST sync features are available; filing features require presenter credentials."
        )

    duration_ms = rest_duration_ms + gateway_duration_ms
    connected = bool(gateway_connected and gateway_attempted) if gateway_attempted else True
    if connected:
        message = "Companies House REST and XML gateway connections are working."
    elif gateway_attempted:
        message = (
            f"REST connection is working, but XML gateway failed: {gateway_error_message or 'Unknown gateway error.'}"
        )
    else:
        message = "REST connection is working. XML gateway test was skipped because presenter credentials are missing."
    return {
        "connected": connected,
        "environment": environment,
        "apiBaseUrl": base_url,
        "endpoint": endpoint,
        "statusCode": response.status_code,
        "durationMs": duration_ms,
        "restDurationMs": rest_duration_ms,
        "gatewayDurationMs": gateway_duration_ms,
        "sampleResultCount": sample_count,
        "restConnected": True,
        "gatewayConnected": gateway_connected,
        "gatewayAttempted": gateway_attempted,
        "gatewayErrorCount": len(gateway_errors),
        "gatewayErrors": gateway_errors[:10],
        "gatewayResponseBytes": gateway_response_bytes,
        "gatewayRequestDebug": gateway_request_debug,
        "gatewayRequestXml": gateway_request_xml,
        "gatewayResponseXml": gateway_response_xml,
        "gatewayError": gateway_error_message,
        "message": message,
    }


def record_audit_event(
    entity_type: str,
    entity_id: str,
    event_type: str,
    user_id: str | None,
    payload: dict | None = None,
) -> None:
    import json

    payload_json = json.dumps(payload or {})
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (entity_type, entity_id, event_type, payload_json, user_id),
            )
        connection.commit()


def save_companies_house_settings(user: dict, payload: dict) -> dict:
    existing = _ensure_settings_row()
    environment = str(payload.get("environment") or existing.get("environment") or "sandbox").strip().lower()
    if environment not in VALID_ENVIRONMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Environment must be 'sandbox' or 'production'.",
        )

    existing_api_key = configured_api_key(existing)
    incoming_api_key = payload.get("apiKey", existing_api_key)
    api_key_value = _validated_companies_house_api_key(_compact_credential(incoming_api_key))
    api_key_encrypted = encrypt_secret(api_key_value, CH_API_KEY_LABEL)
    api_key_hint = _mask(api_key_value)

    existing_presenter_id = configured_presenter_id(existing)
    incoming_presenter_id = payload.get("presenterId", existing_presenter_id)
    presenter_id = _compact_credential(incoming_presenter_id)
    existing_presenter_auth = configured_presenter_auth(existing)
    incoming_presenter_auth = payload.get("presenterAuth", existing_presenter_auth)
    presenter_auth_value = _compact_credential(incoming_presenter_auth)
    presenter_auth_encrypted = encrypt_secret(presenter_auth_value, CH_PRESENTER_AUTH_LABEL)
    presenter_auth_hint = _mask(presenter_auth_value)
    credit_account_number = _compact_credential(payload.get("creditAccountNumber"))
    xero_invoice_account_code = str(payload.get("xeroInvoiceAccountCode") or "").strip()
    xero_invoice_item_code = str(payload.get("xeroInvoiceItemCode") or "").strip()
    xero_invoice_description = str(payload.get("xeroInvoiceDescription") or "Companies House confirmation statement filing").strip()
    xero_invoice_unit_amount = _coerce_decimal(payload.get("xeroInvoiceUnitAmount"), "xeroInvoiceUnitAmount")
    xero_invoice_tax_type = str(payload.get("xeroInvoiceTaxType") or "NONE").strip() or "NONE"
    notify_email = str(payload.get("notifyEmail") or "").strip()
    auto_sync_enabled = bool(payload.get("autoSyncEnabled", existing.get("auto_sync_enabled", True)))

    now = utcnow()
    user_id = user.get("id") if isinstance(user, dict) else None

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ch_settings
                SET environment = %s,
                    api_key_encrypted = %s,
                    api_key_hint = %s,
                    presenter_id = %s,
                    presenter_auth_encrypted = %s,
                    presenter_auth_hint = %s,
                    credit_account_number = %s,
                    xero_invoice_account_code = %s,
                    xero_invoice_item_code = %s,
                    xero_invoice_description = %s,
                    xero_invoice_unit_amount = %s,
                    xero_invoice_tax_type = %s,
                    notify_email = %s,
                    auto_sync_enabled = %s,
                    updated_by_user_id = %s,
                    updated_at = %s
                WHERE singleton_id = 1
                RETURNING *
                """,
                (
                    environment,
                    api_key_encrypted,
                    api_key_hint,
                    presenter_id,
                    presenter_auth_encrypted,
                    presenter_auth_hint,
                    credit_account_number,
                    xero_invoice_account_code,
                    xero_invoice_item_code,
                    xero_invoice_description,
                    xero_invoice_unit_amount,
                    xero_invoice_tax_type,
                    notify_email,
                    auto_sync_enabled,
                    user_id,
                    now,
                ),
            )
            updated = cursor.fetchone()
        connection.commit()

    record_audit_event(
        entity_type="ch_settings",
        entity_id="singleton",
        event_type="settings_updated",
        user_id=user_id,
        payload={
            "environment": environment,
            "apiKeyChanged": api_key_value != existing_api_key,
            "presenterIdChanged": presenter_id != existing_presenter_id,
            "presenterAuthChanged": presenter_auth_value != existing_presenter_auth,
        },
    )

    return _serialise(updated)


def decrypt_api_key() -> str:
    return configured_api_key(_ensure_settings_row())


def _validated_companies_house_api_key(value: str) -> str:
    api_key = str(value or "").strip()
    if not api_key:
        return ""
    lowered = api_key.lower()
    if lowered.startswith("basic ") or lowered.startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Companies House API key is invalid. Save only the raw API key (not a Basic/Bearer Authorization header).",
        )
    if any(char.isspace() for char in api_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Companies House API key is invalid. Remove spaces and save only the raw key value.",
        )
    return api_key


def decrypt_presenter_auth() -> str:
    return configured_presenter_auth(_ensure_settings_row())


def _companies_house_api_base(environment: str) -> str:
    settings = get_settings()
    return (
        settings.companies_house_production_api_base
        if environment == "production"
        else settings.companies_house_sandbox_api_base
    ).rstrip("/")


def _companies_house_http_client(api_key: str) -> httpx.Client:
    return httpx.Client(
        timeout=25.0,
        auth=(api_key, ""),
        headers={
            "Accept": "application/json",
            "User-Agent": "CreditControlConsole/companies-house",
        },
    )


def _companies_house_get_json(client: httpx.Client, url: str) -> dict:
    response = client.get(url)
    if response.status_code == status.HTTP_404_NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found in Companies House.")
    if response.is_error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Companies House request failed ({response.status_code}) for {url}.",
        )
    try:
        return response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Companies House returned invalid JSON for {url}.",
        ) from exc


def _format_registered_office(address: dict | None) -> str:
    if not isinstance(address, dict):
        return ""
    parts = [
        address.get("care_of"),
        address.get("premises"),
        address.get("address_line_1"),
        address.get("address_line_2"),
        address.get("locality"),
        address.get("region"),
        address.get("postal_code"),
        address.get("country"),
    ]
    text_parts = [str(part).strip() for part in parts if str(part or "").strip()]
    return ", ".join(text_parts)


def _normalise_ch_officers(payload: dict | None) -> list[dict]:
    items = (payload or {}).get("items") or []
    output: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "name": str(item.get("name") or "").strip(),
                "role": str(item.get("officer_role") or "").strip(),
                "appointedOn": str(item.get("appointed_on") or "").strip(),
                "resignedOn": str(item.get("resigned_on") or "").strip(),
            }
        )
    return output[:50]


def _normalise_ch_pscs(payload: dict | None) -> list[dict]:
    items = (payload or {}).get("items") or []
    output: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "name": str(item.get("name") or "").strip(),
                "kind": str(item.get("kind") or "").strip(),
                "notifiedOn": str(item.get("notified_on") or "").strip(),
                "ceasedOn": str(item.get("ceased_on") or "").strip(),
            }
        )
    return output[:50]


def _normalise_ch_filing_history(payload: dict | None) -> list[dict]:
    items = (payload or {}).get("items") or []
    output: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        description_values = item.get("description_values") if isinstance(item.get("description_values"), dict) else {}
        output.append(
            {
                "date": str(item.get("date") or "").strip(),
                "type": str(item.get("type") or "").strip(),
                "description": str(item.get("description") or "").strip(),
                "category": str(item.get("category") or "").strip(),
                "made_up_to": str(
                    item.get("made_up_to")
                    or item.get("made_up_to_date")
                    or description_values.get("made_up_date")
                    or description_values.get("made_up_to")
                    or ""
                ).strip(),
                "period_end": str(
                    item.get("period_end")
                    or item.get("period_end_on")
                    or item.get("period_end_date")
                    or description_values.get("period_end_on")
                    or description_values.get("period_end_date")
                    or ""
                ).strip(),
            }
        )
    return output[:50]


def _is_confirmation_statement_filing(item: dict | None) -> bool:
    if not isinstance(item, dict):
        return False
    filing_type = str(item.get("type") or "").strip().upper()
    category = str(item.get("category") or "").strip().lower()
    description = str(item.get("description") or "").strip().lower()
    if filing_type in {"CS01", "AR01", "AR02", "AR"}:
        return True
    if "confirmation" in category:
        return True
    if "confirmation statement" in description or "confirmation-statement" in description:
        return True
    if "annual return" in description:
        return True
    return False


def _latest_confirmation_statement_filed_date(filing_history: list[dict]) -> date | None:
    filed_dates: list[date] = []
    for item in filing_history:
        if not _is_confirmation_statement_filing(item):
            continue
        filed_on = _parse_date_from_text(item.get("date"))
        if filed_on:
            filed_dates.append(filed_on)
    return max(filed_dates) if filed_dates else None


def _cached_ch_company_snapshot(company_number: str, max_age: timedelta = CH_COMPANY_SNAPSHOT_CACHE_TTL) -> dict | None:
    cutoff = utcnow() - max_age
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT company_number, company_name, company_status, incorporation_date, registered_office,
                       sic_codes, officers, pscs, next_made_up_to_date, next_due_date, last_filed_date, filing_history
                FROM ch_companies
                WHERE company_number = %s
                  AND last_synced_at IS NOT NULL
                  AND last_synced_at >= %s
                LIMIT 1
                """,
                (company_number, cutoff),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        return None
    return {
        "companyNumber": company_number,
        "companyName": str(row.get("company_name") or "").strip(),
        "companyStatus": str(row.get("company_status") or "").strip(),
        "incorporationDate": row.get("incorporation_date"),
        "registeredOffice": str(row.get("registered_office") or "").strip(),
        "sicCodes": row.get("sic_codes") if isinstance(row.get("sic_codes"), list) else [],
        "officers": row.get("officers") if isinstance(row.get("officers"), list) else [],
        "pscs": row.get("pscs") if isinstance(row.get("pscs"), list) else [],
        "nextMadeUpToDate": row.get("next_made_up_to_date"),
        "nextDueDate": row.get("next_due_date"),
        "lastFiledDate": row.get("last_filed_date"),
        "filingHistory": row.get("filing_history") if isinstance(row.get("filing_history"), list) else [],
    }


def _fetch_ch_company_snapshot(
    company_number: str,
    *,
    prefer_cache: bool = True,
    max_cache_age: timedelta = CH_COMPANY_SNAPSHOT_CACHE_TTL,
) -> dict:
    company_number = normalise_company_number(company_number)
    if not _is_valid_company_number(company_number):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid company number.")
    if prefer_cache:
        cached_snapshot = _cached_ch_company_snapshot(company_number, max_cache_age)
        if cached_snapshot:
            return cached_snapshot
    settings_row = _ensure_settings_row()
    environment = str(settings_row.get("environment") or "sandbox").strip().lower()
    api_key = _validated_companies_house_api_key(decrypt_api_key())
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configure a Companies House API key before syncing.",
        )
    base_url = _companies_house_api_base(environment)
    with _companies_house_http_client(api_key) as client:
        company_payload = _companies_house_get_json(client, f"{base_url}/company/{company_number}")
        try:
            officers_payload = _companies_house_get_json(client, f"{base_url}/company/{company_number}/officers")
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                officers_payload = {}
            else:
                raise
        try:
            psc_payload = _companies_house_get_json(
                client, f"{base_url}/company/{company_number}/persons-with-significant-control"
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                psc_payload = {}
            else:
                raise
        try:
            filing_payload = _companies_house_get_json(
                client, f"{base_url}/company/{company_number}/filing-history?items_per_page=25"
            )
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                filing_payload = {}
            else:
                raise
    confirmation = company_payload.get("confirmation_statement") or {}
    filing_history = _normalise_ch_filing_history(filing_payload)
    return {
        "companyNumber": company_number,
        "companyName": str(company_payload.get("company_name") or "").strip(),
        "companyStatus": str(company_payload.get("company_status") or "").strip(),
        "incorporationDate": _parse_date_from_text(company_payload.get("date_of_creation")),
        "registeredOffice": _format_registered_office(company_payload.get("registered_office_address")),
        "sicCodes": company_payload.get("sic_codes") or [],
        "officers": _normalise_ch_officers(officers_payload),
        "pscs": _normalise_ch_pscs(psc_payload),
        "nextMadeUpToDate": _parse_date_from_text(confirmation.get("next_made_up_to")),
        "nextDueDate": _parse_date_from_text(confirmation.get("next_due")),
        "lastFiledDate": _latest_confirmation_statement_filed_date(filing_history)
        or _parse_date_from_text(confirmation.get("last_made_up_to")),
        "filingHistory": filing_history,
    }


def _apply_company_snapshot(cursor, company_id: str, snapshot: dict) -> None:
    cursor.execute(
        """
        UPDATE ch_companies
        SET company_name = COALESCE(NULLIF(%s, ''), company_name),
            registered_office = %s,
            company_status = %s,
            incorporation_date = %s,
            sic_codes = %s::jsonb,
            officers = %s::jsonb,
            pscs = %s::jsonb,
            next_made_up_to_date = %s,
            next_due_date = %s,
            last_filed_date = %s,
            filing_history = %s::jsonb,
            last_synced_at = NOW(),
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            snapshot.get("companyName") or "",
            snapshot.get("registeredOffice") or "",
            snapshot.get("companyStatus") or "",
            snapshot.get("incorporationDate"),
            json.dumps(snapshot.get("sicCodes") or []),
            json.dumps(snapshot.get("officers") or []),
            json.dumps(snapshot.get("pscs") or []),
            snapshot.get("nextMadeUpToDate"),
            snapshot.get("nextDueDate"),
            snapshot.get("lastFiledDate"),
            json.dumps(snapshot.get("filingHistory") or []),
            company_id,
        ),
    )


def sync_companies_house_companies(user: dict | None, payload: dict | None = None) -> dict:
    payload = payload or {}
    company_ids = _chunk_company_ids(payload.get("companyIds") or [])
    limit = max(1, min(int(payload.get("limit") or MAX_COMPANIES_HOUSE_SYNC_BATCH), MAX_COMPANIES_HOUSE_SYNC_BATCH))
    user_id = user.get("id") if isinstance(user, dict) else None
    mode = str(payload.get("mode") or "manual").strip().lower()

    with _CH_SYNC_LOCK:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                if company_ids:
                    cursor.execute(
                        """
                        SELECT id, company_number, company_name
                        FROM ch_companies
                        WHERE id = ANY(%s)
                        ORDER BY company_name ASC
                        LIMIT %s
                        """,
                        (company_ids, limit),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT id, company_number, company_name
                        FROM ch_companies
                        ORDER BY
                            CASE WHEN next_due_date IS NULL THEN 1 ELSE 0 END,
                            next_due_date ASC,
                            company_name ASC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                companies = cursor.fetchall() or []
            connection.commit()

        if not companies:
            return {
                "mode": mode,
                "targetCount": 0,
                "syncedCount": 0,
                "failedCount": 0,
                "skippedCount": 0,
                "synced": [],
                "failed": [],
            }

        synced: list[dict] = []
        failed: list[dict] = []
        for row in companies:
            company_id = str(row.get("id") or "")
            company_number = normalise_company_number(row.get("company_number"))
            if not company_id or not company_number:
                failed.append(
                    {
                        "companyId": company_id,
                        "companyNumber": company_number,
                        "companyName": row.get("company_name") or "",
                        "reason": "Missing company id or company number.",
                    }
                )
                continue
            try:
                snapshot = _fetch_ch_company_snapshot(company_number)
                with get_connection() as connection:
                    with connection.cursor() as cursor:
                        _apply_company_snapshot(cursor, company_id, snapshot)
                        cursor.execute(
                            """
                            INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                            VALUES ('ch_company', %s, 'company_synced_from_companies_house', %s::jsonb, %s)
                            """,
                            (
                                company_id,
                                json.dumps(
                                    {
                                        "companyNumber": company_number,
                                        "mode": mode,
                                        "nextDueDate": snapshot.get("nextDueDate").isoformat()
                                        if isinstance(snapshot.get("nextDueDate"), date)
                                        else None,
                                    }
                                ),
                                user_id,
                            ),
                        )
                    connection.commit()
                synced.append(
                    {
                        "companyId": company_id,
                        "companyNumber": company_number,
                        "companyName": snapshot.get("companyName") or row.get("company_name") or "",
                    }
                )
            except HTTPException as exc:
                failed.append(
                    {
                        "companyId": company_id,
                        "companyNumber": company_number,
                        "companyName": row.get("company_name") or "",
                        "reason": str(exc.detail),
                    }
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Unexpected Companies House sync failure for %s", company_number)
                failed.append(
                    {
                        "companyId": company_id,
                        "companyNumber": company_number,
                        "companyName": row.get("company_name") or "",
                        "reason": str(exc) or exc.__class__.__name__,
                    }
                )

        if mode == "auto":
            record_audit_event(
                entity_type="ch_sync",
                entity_id="auto",
                event_type="auto_sync_completed",
                user_id=user_id,
                payload={
                    "targetCount": len(companies),
                    "syncedCount": len(synced),
                    "failedCount": len(failed),
                },
            )

        return {
            "mode": mode,
            "targetCount": len(companies),
            "syncedCount": len(synced),
            "failedCount": len(failed),
            "skippedCount": 0,
            "synced": synced,
            "failed": failed,
        }


def _latest_auto_sync_completed_at() -> datetime | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT created_at
                FROM audit_events
                WHERE entity_type = 'ch_sync'
                  AND event_type = 'auto_sync_completed'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone() or {}
        connection.commit()
    return row.get("created_at")


def _auto_sync_due() -> bool:
    settings_row = _ensure_settings_row()
    if not bool(settings_row.get("auto_sync_enabled")):
        return False
    if not decrypt_api_key():
        return False
    last_completed = _latest_auto_sync_completed_at()
    if not isinstance(last_completed, datetime):
        return True
    now = utcnow()
    return now - last_completed >= AUTO_SYNC_MIN_GAP


def run_companies_house_auto_sync_if_due() -> dict | None:
    if not _auto_sync_due():
        return None
    return sync_companies_house_companies(
        user=None,
        payload={"mode": "auto", "limit": MAX_COMPANIES_HOUSE_SYNC_BATCH},
    )


def _companies_house_auto_sync_worker() -> None:
    while not _CH_AUTO_SYNC_STOP.is_set():
        try:
            run_companies_house_auto_sync_if_due()
            run_companies_house_submission_reconciliation({"limit": 200})
        except Exception:
            logger.exception("Companies House auto-sync worker failed")
        _CH_AUTO_SYNC_STOP.wait(AUTO_SYNC_INTERVAL_SECONDS)


def start_companies_house_auto_sync_worker() -> None:
    global _CH_AUTO_SYNC_THREAD
    if _CH_AUTO_SYNC_THREAD and _CH_AUTO_SYNC_THREAD.is_alive():
        return
    _CH_AUTO_SYNC_STOP.clear()
    _CH_AUTO_SYNC_THREAD = threading.Thread(
        target=_companies_house_auto_sync_worker,
        name="companies-house-auto-sync",
        daemon=True,
    )
    _CH_AUTO_SYNC_THREAD.start()


def _xml_text(value: object, fallback: str = "") -> str:
    text = str(value if value is not None else fallback).strip()
    return text or fallback


def _ch_is_sandbox(environment: str) -> bool:
    return str(environment or "").strip().lower() != "production"


def _ch_gateway_test_flag(environment: str) -> str:
    return "1" if _ch_is_sandbox(environment) else "0"


def _ch_txn_id() -> str:
    return secrets.token_hex(16).upper()


def _ch_submission_number() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _next_unique_submission_number(max_attempts: int = 30) -> str:
    for _ in range(max_attempts):
        candidate = _ch_submission_number()
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 1
                    FROM ch_submissions
                    WHERE submission_reference = %s
                    LIMIT 1
                    """,
                    (candidate,),
                )
                existing_submission = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT 1
                    FROM ch_secretarial_filings
                    WHERE companies_house_ref = %s
                    LIMIT 1
                    """,
                    (candidate,),
                )
                existing_secretarial = cursor.fetchone()
            connection.commit()
        if not existing_submission and not existing_secretarial:
            return candidate
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unable to generate a unique submission number. Retry in a moment.",
    )


def _ch_split_company_number(company_number: str) -> tuple[str, str]:
    value = normalise_company_number(company_number)
    if not value:
        return "", ""
    if value.isdigit():
        return "EW", value
    if len(value) != 8:
        return "", ""
    prefix = value[:2]
    if prefix in {"SC", "NI", "OC", "SO", "NC"} and value[2:].isdigit():
        return prefix, value[2:]
    if value.startswith("R") and value[1:].isdigit():
        return "R", value[1:]
    return "", ""


def _ch_find_first(node: ET.Element, *local_names: str) -> ET.Element | None:
    wanted = set(local_names)
    for child in node.iter():
        tag = child.tag
        local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if local in wanted:
            return child
    return None


def _ch_find_all(node: ET.Element, local_name: str) -> list[ET.Element]:
    output: list[ET.Element] = []
    for child in node.iter():
        tag = child.tag
        local = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if local == local_name:
            output.append(child)
    return output


def _ch_gateway_errors(root: ET.Element) -> list[str]:
    errors: list[str] = []
    for node in _ch_find_all(root, "Error"):
        text = _xml_text(_ch_find_first(node, "Text").text if _ch_find_first(node, "Text") is not None else "")
        number = _xml_text(_ch_find_first(node, "Number").text if _ch_find_first(node, "Number") is not None else "")
        raised_by = _xml_text(_ch_find_first(node, "RaisedBy").text if _ch_find_first(node, "RaisedBy") is not None else "")
        parts = [part for part in [raised_by, number, text] if part]
        if parts:
            errors.append(" - ".join(parts))
    return errors


def _ch_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return default


def _ch_decimal_text(value: object) -> str:
    try:
        return str(Decimal(str(value)).normalize())
    except Exception:
        return "0"


def _normalise_shareholdings(share_capital: dict | None) -> list[dict]:
    if not isinstance(share_capital, dict):
        return []
    rows = share_capital.get("shareholdings")
    if not isinstance(rows, list):
        return []
    output: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        share_class = str(row.get("shareClass") or row.get("share_class") or "").strip()
        if not share_class:
            continue
        output.append(
            {
                "shareClass": share_class,
                "numberHeld": row.get("numberHeld") if row.get("numberHeld") is not None else row.get("number_held"),
                "shareholders": row.get("shareholders") if isinstance(row.get("shareholders"), list) else [],
                "transfers": row.get("transfers") if isinstance(row.get("transfers"), list) else [],
            }
        )
    return output[:5000]


def _first_bool_from_sources(*sources: object) -> bool | None:
    for source in sources:
        if source is None:
            continue
        if isinstance(source, bool):
            return source
        if isinstance(source, (int, float)) and source in {0, 1}:
            return bool(source)
        text = str(source).strip().lower()
        if text in {"true", "1", "yes", "y"}:
            return True
        if text in {"false", "0", "no", "n"}:
            return False
    return None


def _build_cs01_payload(company_row: dict, *, include_change_sections: bool = True) -> dict:
    share_capital = company_row.get("share_capital") if isinstance(company_row.get("share_capital"), dict) else {}
    statement_of_capital = share_capital.get("statementOfCapital") if isinstance(share_capital.get("statementOfCapital"), dict) else {}
    confirmation_statement = share_capital.get("confirmationStatement") if isinstance(share_capital.get("confirmationStatement"), dict) else {}
    cs_flags = share_capital.get("cs01Flags") if isinstance(share_capital.get("cs01Flags"), dict) else {}
    payload: dict = {}

    if include_change_sections:
        payload.update(
            {
                "sicCodes": company_row.get("sic_codes") if isinstance(company_row.get("sic_codes"), list) else [],
                "statementOfCapital": statement_of_capital,
                "shareholdings": _normalise_shareholdings(share_capital),
            }
        )

    for key in (
        "tradingOnMarket",
        "dtr5Applies",
        "pscExemptAsTradingOnRegulatedMarket",
        "pscExemptAsSharesAdmittedOnMarket",
        "pscExemptAsTradingOnUKRegulatedMarket",
    ):
        value = _first_bool_from_sources(
            company_row.get(key),
            confirmation_statement.get(key),
            cs_flags.get(key),
        )
        if value is not None:
            payload[key] = value

    pscs = company_row.get("pscs") if isinstance(company_row.get("pscs"), list) else []
    has_pscs = any(isinstance(item, dict) and not str(item.get("ceasedOn") or "").strip() for item in pscs)
    exemption_keys = (
        "pscExemptAsTradingOnRegulatedMarket",
        "pscExemptAsSharesAdmittedOnMarket",
        "pscExemptAsTradingOnUKRegulatedMarket",
    )
    exemptions_present = any(payload.get(key) is True for key in exemption_keys)

    if payload.get("dtr5Applies") is True and payload.get("tradingOnMarket") is None:
        payload["tradingOnMarket"] = True

    if not has_pscs and not exemptions_present:
        if payload.get("dtr5Applies") is True:
            payload["pscExemptAsSharesAdmittedOnMarket"] = True
        elif payload.get("tradingOnMarket") is True:
            payload["pscExemptAsTradingOnRegulatedMarket"] = True

    if payload.get("pscExemptAsTradingOnUKRegulatedMarket") is True and payload.get("pscExemptAsTradingOnRegulatedMarket") is None:
        payload["pscExemptAsTradingOnRegulatedMarket"] = True

    registered_email = _xml_text(
        confirmation_statement.get("registeredEmailAddress")
        or confirmation_statement.get("registered_email_address")
        or company_row.get("contact_email")
    )
    if registered_email:
        payload["registeredEmailAddress"] = registered_email
    lawful_purpose = _first_bool_from_sources(
        confirmation_statement.get("acceptLawfulPurposeStatement"),
        confirmation_statement.get("lawfulPurposeStatement"),
        confirmation_statement.get("lawfulPurposeAccepted"),
    )
    payload["acceptLawfulPurposeStatement"] = True if lawful_purpose is None else bool(lawful_purpose)
    state_confirmation = _first_bool_from_sources(
        confirmation_statement.get("stateConfirmation"),
        confirmation_statement.get("state_confirmation"),
    )
    payload["stateConfirmation"] = True if state_confirmation is None else bool(state_confirmation)
    review_period_start = _parse_date_from_text(
        confirmation_statement.get("reviewPeriodStart")
        or confirmation_statement.get("review_period_start")
    )
    review_period_end = _parse_date_from_text(
        confirmation_statement.get("reviewPeriodEnd")
        or confirmation_statement.get("review_period_end")
    )
    if review_period_start:
        payload["reviewPeriodStart"] = review_period_start.isoformat()
    if review_period_end:
        payload["reviewPeriodEnd"] = review_period_end.isoformat()
    identity_verification = (
        confirmation_statement.get("identityVerification")
        if isinstance(confirmation_statement.get("identityVerification"), dict)
        else {}
    )
    if identity_verification:
        payload["identityVerification"] = identity_verification
    return payload


def _load_latest_submission_cs01_payload(company_id: str) -> dict:
    safe_company_id = str(company_id or "").strip()
    if not safe_company_id:
        return {}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT response_payload
                FROM ch_submissions
                WHERE company_id = %s
                ORDER BY submitted_at DESC NULLS LAST, created_at DESC
                LIMIT 30
                """,
                (safe_company_id,),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    for row in rows:
        response_payload = row.get("response_payload") if isinstance(row.get("response_payload"), dict) else {}
        candidate = response_payload.get("csPayload")
        if isinstance(candidate, dict):
            return candidate
    return {}


def _prefill_no_changes_cs01_payload(
    company_row: dict,
    current_payload: dict | None,
    review_date: date,
) -> dict:
    payload = dict(current_payload) if isinstance(current_payload, dict) else {}
    previous_payload = _load_latest_submission_cs01_payload(str(company_row.get("id") or ""))

    def _is_empty(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, dict)):
            return len(value) == 0
        return False

    for key in (
        "tradingOnMarket",
        "dtr5Applies",
        "pscExemptAsTradingOnRegulatedMarket",
        "pscExemptAsSharesAdmittedOnMarket",
        "pscExemptAsTradingOnUKRegulatedMarket",
        "registeredEmailAddress",
        "acceptLawfulPurposeStatement",
        "stateConfirmation",
        "identityVerification",
        "sicCodes",
        "statementOfCapital",
        "shareholdings",
    ):
        if _is_empty(payload.get(key)) and not _is_empty(previous_payload.get(key)):
            payload[key] = previous_payload.get(key)

    if _is_empty(payload.get("sicCodes")) and isinstance(company_row.get("sic_codes"), list):
        payload["sicCodes"] = company_row.get("sic_codes") or []
    share_capital = company_row.get("share_capital") if isinstance(company_row.get("share_capital"), dict) else {}
    statement_of_capital = share_capital.get("statementOfCapital") if isinstance(share_capital.get("statementOfCapital"), dict) else {}
    if _is_empty(payload.get("statementOfCapital")) and statement_of_capital:
        payload["statementOfCapital"] = statement_of_capital
    shareholdings = _normalise_shareholdings(share_capital)
    if _is_empty(payload.get("shareholdings")) and shareholdings:
        payload["shareholdings"] = shareholdings

    # Keep submission period aligned to this year's review date.
    previous_review_end = _parse_date_from_text(previous_payload.get("reviewPeriodEnd"))
    last_filed_date = company_row.get("last_filed_date")
    derived_start = (
        (previous_review_end + timedelta(days=1)) if isinstance(previous_review_end, date)
        else ((last_filed_date + timedelta(days=1)) if isinstance(last_filed_date, date) else None)
    )
    current_review_start = _parse_date_from_text(payload.get("reviewPeriodStart"))
    if isinstance(derived_start, date):
        payload["reviewPeriodStart"] = derived_start.isoformat()
    elif isinstance(current_review_start, date) and current_review_start <= review_date:
        payload["reviewPeriodStart"] = current_review_start.isoformat()
    else:
        payload.pop("reviewPeriodStart", None)

    payload["reviewPeriodEnd"] = review_date.isoformat()
    if _is_empty(payload.get("acceptLawfulPurposeStatement")):
        payload["acceptLawfulPurposeStatement"] = True
    if _is_empty(payload.get("stateConfirmation")):
        payload["stateConfirmation"] = True
    return payload


def _cs01_psc_market_errors(pscs: list[dict], cs_payload: dict) -> list[str]:
    errors: list[str] = []
    has_pscs = any(isinstance(item, dict) and not str(item.get("ceasedOn") or "").strip() for item in pscs)
    trading_on_market = cs_payload.get("tradingOnMarket")
    dtr5_applies = cs_payload.get("dtr5Applies")
    exempt_regulated = cs_payload.get("pscExemptAsTradingOnRegulatedMarket")
    exempt_shares = cs_payload.get("pscExemptAsSharesAdmittedOnMarket")
    exempt_uk_regulated = cs_payload.get("pscExemptAsTradingOnUKRegulatedMarket")
    exemption_truths = {
        "PSCExemptAsTradingOnRegulatedMarket": exempt_regulated is True,
        "PSCExemptAsSharesAdmittedOnMarket": exempt_shares is True,
        "PSCExemptAsTradingOnUKRegulatedMarket": exempt_uk_regulated is True,
    }
    enabled_exemptions = [label for label, enabled in exemption_truths.items() if enabled]

    if dtr5_applies is True and trading_on_market is False:
        errors.append("DTR5Applies cannot be true when TradingOnMarket is false.")
    if exempt_regulated is True and trading_on_market is False:
        errors.append("PSCExemptAsTradingOnRegulatedMarket requires TradingOnMarket to be true.")
    if exempt_shares is True and dtr5_applies is False:
        errors.append("PSCExemptAsSharesAdmittedOnMarket requires DTR5Applies to be true.")
    if exempt_uk_regulated is True and trading_on_market is False:
        errors.append("PSCExemptAsTradingOnUKRegulatedMarket requires TradingOnMarket to be true.")
    if len(enabled_exemptions) > 1 and not (
        set(enabled_exemptions)
        == {"PSCExemptAsTradingOnRegulatedMarket", "PSCExemptAsTradingOnUKRegulatedMarket"}
    ):
        errors.append("Only one PSC exemption route can be selected for CS01 (except UK regulated market + regulated market pairing).")
    if has_pscs and enabled_exemptions:
        errors.append("Active PSC records and PSC exemption flags cannot both be supplied.")
    if not has_pscs and not enabled_exemptions:
        errors.append("No active PSCs were found and no PSC exemption was selected for CS01.")
    return errors


@lru_cache(maxsize=2)
def _load_ch_xsd_schema(schema_url: str):
    if LET is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CS01 XML validation requires the 'lxml' package to be installed.",
        )
    try:
        schema_doc = LET.parse(schema_url)
        return LET.XMLSchema(schema_doc)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unable to load Companies House XSD schema for validation ({schema_url}).",
        ) from exc


def _validate_ch_submission_xml_against_xsd(xml_payload: bytes) -> list[str]:
    if not CH_XSD_VALIDATION_ENABLED:
        return []
    if LET is None:
        logger.warning("Skipping CH XSD validation because lxml is not installed.")
        return []
    try:
        schema = _load_ch_xsd_schema(CH_FORM_SUBMISSION_XSD_URL)
        xml_doc = LET.fromstring(xml_payload)
        form_submission_nodes = xml_doc.xpath("//*[local-name()='FormSubmission']")
        if not form_submission_nodes:
            return ["Generated XML is missing FormSubmission, so XSD validation could not run."]
        xml_doc = form_submission_nodes[0]
    except HTTPException as exc:
        logger.warning("Skipping CH XSD validation because schema could not be loaded: %s", exc.detail)
        return []
    except Exception as exc:
        return [f"Unable to parse generated Companies House XML for XSD validation: {str(exc) or exc.__class__.__name__}"]
    if schema.validate(xml_doc):
        return []
    return [str(error.message) for error in schema.error_log][:10]


def _validate_cs01_payload(
    company_row: dict,
    review_date: date,
    cs_payload: dict | None = None,
    *,
    include_change_sections: bool = True,
) -> list[str]:
    errors: list[str] = []
    company_number = normalise_company_number(company_row.get("company_number"))
    if not _is_valid_company_number(company_number):
        errors.append("Company number must be 8 alphanumeric characters.")
    made_up_to = company_row.get("next_made_up_to_date")
    if not isinstance(made_up_to, date):
        errors.append("Made up to date is required before submitting CS01.")
    elif review_date != made_up_to:
        errors.append("Review date must match the recorded made up to date.")
    next_due = company_row.get("next_due_date")
    if isinstance(next_due, date) and review_date > next_due:
        errors.append("Review date cannot be after the recorded due date.")
    share_capital = company_row.get("share_capital") or {}
    if include_change_sections:
        shareholdings = _normalise_shareholdings(share_capital if isinstance(share_capital, dict) else {})
        if shareholdings:
            for idx, item in enumerate(shareholdings, start=1):
                if item.get("numberHeld") in (None, ""):
                    errors.append(f"Shareholding row {idx} is missing NumberHeld.")
                if not item.get("shareholders"):
                    errors.append(f"Shareholding row {idx} must include at least one shareholder.")
    pscs = company_row.get("pscs") if isinstance(company_row.get("pscs"), list) else []
    payload = (
        cs_payload
        if isinstance(cs_payload, dict)
        else _build_cs01_payload(company_row, include_change_sections=include_change_sections)
    )
    review_period_start = _parse_date_from_text(payload.get("reviewPeriodStart"))
    review_period_end = _parse_date_from_text(payload.get("reviewPeriodEnd"))
    if review_period_start and review_period_end and review_period_start > review_period_end:
        errors.append("Review period start cannot be after review period end.")
    if review_period_end and review_period_end != review_date:
        errors.append("Review period end must match the submission review date.")
    if payload.get("acceptLawfulPurposeStatement") is not True:
        errors.append("Lawful purpose statement must be accepted for CS01.")
    if payload.get("stateConfirmation") is not True:
        errors.append("State confirmation must be set to true for CS01.")
    identity_verification = payload.get("identityVerification")
    if isinstance(identity_verification, dict) and identity_verification.get("verificationStatementGiven") is False:
        errors.append("Identity verification statement must be confirmed when identity verification data is supplied.")
    errors.extend(_cs01_psc_market_errors(pscs, payload))
    return errors


def _build_ch_submission_xml(
    *,
    presenter_id: str,
    presenter_auth: str,
    environment: str,
    company_number: str,
    company_name: str,
    company_auth_code: str,
    review_date: date,
    registered_email: str,
    package_reference: str,
    transaction_id: str,
    submission_number: str,
    cs_payload: dict | None = None,
) -> bytes:
    gov = ET.Element(
        "GovTalkMessage",
        {
            "xmlns": GOVTALK_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{GOVTALK_NS} http://xmlgw.companieshouse.gov.uk/v2-1/schema/Egov_ch-v2-0.xsd",
        },
    )
    ET.SubElement(gov, "EnvelopeVersion").text = "1.0"
    header = ET.SubElement(gov, "Header")
    message_details = ET.SubElement(header, "MessageDetails")
    ET.SubElement(message_details, "Class").text = "ConfirmationStatement"
    ET.SubElement(message_details, "Qualifier").text = "request"
    ET.SubElement(message_details, "TransactionID").text = transaction_id
    ET.SubElement(message_details, "GatewayTest").text = _ch_gateway_test_flag(environment)
    sender_details = ET.SubElement(header, "SenderDetails")
    id_auth = ET.SubElement(sender_details, "IDAuthentication")
    ET.SubElement(id_auth, "SenderID").text = presenter_id
    auth = ET.SubElement(id_auth, "Authentication")
    _ch_auth_method_value = _ch_auth_method()
    ET.SubElement(auth, "Method").text = _ch_auth_method_value
    ET.SubElement(auth, "Value").text = _ch_auth_value(_ch_auth_method_value, presenter_auth)
    govtalk_details = ET.SubElement(gov, "GovTalkDetails")
    ET.SubElement(govtalk_details, "Keys")

    body = ET.SubElement(gov, "Body")
    form_submission = ET.SubElement(
        body,
        f"{{{CH_HEADER_NS}}}FormSubmission",
        {
            "xmlns": CH_HEADER_NS,
            "xmlns:bs": CH_FORMS_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{CH_HEADER_NS} http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/FormSubmission-v2-11.xsd",
        },
    )
    form_header = ET.SubElement(form_submission, f"{{{CH_HEADER_NS}}}FormHeader")
    company_type, company_number_digits = _ch_split_company_number(company_number)
    if not company_number_digits:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported company number format for XML gateway: {company_number}.",
        )
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}CompanyNumber").text = company_number_digits
    if company_type != "EW":
        ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}CompanyType").text = company_type
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}CompanyName").text = _xml_text(company_name, "UNKNOWN COMPANY")
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}CompanyAuthenticationCode").text = company_auth_code
    package_reference_value = _xml_text(package_reference)
    if not package_reference_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Companies House PackageReference is not configured. "
                "Set COMPANIES_HOUSE_PACKAGE_REFERENCE to the package reference "
                "issued by Companies House for your software filing account before submitting."
            ),
        )
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}PackageReference").text = package_reference_value
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}Language").text = "EN"
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}FormIdentifier").text = "ConfirmationStatement"
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}SubmissionNumber").text = submission_number
    ET.SubElement(form_submission, f"{{{CH_HEADER_NS}}}DateSigned").text = review_date.isoformat()
    form = ET.SubElement(form_submission, f"{{{CH_HEADER_NS}}}Form")
    cs = ET.SubElement(
        form,
        f"{{{CH_FORMS_NS}}}ConfirmationStatement",
        {
            "xmlns": CH_FORMS_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{CH_FORMS_NS} http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/ConfirmationStatement-v1-3.xsd",
        },
    )
    ET.SubElement(cs, f"{{{CH_FORMS_NS}}}ReviewDate").text = review_date.isoformat()
    payload = cs_payload if isinstance(cs_payload, dict) else {}
    trading_on_market = payload.get("tradingOnMarket")
    if trading_on_market is not None:
        ET.SubElement(cs, f"{{{CH_FORMS_NS}}}TradingOnMarket").text = "true" if _ch_bool(trading_on_market) else "false"
    dtr5_applies = payload.get("dtr5Applies")
    if dtr5_applies is not None:
        ET.SubElement(cs, f"{{{CH_FORMS_NS}}}DTR5Applies").text = "true" if _ch_bool(dtr5_applies) else "false"
    for key, node_name in (
        ("pscExemptAsTradingOnRegulatedMarket", "PSCExemptAsTradingOnRegulatedMarket"),
        ("pscExemptAsSharesAdmittedOnMarket", "PSCExemptAsSharesAdmittedOnMarket"),
        ("pscExemptAsTradingOnUKRegulatedMarket", "PSCExemptAsTradingOnUKRegulatedMarket"),
    ):
        value = payload.get(key)
        if value is not None:
            ET.SubElement(cs, f"{{{CH_FORMS_NS}}}{node_name}").text = "true" if _ch_bool(value) else "false"
    sic_codes = payload.get("sicCodes")
    if isinstance(sic_codes, list) and sic_codes:
        sic_node = ET.SubElement(cs, f"{{{CH_FORMS_NS}}}SICCodes")
        for code in sic_codes[:4]:
            code_text = str(code or "").strip()
            if code_text:
                ET.SubElement(sic_node, f"{{{CH_FORMS_NS}}}SICCode").text = code_text
    statement_of_capital = payload.get("statementOfCapital")
    if isinstance(statement_of_capital, dict) and statement_of_capital:
        soc = ET.SubElement(cs, f"{{{CH_FORMS_NS}}}StatementOfCapital")
        total_shares = statement_of_capital.get("totalNumberOfSharesIssued")
        total_unpaid = statement_of_capital.get("totalAggregateNominalValue")
        if total_shares is not None:
            ET.SubElement(soc, f"{{{CH_FORMS_NS}}}TotalNumberOfSharesIssued").text = _ch_decimal_text(total_shares)
        if total_unpaid is not None:
            ET.SubElement(soc, f"{{{CH_FORMS_NS}}}TotalAggregateNominalValue").text = _ch_decimal_text(total_unpaid)
    shareholdings = payload.get("shareholdings")
    if isinstance(shareholdings, list):
        for row in shareholdings:
            if not isinstance(row, dict):
                continue
            share_class = str(row.get("shareClass") or "").strip()
            if not share_class:
                continue
            sh = ET.SubElement(cs, f"{{{CH_FORMS_NS}}}Shareholdings")
            ET.SubElement(sh, f"{{{CH_FORMS_NS}}}ShareClass").text = share_class
            ET.SubElement(sh, f"{{{CH_FORMS_NS}}}NumberHeld").text = _ch_decimal_text(row.get("numberHeld"))
            transfers = row.get("transfers") if isinstance(row.get("transfers"), list) else []
            for transfer in transfers[:200]:
                if not isinstance(transfer, dict):
                    continue
                transfer_date = _parse_date_from_text(transfer.get("dateOfTransfer") or transfer.get("date"))
                transfer_amount = transfer.get("numberSharesTransferred")
                if not transfer_date or transfer_amount in (None, ""):
                    continue
                transfer_node = ET.SubElement(sh, f"{{{CH_FORMS_NS}}}Transfers")
                ET.SubElement(transfer_node, f"{{{CH_FORMS_NS}}}DateOfTransfer").text = transfer_date.isoformat()
                ET.SubElement(transfer_node, f"{{{CH_FORMS_NS}}}NumberSharesTransferred").text = _ch_decimal_text(transfer_amount)
            shareholders = row.get("shareholders") if isinstance(row.get("shareholders"), list) else []
            for shareholder in shareholders[:10]:
                if not isinstance(shareholder, dict):
                    continue
                raw_name = shareholder.get("name") or shareholder.get("fullName") or ""
                name_text = str(raw_name).strip()
                if not name_text:
                    continue
                holder_node = ET.SubElement(sh, f"{{{CH_FORMS_NS}}}Shareholders")
                name_node = ET.SubElement(holder_node, f"{{{CH_FORMS_NS}}}Name")
                name_parts = [part for part in re.split(r"\s+", name_text) if part]
                if len(name_parts) >= 2:
                    ET.SubElement(name_node, f"{{{CH_FORMS_NS}}}Surname").text = name_parts[-1]
                    ET.SubElement(name_node, f"{{{CH_FORMS_NS}}}Forename").text = " ".join(name_parts[:-1])
                else:
                    ET.SubElement(name_node, f"{{{CH_FORMS_NS}}}AmalgamatedName").text = name_text
    registered_email_value = _xml_text(payload.get("registeredEmailAddress"), registered_email)
    if registered_email_value:
        ET.SubElement(cs, f"{{{CH_FORMS_NS}}}RegisteredEmailAddress").text = registered_email_value
    ET.SubElement(cs, f"{{{CH_FORMS_NS}}}AcceptLawfulPurposeStatement").text = "true" if _ch_bool(payload.get("acceptLawfulPurposeStatement"), True) else "false"
    ET.SubElement(cs, f"{{{CH_FORMS_NS}}}StateConfirmation").text = "true" if _ch_bool(payload.get("stateConfirmation"), True) else "false"
    return ET.tostring(gov, encoding="utf-8", xml_declaration=True)


def _build_ch_status_xml(
    *,
    presenter_id: str,
    presenter_auth: str,
    environment: str,
    transaction_id: str,
    submission_number: str | None = None,
    company_number: str | None = None,
) -> bytes:
    gov = ET.Element(
        "GovTalkMessage",
        {
            "xmlns": GOVTALK_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{GOVTALK_NS} http://xmlgw.companieshouse.gov.uk/v2-1/schema/Egov_ch-v2-0.xsd",
        },
    )
    ET.SubElement(gov, "EnvelopeVersion").text = "1.0"
    header = ET.SubElement(gov, "Header")
    message_details = ET.SubElement(header, "MessageDetails")
    ET.SubElement(message_details, "Class").text = "GetSubmissionStatus"
    ET.SubElement(message_details, "Qualifier").text = "request"
    ET.SubElement(message_details, "TransactionID").text = transaction_id
    ET.SubElement(message_details, "GatewayTest").text = _ch_gateway_test_flag(environment)
    sender_details = ET.SubElement(header, "SenderDetails")
    id_auth = ET.SubElement(sender_details, "IDAuthentication")
    ET.SubElement(id_auth, "SenderID").text = presenter_id
    auth = ET.SubElement(id_auth, "Authentication")
    _ch_auth_method_value = _ch_auth_method()
    ET.SubElement(auth, "Method").text = _ch_auth_method_value
    ET.SubElement(auth, "Value").text = _ch_auth_value(_ch_auth_method_value, presenter_auth)
    govtalk_details = ET.SubElement(gov, "GovTalkDetails")
    ET.SubElement(govtalk_details, "Keys")
    body = ET.SubElement(gov, "Body")
    request = ET.SubElement(
        body,
        f"{{{CH_FORMS_NS}}}GetSubmissionStatus",
        {
            "xmlns": CH_FORMS_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{CH_FORMS_NS} http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/GetSubmissionStatus-v2-9.xsd",
        },
    )
    if submission_number:
        ET.SubElement(request, f"{{{CH_FORMS_NS}}}SubmissionNumber").text = submission_number
    elif company_number:
        _, number_digits = _ch_split_company_number(company_number)
        if number_digits:
            ET.SubElement(request, f"{{{CH_FORMS_NS}}}CompanyNumber").text = number_digits
    ET.SubElement(request, f"{{{CH_FORMS_NS}}}PresenterID").text = presenter_id
    return ET.tostring(gov, encoding="utf-8", xml_declaration=True)


def _build_ch_status_ack_xml(
    *,
    presenter_id: str,
    presenter_auth: str,
    environment: str,
    transaction_id: str,
) -> bytes:
    gov = ET.Element(
        "GovTalkMessage",
        {
            "xmlns": GOVTALK_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{GOVTALK_NS} http://xmlgw.companieshouse.gov.uk/v2-1/schema/Egov_ch-v2-0.xsd",
        },
    )
    ET.SubElement(gov, "EnvelopeVersion").text = "1.0"
    header = ET.SubElement(gov, "Header")
    message_details = ET.SubElement(header, "MessageDetails")
    ET.SubElement(message_details, "Class").text = "StatusAck"
    ET.SubElement(message_details, "Qualifier").text = "request"
    ET.SubElement(message_details, "TransactionID").text = transaction_id
    ET.SubElement(message_details, "GatewayTest").text = _ch_gateway_test_flag(environment)
    sender_details = ET.SubElement(header, "SenderDetails")
    id_auth = ET.SubElement(sender_details, "IDAuthentication")
    ET.SubElement(id_auth, "SenderID").text = presenter_id
    auth = ET.SubElement(id_auth, "Authentication")
    _ch_auth_method_value = _ch_auth_method()
    ET.SubElement(auth, "Method").text = _ch_auth_method_value
    ET.SubElement(auth, "Value").text = _ch_auth_value(_ch_auth_method_value, presenter_auth)
    govtalk_details = ET.SubElement(gov, "GovTalkDetails")
    ET.SubElement(govtalk_details, "Keys")
    body = ET.SubElement(gov, "Body")
    request = ET.SubElement(
        body,
        f"{{{CH_FORMS_NS}}}StatusAck",
        {
            "xmlns": CH_FORMS_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{CH_FORMS_NS} http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/GetStatusAck-v1-1.xsd",
        },
    )
    return ET.tostring(gov, encoding="utf-8", xml_declaration=True)


def _build_ch_document_xml(
    *,
    presenter_id: str,
    presenter_auth: str,
    environment: str,
    transaction_id: str,
    doc_request_key: str,
) -> bytes:
    gov = ET.Element(
        "GovTalkMessage",
        {
            "xmlns": GOVTALK_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{GOVTALK_NS} http://xmlgw.companieshouse.gov.uk/v2-1/schema/Egov_ch-v2-0.xsd",
        },
    )
    ET.SubElement(gov, "EnvelopeVersion").text = "1.0"
    header = ET.SubElement(gov, "Header")
    message_details = ET.SubElement(header, "MessageDetails")
    ET.SubElement(message_details, "Class").text = "DocumentRequest"
    ET.SubElement(message_details, "Qualifier").text = "request"
    ET.SubElement(message_details, "TransactionID").text = transaction_id
    ET.SubElement(message_details, "GatewayTest").text = _ch_gateway_test_flag(environment)
    sender_details = ET.SubElement(header, "SenderDetails")
    id_auth = ET.SubElement(sender_details, "IDAuthentication")
    ET.SubElement(id_auth, "SenderID").text = presenter_id
    auth = ET.SubElement(id_auth, "Authentication")
    _ch_auth_method_value = _ch_auth_method()
    ET.SubElement(auth, "Method").text = _ch_auth_method_value
    ET.SubElement(auth, "Value").text = _ch_auth_value(_ch_auth_method_value, presenter_auth)
    govtalk_details = ET.SubElement(gov, "GovTalkDetails")
    ET.SubElement(govtalk_details, "Keys")
    body = ET.SubElement(gov, "Body")
    request = ET.SubElement(
        body,
        f"{{{CH_FORMS_NS}}}GetDocument",
        {
            "xmlns": CH_FORMS_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{CH_FORMS_NS} http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/GetDocument-v1-1.xsd",
        },
    )
    ET.SubElement(request, f"{{{CH_FORMS_NS}}}DocRequestKey").text = doc_request_key
    return ET.tostring(gov, encoding="utf-8", xml_declaration=True)


def _poll_ch_status_ack_and_document(
    *,
    presenter_id: str,
    presenter_auth: str,
    environment: str,
    doc_request_key: str | None,
) -> dict:
    output: dict[str, object] = {"statusAckRawResponse": "", "documentRawResponse": "", "errors": []}
    errors: list[str] = []
    try:
        ack_xml = _build_ch_status_ack_xml(
            presenter_id=presenter_id,
            presenter_auth=presenter_auth,
            environment=environment,
            transaction_id=_ch_txn_id(),
        )
        ack_response, _ = _post_ch_gateway(ack_xml)
        output["statusAckRawResponse"] = ack_response[:30000]
    except Exception as exc:
        errors.append(f"GetStatusAck failed: {str(exc) or exc.__class__.__name__}")
    try:
        if doc_request_key:
            document_xml = _build_ch_document_xml(
                presenter_id=presenter_id,
                presenter_auth=presenter_auth,
                environment=environment,
                transaction_id=_ch_txn_id(),
                doc_request_key=doc_request_key,
            )
            document_response, _ = _post_ch_gateway(document_xml)
            output["documentRawResponse"] = document_response[:30000]
        else:
            errors.append("GetDocument skipped: no DocRequestKey was returned by GetSubmissionStatus.")
    except Exception as exc:
        errors.append(f"GetDocument failed: {str(exc) or exc.__class__.__name__}")
    output["errors"] = errors
    return output


def _post_ch_gateway(xml_payload: bytes) -> tuple[str, ET.Element]:
    last_error = ""
    started_at = time.monotonic()
    for attempt in range(1, CH_GATEWAY_MAX_ATTEMPTS + 1):
        elapsed = time.monotonic() - started_at
        remaining = CH_GATEWAY_MAX_ELAPSED_SECONDS - elapsed
        if remaining <= 0:
            break
        request_timeout = max(1.0, min(CH_GATEWAY_REQUEST_TIMEOUT_SECONDS, remaining))
        try:
            response = httpx.post(
                COMPANIES_HOUSE_XML_GATEWAY_URL,
                content=xml_payload,
                headers={"Content-Type": "text/xml; charset=utf-8"},
                timeout=request_timeout,
            )
            if response.status_code >= 500 and attempt < CH_GATEWAY_MAX_ATTEMPTS:
                last_error = f"HTTP {response.status_code}"
                remaining_before_sleep = CH_GATEWAY_MAX_ELAPSED_SECONDS - (time.monotonic() - started_at)
                sleep_seconds = min(CH_GATEWAY_BACKOFF_SECONDS * attempt, max(0.0, remaining_before_sleep - 0.1))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                continue
            if response.is_error:
                detail = f"Companies House gateway returned HTTP {response.status_code}."
                response_excerpt = _xml_text(response.text)[:320]
                if response_excerpt:
                    detail = f"{detail} Response: {response_excerpt}"
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=detail,
                )
            response_text = response.text or ""
            try:
                root = ET.fromstring(response_text.encode("utf-8"))
            except ET.ParseError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Companies House gateway returned invalid XML.",
                ) from exc
            return response_text, root
        except httpx.TimeoutException as exc:
            last_error = f"Timed out after {int(round(request_timeout))}s"
            if attempt >= CH_GATEWAY_MAX_ATTEMPTS:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail=(
                        "Companies House gateway timed out while waiting for a submission status response. "
                        "No confirmation that the filing was sent was returned."
                    ),
                ) from exc
            remaining_before_sleep = CH_GATEWAY_MAX_ELAPSED_SECONDS - (time.monotonic() - started_at)
            sleep_seconds = min(CH_GATEWAY_BACKOFF_SECONDS * attempt, max(0.0, remaining_before_sleep - 0.1))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        except httpx.HTTPError as exc:
            last_error = str(exc) or exc.__class__.__name__
            if attempt >= CH_GATEWAY_MAX_ATTEMPTS:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Companies House gateway request failed after retries: {last_error}",
                ) from exc
            remaining_before_sleep = CH_GATEWAY_MAX_ELAPSED_SECONDS - (time.monotonic() - started_at)
            sleep_seconds = min(CH_GATEWAY_BACKOFF_SECONDS * attempt, max(0.0, remaining_before_sleep - 0.1))
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    if "timed out" in last_error.lower():
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=(
                "Companies House gateway timed out while waiting for a submission status response. "
                "No confirmation that the filing was sent was returned."
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Companies House gateway request failed after retries: {last_error or 'Unknown error'}",
    )


def _extract_payment_evidence(response_root: ET.Element) -> dict:
    evidence: dict[str, str] = {}
    for local_name, key in (
        ("PaymentReference", "paymentReference"),
        ("ChargeReference", "chargeReference"),
        ("PaymentStatus", "paymentStatus"),
        ("Paid", "paid"),
        ("Amount", "amount"),
    ):
        node = _ch_find_first(response_root, local_name)
        if node is not None and _xml_text(node.text):
            evidence[key] = _xml_text(node.text)
    return evidence


def _payment_evidence_complete(evidence: dict | None) -> bool:
    if not isinstance(evidence, dict):
        return False
    for key in ("paymentReference", "chargeReference", "paymentStatus", "paid", "amount"):
        if _xml_text(evidence.get(key)):
            return True
    return False


def _payment_confirmation_fallback_evidence(*, source: str, status_code: str, now: datetime) -> dict:
    return {
        "paymentConfirmationFallback": True,
        "paymentConfirmationSource": source,
        "statusCode": _xml_text(status_code, "UNKNOWN"),
        "confirmedAt": now.isoformat(),
    }


def _status_poll_payment_reconciliation(
    *,
    presenter_id: str,
    presenter_auth: str,
    environment: str,
    submission_number: str,
    now: datetime,
) -> dict:
    try:
        request_xml = _build_ch_status_xml(
            presenter_id=presenter_id,
            presenter_auth=presenter_auth,
            environment=environment,
            transaction_id=_ch_txn_id(),
            submission_number=submission_number,
        )
        response_text, response_root = _post_ch_gateway(request_xml)
        parsed = _parse_ch_status_response(response_text=response_text, response_root=response_root)
        status_row = next((item for item in parsed.get("statuses", []) if item.get("submissionNumber") == submission_number), None)
        status_code = _xml_text((status_row or {}).get("statusCode"), "UNKNOWN")
        doc_request_key = _xml_text((status_row or {}).get("docRequestKey"))
        payment_evidence = parsed.get("paymentEvidence") or {}
        ack_document_payload = _poll_ch_status_ack_and_document(
            presenter_id=presenter_id,
            presenter_auth=presenter_auth,
            environment=environment,
            doc_request_key=doc_request_key or None,
        )
        if not _payment_evidence_complete(payment_evidence):
            payment_evidence = {
                **payment_evidence,
                **_payment_confirmation_fallback_evidence(
                    source="status_poll_acceptance",
                    status_code=status_code,
                    now=now,
                ),
            }
        return {
            "ok": True,
            "statusCode": status_code,
            "docRequestKey": doc_request_key,
            "paymentEvidence": payment_evidence,
            "rawResponse": parsed.get("rawResponse") or response_text[:30000],
            "statusAckRawResponse": ack_document_payload.get("statusAckRawResponse", ""),
            "documentRawResponse": ack_document_payload.get("documentRawResponse", ""),
            "pollErrors": ack_document_payload.get("errors", []),
        }
    except Exception as exc:
        logger.exception("Unable to reconcile payment evidence via status poll for %s", submission_number)
        return {
            "ok": False,
            "error": str(exc) or exc.__class__.__name__,
            "paymentEvidence": _payment_confirmation_fallback_evidence(
                source="accepted_without_gateway_payment_fields",
                status_code="ACCEPT",
                now=now,
            ),
        }


def _record_dead_letter(
    *,
    company_id: str,
    submission_id: str | None,
    stage: str,
    reason: str,
    payload: dict | None = None,
) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ch_dead_letters (submission_id, company_id, workflow, stage, reason, payload)
                VALUES (%s, %s, 'confirmation_statement_bulk', %s, %s, %s::jsonb)
                """,
                (submission_id, company_id, stage, reason, json.dumps(payload or {})),
            )
        connection.commit()
    _send_dead_letter_alert(
        {
            "submissionId": submission_id or "",
            "companyId": company_id,
            "stage": stage,
            "reason": reason,
            "payload": payload or {},
            "createdAt": utcnow().isoformat(),
        }
    )


def _send_dead_letter_alert(event: dict) -> None:
    webhook_url = str(get_settings().ch_alert_webhook_url or "").strip()
    if not webhook_url:
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(webhook_url, json={"type": "companies_house_dead_letter", "event": event})
    except Exception:
        logger.exception("Failed to send Companies House dead-letter alert")


def _parse_ch_submission_response(*, response_text: str, response_root: ET.Element, requested_submission_number: str) -> dict:
    errors = _ch_gateway_errors(response_root)
    payment_evidence = _extract_payment_evidence(response_root)
    statuses: list[dict] = []
    for status_node in _ch_find_all(response_root, "Status"):
        sub_no_node = _ch_find_first(status_node, "SubmissionNumber")
        code_node = _ch_find_first(status_node, "StatusCode")
        company_number_node = _ch_find_first(status_node, "CompanyNumber")
        doc_request_key_node = _ch_find_first(status_node, "DocRequestKey")
        reason_parts: list[str] = []
        for reject in _ch_find_all(status_node, "Reject"):
            description = _ch_find_first(reject, "Description")
            code = _ch_find_first(reject, "RejectCode")
            part = " ".join(
                part for part in [f"[{_xml_text(code.text)}]" if code is not None and _xml_text(code.text) else "", _xml_text(description.text)] if part
            ).strip()
            if part:
                reason_parts.append(part)
        statuses.append(
            {
                "submissionNumber": _xml_text(sub_no_node.text) if sub_no_node is not None else "",
                "statusCode": _xml_text(code_node.text).upper() if code_node is not None else "",
                "companyNumber": _xml_text(company_number_node.text) if company_number_node is not None else "",
                "docRequestKey": _xml_text(doc_request_key_node.text) if doc_request_key_node is not None else "",
                "rejectionReason": " | ".join(reason_parts),
            }
        )
    status_row = next((item for item in statuses if item["submissionNumber"] == requested_submission_number), None)
    if status_row is None and statuses:
        status_row = statuses[0]
    status_code = _xml_text((status_row or {}).get("statusCode"), "PENDING").upper()
    rejection_reason = _xml_text((status_row or {}).get("rejectionReason"))
    if errors:
        status_code = "REJECT"
        rejection_reason = " | ".join(errors)
    internal_status = _reconcile_submission_status_code(status_code)
    return {
        "statusCode": status_code,
        "status": internal_status,
        "rejectionReason": rejection_reason,
        "errors": errors,
        "statuses": statuses,
        "paymentEvidence": payment_evidence,
        "rawResponse": response_text[:30000],
    }


def _enhance_authorisation_failure_reason(
    reason: str,
    *,
    environment: str,
    presenter_id: str,
    presenter_auth: str,
    company_auth_code: str,
    company_number: str,
) -> str:
    text = _xml_text(reason)
    lower = text.lower()
    if "authorisation failure" not in lower and "authorization failure" not in lower and "authentication" not in lower:
        return text
    environment_text = _xml_text(environment, "sandbox").lower()
    context = (
        f"Authorisation check: environment={environment_text}, "
        f"company={_xml_text(company_number)}, presenterId={_xml_text(presenter_id)}, "
        f"presenterAuth={_xml_text(presenter_auth)}, companyAuthCode={_xml_text(company_auth_code)}. "
        "CH rejected credentials for this filing path."
    )
    likely_causes = [
        (
            "Environment is sandbox (test mode). Live UK filings usually require production "
            "presenter credentials and production filing authority."
            if environment_text != "production"
            else "Presenter ID/auth may not match a live Companies House XML Gateway software-filing profile."
        ),
        (
            "Presenter authentication code may be wrong, rotated, or disabled on the Companies House "
            "software filing account."
        ),
        (
            "Company authentication code may be wrong for this company. Use the 6-character Companies House "
            "company auth code (not a GOV.UK One Login / personal code)."
        ),
        "Presenter account may not be authorised for this company or filing route in Companies House.",
    ]
    likely_causes_text = "Likely UK causes: " + " ".join(
        f"{index}. {message}" for index, message in enumerate(likely_causes, start=1)
    )
    if context.lower() in lower:
        return text
    if "likely uk causes:" in lower:
        return text
    if text:
        return f"{text} | {context} | {likely_causes_text}"
    return f"{context} | {likely_causes_text}"


def _parse_ch_status_response(*, response_text: str, response_root: ET.Element) -> dict:
    errors = _ch_gateway_errors(response_root)
    payment_evidence = _extract_payment_evidence(response_root)
    statuses: list[dict] = []
    for status_node in _ch_find_all(response_root, "Status"):
        submission_number = _xml_text(_ch_find_first(status_node, "SubmissionNumber").text if _ch_find_first(status_node, "SubmissionNumber") is not None else "")
        status_code = _xml_text(_ch_find_first(status_node, "StatusCode").text if _ch_find_first(status_node, "StatusCode") is not None else "").upper()
        company_number = _xml_text(_ch_find_first(status_node, "CompanyNumber").text if _ch_find_first(status_node, "CompanyNumber") is not None else "")
        doc_request_key = _xml_text(_ch_find_first(status_node, "DocRequestKey").text if _ch_find_first(status_node, "DocRequestKey") is not None else "")
        rejection_parts: list[str] = []
        for reject_node in _ch_find_all(status_node, "Reject"):
            code = _xml_text(_ch_find_first(reject_node, "RejectCode").text if _ch_find_first(reject_node, "RejectCode") is not None else "")
            desc = _xml_text(_ch_find_first(reject_node, "Description").text if _ch_find_first(reject_node, "Description") is not None else "")
            message = " ".join(part for part in [f"[{code}]" if code else "", desc] if part).strip()
            if message:
                rejection_parts.append(message)
        statuses.append(
            {
                "submissionNumber": submission_number,
                "statusCode": status_code,
                "companyNumber": company_number,
                "docRequestKey": doc_request_key,
                "rejectionReason": " | ".join(rejection_parts),
            }
        )
    return {"errors": errors, "statuses": statuses, "paymentEvidence": payment_evidence, "rawResponse": response_text[:30000]}


def _load_company_auth_code(company_id: str) -> str:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT code_encrypted FROM ch_auth_codes WHERE company_id = %s", (company_id,))
            row = cursor.fetchone() or {}
        connection.commit()
    encrypted = _xml_text(row.get("code_encrypted"))
    if not encrypted:
        return ""
    try:
        return decrypt_secret(encrypted, f"{CH_COMPANY_AUTH_LABEL}:{company_id}")
    except Exception:
        logger.exception("Failed to decrypt CH auth code for company %s", company_id)
        return ""


def _reconcile_submission_status_code(status_code: str) -> str:
    code = _xml_text(status_code).upper()
    if code in {"ACCEPT", "ACCEPTED"} or code.startswith("ACCEPT"):
        return "accepted"
    if code in {"REJECT", "REJECTED", "INTERNAL_FAILURE", "FAILED"} or code.startswith("REJECT") or "FAIL" in code:
        return "rejected"
    return "submitted"


def run_companies_house_submission_reconciliation(payload: dict | None = None) -> dict:
    payload = payload or {}
    requested_submission_numbers = [str(value or "").strip() for value in (payload.get("submissionNumbers") or []) if str(value or "").strip()]
    limit = max(1, min(int(payload.get("limit") or 200), 500))
    settings_row = _ensure_settings_row()
    presenter_id = configured_presenter_id(settings_row)
    presenter_auth = decrypt_presenter_auth()
    environment = _xml_text(settings_row.get("environment"), "sandbox")
    if not presenter_id or not presenter_auth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configure Presenter ID and Presenter authentication code before polling submission status.",
        )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            if requested_submission_numbers:
                cursor.execute(
                    """
                    SELECT id, company_id, submission_reference, status, response_payload, rejection_reason, payment_evidence
                    FROM ch_submissions
                    WHERE submission_reference = ANY(%s)
                    """,
                    (requested_submission_numbers,),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, company_id, submission_reference, status, response_payload, rejection_reason, payment_evidence
                    FROM ch_submissions
                    WHERE status IN ('submitted')
                    ORDER BY submitted_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cursor.fetchall() or []
        connection.commit()

    if not rows:
        return {"checkedCount": 0, "updatedCount": 0, "acceptedCount": 0, "rejectedCount": 0, "pendingCount": 0, "errors": []}

    updated = 0
    accepted = 0
    rejected = 0
    pending = 0
    errors: list[str] = []

    for row in rows:
        submission_reference = _xml_text(row.get("submission_reference"))
        if not submission_reference:
            continue
        try:
            request_xml = _build_ch_status_xml(
                presenter_id=presenter_id,
                presenter_auth=presenter_auth,
                environment=environment,
                transaction_id=_ch_txn_id(),
                submission_number=submission_reference,
            )
            response_text, root = _post_ch_gateway(request_xml)
            parsed = _parse_ch_status_response(response_text=response_text, response_root=root)
            status_row = next((item for item in parsed.get("statuses", []) if item.get("submissionNumber") == submission_reference), None)
            if status_row is None and parsed.get("statuses"):
                status_row = parsed["statuses"][0]
            if status_row is None:
                pending += 1
                continue
            status_code = _xml_text(status_row.get("statusCode"), "PENDING")
            internal_status = _reconcile_submission_status_code(status_code)
            rejection_reason = _xml_text(status_row.get("rejectionReason"))
            doc_request_key = _xml_text(status_row.get("docRequestKey"))
            payment_evidence = parsed.get("paymentEvidence") or {}
            ack_document_payload = _poll_ch_status_ack_and_document(
                presenter_id=presenter_id,
                presenter_auth=presenter_auth,
                environment=environment,
                doc_request_key=doc_request_key or None,
            )
            existing_payment_evidence = row.get("payment_evidence") if isinstance(row.get("payment_evidence"), dict) else {}
            merged_payment_evidence = {**existing_payment_evidence, **payment_evidence}
            now = utcnow()
            payment_confirmed = True if internal_status == "accepted" else None
            if internal_status == "accepted" and not _payment_evidence_complete(merged_payment_evidence):
                merged_payment_evidence = {
                    **merged_payment_evidence,
                    **_payment_confirmation_fallback_evidence(
                        source="status_reconciliation_acceptance",
                        status_code=status_code,
                        now=now,
                    ),
                }
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE ch_submissions
                        SET status = %s,
                            rejection_reason = %s,
                            payment_confirmed = CASE WHEN %s IS TRUE THEN TRUE ELSE payment_confirmed END,
                            payment_evidence = COALESCE(payment_evidence, '{}'::jsonb) || %s::jsonb,
                            response_payload = COALESCE(response_payload, '{}'::jsonb) || %s::jsonb,
                            completed_at = CASE WHEN %s IN ('accepted', 'rejected') THEN COALESCE(completed_at, %s) ELSE completed_at END,
                            updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            internal_status,
                            rejection_reason,
                            payment_confirmed,
                            json.dumps(merged_payment_evidence),
                            json.dumps(
                                {
                                    "statusPoll": {
                                        "statusCode": status_code,
                                        "docRequestKey": doc_request_key,
                                        "rawResponse": parsed.get("rawResponse", ""),
                                        "paymentEvidence": payment_evidence,
                                        "statusAckRawResponse": ack_document_payload.get("statusAckRawResponse", ""),
                                        "documentRawResponse": ack_document_payload.get("documentRawResponse", ""),
                                        "pollErrors": ack_document_payload.get("errors", []),
                                    }
                                }
                            ),
                            internal_status,
                            now,
                            now,
                            row["id"],
                        ),
                    )
                connection.commit()
            if internal_status == "rejected":
                _record_dead_letter(
                    company_id=str(row.get("company_id") or ""),
                    submission_id=str(row.get("id") or ""),
                    stage="status_reconcile",
                    reason=rejection_reason or status_code,
                    payload={"statusCode": status_code, "paymentEvidence": payment_evidence},
                )
            updated += 1
            if internal_status == "accepted":
                accepted += 1
            elif internal_status == "rejected":
                rejected += 1
            else:
                pending += 1
        except HTTPException as exc:
            errors.append(str(exc.detail))
            _record_dead_letter(
                company_id=str(row.get("company_id") or ""),
                submission_id=str(row.get("id") or ""),
                stage="status_reconcile",
                reason=str(exc.detail),
                payload={"submissionReference": submission_reference},
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Submission status reconcile failed for %s", submission_reference)
            errors.append(str(exc) or exc.__class__.__name__)
            _record_dead_letter(
                company_id=str(row.get("company_id") or ""),
                submission_id=str(row.get("id") or ""),
                stage="status_reconcile",
                reason=str(exc) or exc.__class__.__name__,
                payload={"submissionReference": submission_reference},
            )

    return {
        "checkedCount": len(rows),
        "updatedCount": updated,
        "acceptedCount": accepted,
        "rejectedCount": rejected,
        "pendingCount": pending,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Phase 2: company records, bulk client import, dashboard
# ---------------------------------------------------------------------------


def normalise_company_number(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    if text.isdigit() and len(text) < 8:
        text = text.zfill(8)
    return text


def _is_valid_company_number(value: str) -> bool:
    if not value:
        return False
    if len(value) != 8:
        return False
    return bool(re.fullmatch(r"[A-Z0-9]{8}", value))


def _normalise_company_name_for_match(value: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower())
    text = re.sub(r"\b(ltd|limited|plc|llp|the|uk)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _company_name_match_score(candidate_name: str, tenant_name: str) -> int:
    candidate = _normalise_company_name_for_match(candidate_name)
    tenant = _normalise_company_name_for_match(tenant_name)
    if not candidate or not tenant:
        return 0
    if candidate == tenant:
        return 100
    if candidate.startswith(tenant) or tenant.startswith(candidate):
        return 90
    candidate_tokens = set(candidate.split())
    tenant_tokens = set(tenant.split())
    if not candidate_tokens or not tenant_tokens:
        return 0
    overlap = len(candidate_tokens & tenant_tokens)
    ratio = overlap / max(len(candidate_tokens), len(tenant_tokens))
    return int(round(ratio * 80))


def _score_company_candidate(candidate: dict, tenant_name: str) -> int:
    company_name = str(candidate.get("companyName") or candidate.get("company_name") or "").strip()
    client_name = str(candidate.get("clientName") or candidate.get("client_name") or "").strip()
    company_status = str(candidate.get("companyStatus") or candidate.get("company_status") or "").strip().lower()
    base_score = max(
        _company_name_match_score(company_name, tenant_name),
        _company_name_match_score(client_name, tenant_name),
    )
    status_boost = 5 if company_status == "active" else 0
    return base_score + status_boost


def _best_company_match_candidate(candidates: list[dict], tenant_name: str) -> tuple[dict | None, int, int, bool]:
    scored: list[tuple[int, dict]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        candidate_number = normalise_company_number(
            candidate.get("companyNumber") or candidate.get("company_number")
        )
        if not _is_valid_company_number(candidate_number):
            continue
        score = _score_company_candidate(candidate, tenant_name)
        scored.append((score, candidate))
    if not scored:
        return None, 0, 0, False
    scored.sort(key=lambda row: row[0], reverse=True)
    top_score, top_item = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    confident = top_score >= 75 and (top_score - second_score >= 12 or top_score >= 92)
    return top_item, top_score, second_score, confident


def populate_xero_lock_date_company_numbers(user: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    user_id = str((user or {}).get("id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User session is missing.")

    settings_row = _ensure_settings_row()
    environment = str(settings_row.get("environment") or "sandbox").strip().lower()
    api_key = _validated_companies_house_api_key(decrypt_api_key())
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configure a Companies House API key before populating company numbers.",
        )

    max_tenants = int(payload.get("limit") or 250)
    if max_tenants < 1:
        max_tenants = 1
    if max_tenants > 1000:
        max_tenants = 1000

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tenant_id, tenant_name
                FROM xero_connections
                WHERE user_id = %s
                ORDER BY updated_at DESC NULLS LAST, created_at DESC
                """,
                (user_id,),
            )
            tenant_rows = cursor.fetchall() or []
            tenant_ids = [str(row.get("tenant_id") or "").strip() for row in tenant_rows if row.get("tenant_id")]
            cursor.execute(
                """
                SELECT tenant_id, company_number, company_name, client_name
                FROM xero_tenant_company_mappings
                WHERE tenant_id = ANY(%s)
                """,
                (tenant_ids or [""],),
            )
            mapping_rows = cursor.fetchall() or []
            mapping_by_tenant = {str(row.get("tenant_id") or "").strip(): row for row in mapping_rows}
            cursor.execute(
                """
                SELECT company_number, company_name, client_name, company_status
                FROM ch_companies
                WHERE company_number <> ''
                """
            )
            local_company_rows = cursor.fetchall() or []
        connection.commit()

    pending_tenants = []
    for row in tenant_rows:
        tenant_id = str(row.get("tenant_id") or "").strip()
        if not tenant_id:
            continue
        mapped_number = str((mapping_by_tenant.get(tenant_id) or {}).get("company_number") or "").strip()
        if mapped_number:
            continue
        pending_tenants.append(
            {
                "tenantId": tenant_id,
                "tenantName": str(row.get("tenant_name") or "").strip(),
            }
        )
    pending_tenants = pending_tenants[:max_tenants]

    base_url = _companies_house_api_base(environment)
    populated: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    now = utcnow()

    with _companies_house_http_client(api_key) as client:
        for tenant in pending_tenants:
            tenant_id = tenant["tenantId"]
            tenant_name = tenant["tenantName"]
            query_text = re.sub(r"\s+", " ", str(tenant_name or "").strip())
            # Companies House search rejects certain malformed queries (400), so strip unsafe characters first.
            query_text = re.sub(r"[^A-Za-z0-9 &'().,/-]", " ", query_text)
            query_text = re.sub(r"\s+", " ", query_text).strip()
            if not query_text:
                skipped.append({"tenantId": tenant_id, "tenantName": tenant_name, "reason": "Tenant name is empty."})
                continue
            query_alnum = re.sub(r"[^A-Za-z0-9]", "", query_text)
            if len(query_alnum) < 2:
                skipped.append(
                    {
                        "tenantId": tenant_id,
                        "tenantName": tenant_name,
                        "reason": "Tenant name could not be converted into a valid Companies House search query.",
                    }
                )
                continue
            local_match, local_top_score, local_second_score, local_confident = _best_company_match_candidate(
                local_company_rows,
                tenant_name,
            )
            if local_match and local_confident:
                company_number = normalise_company_number(local_match.get("company_number"))
                company_name = str(local_match.get("company_name") or "").strip()
                with get_connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO xero_tenant_company_mappings (
                                tenant_id, company_number, client_name, company_name, notes, updated_by_user_id, created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (tenant_id) DO UPDATE
                            SET company_number = EXCLUDED.company_number,
                                client_name = EXCLUDED.client_name,
                                company_name = EXCLUDED.company_name,
                                updated_by_user_id = EXCLUDED.updated_by_user_id,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (
                                tenant_id,
                                company_number,
                                tenant_name,
                                company_name,
                                "Auto-populated from imported Companies House company list.",
                                user_id,
                                now,
                                now,
                            ),
                        )
                    connection.commit()
                populated.append(
                    {
                        "tenantId": tenant_id,
                        "tenantName": tenant_name,
                        "companyNumber": company_number,
                        "companyName": company_name,
                        "confidenceScore": local_top_score,
                    }
                )
                continue
            try:
                response = client.get(
                    f"{base_url}/search/companies",
                    params={"q": query_text, "items_per_page": 8},
                )
                if response.status_code in {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN}:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Companies House rejected the API credentials while searching for company numbers.",
                    )
                if response.is_error:
                    error_detail = ""
                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type:
                        try:
                            error_payload = response.json()
                        except ValueError:
                            error_payload = None
                        if isinstance(error_payload, dict):
                            if isinstance(error_payload.get("errors"), list):
                                flattened = [
                                    str(item.get("error") or item.get("message") or "").strip()
                                    for item in error_payload["errors"]
                                    if isinstance(item, dict)
                                ]
                                error_detail = "; ".join([item for item in flattened if item])
                            if not error_detail:
                                error_detail = str(
                                    error_payload.get("error")
                                    or error_payload.get("message")
                                    or error_payload.get("detail")
                                    or ""
                                ).strip()
                        elif isinstance(error_payload, str):
                            error_detail = error_payload.strip()
                    if not error_detail:
                        error_detail = (response.text or "").strip()
                    if len(error_detail) > 180:
                        error_detail = f"{error_detail[:177]}..."
                    include_query = True
                    if (
                        response.status_code == status.HTTP_400_BAD_REQUEST
                        and "invalid authorization header" in error_detail.lower()
                    ):
                        reason = (
                            "Companies House rejected the API key format. "
                            "In Settings, save only the raw API key (no Basic/Bearer prefix, no spaces)."
                        )
                        include_query = False
                    else:
                        reason = f"Companies House search failed ({response.status_code})."
                    if error_detail:
                        reason = f"{reason} {error_detail}"
                    if include_query:
                        reason = f"{reason} Query: \"{query_text}\"."
                    failed.append(
                        {
                            "tenantId": tenant_id,
                            "tenantName": tenant_name,
                            "reason": reason,
                        }
                    )
                    continue
                payload_data = response.json() if response.content else {}
                items = payload_data.get("items") if isinstance(payload_data, dict) else []
                if not isinstance(items, list) or not items:
                    skipped.append({"tenantId": tenant_id, "tenantName": tenant_name, "reason": "No CH search matches."})
                    continue

                ch_candidates = [
                    {
                        "company_number": item.get("company_number"),
                        "company_name": item.get("title"),
                        "client_name": "",
                        "company_status": item.get("company_status"),
                    }
                    for item in items
                    if isinstance(item, dict)
                ]
                top_item, top_score, second_score, confident = _best_company_match_candidate(ch_candidates, tenant_name)
                if not top_item:
                    skipped.append({"tenantId": tenant_id, "tenantName": tenant_name, "reason": "No valid CH company number in results."})
                    continue
                if not confident:
                    skipped.append(
                        {
                            "tenantId": tenant_id,
                            "tenantName": tenant_name,
                            "reason": f"Match confidence too low (top {top_score}, next {second_score}).",
                        }
                    )
                    continue

                company_number = normalise_company_number(top_item.get("company_number"))
                company_name = str(top_item.get("title") or "").strip()
                with get_connection() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO xero_tenant_company_mappings (
                                tenant_id, company_number, client_name, company_name, notes, updated_by_user_id, created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (tenant_id) DO UPDATE
                            SET company_number = EXCLUDED.company_number,
                                client_name = EXCLUDED.client_name,
                                company_name = EXCLUDED.company_name,
                                updated_by_user_id = EXCLUDED.updated_by_user_id,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (
                                tenant_id,
                                company_number,
                                tenant_name,
                                company_name,
                                "Auto-populated from Companies House search.",
                                user_id,
                                now,
                                now,
                            ),
                        )
                    connection.commit()
                populated.append(
                    {
                        "tenantId": tenant_id,
                        "tenantName": tenant_name,
                        "companyNumber": company_number,
                        "companyName": company_name,
                        "confidenceScore": top_score,
                    }
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("Unable to auto-populate CH number for tenant %s", tenant_id)
                failed.append(
                    {
                        "tenantId": tenant_id,
                        "tenantName": tenant_name,
                        "reason": str(exc) or "Unexpected error during CH search.",
                    }
                )

    return {
        "summary": {
            "reviewedTenants": len(pending_tenants),
            "populatedCount": len(populated),
            "skippedCount": len(skipped),
            "failedCount": len(failed),
        },
        "populated": populated,
        "skipped": skipped,
        "failed": failed,
    }


def sync_xero_lock_date_company_records(user: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    user_id = str((user or {}).get("id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User session is missing.")

    max_companies = int(payload.get("limit") or 250)
    if max_companies < 1:
        max_companies = 1
    if max_companies > 1000:
        max_companies = 1000

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT UPPER(TRIM(m.company_number)) AS company_number
                FROM xero_tenant_company_mappings m
                INNER JOIN xero_connections x ON x.tenant_id = m.tenant_id
                WHERE x.user_id = %s
                  AND TRIM(m.company_number) <> ''
                ORDER BY UPPER(TRIM(m.company_number)) ASC
                LIMIT %s
                """,
                (user_id, max_companies),
            )
            company_rows = cursor.fetchall() or []
        connection.commit()

    company_numbers = [
        normalise_company_number(row.get("company_number"))
        for row in company_rows
        if normalise_company_number(row.get("company_number"))
    ]
    if not company_numbers:
        return {
            "summary": {
                "targetCount": 0,
                "syncedCount": 0,
                "failedCount": 0,
            },
            "synced": [],
            "failed": [],
        }

    synced: list[dict] = []
    failed: list[dict] = []
    for company_number in company_numbers:
        try:
            snapshot = _fetch_ch_company_snapshot(company_number)
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO ch_companies (company_number, company_name, filing_authority_status, updated_at)
                        VALUES (%s, %s, 'authorised', NOW())
                        ON CONFLICT (company_number) DO UPDATE
                        SET company_name = COALESCE(NULLIF(EXCLUDED.company_name, ''), ch_companies.company_name),
                            updated_at = NOW()
                        RETURNING id
                        """,
                        (
                            company_number,
                            snapshot.get("companyName") or "",
                        ),
                    )
                    row = cursor.fetchone() or {}
                    company_id = str(row.get("id") or "")
                    if not company_id:
                        raise RuntimeError("Unable to resolve CH company ID for sync.")
                    _apply_company_snapshot(cursor, company_id, snapshot)
                connection.commit()
            synced.append(
                {
                    "companyNumber": company_number,
                    "companyName": str(snapshot.get("companyName") or ""),
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "companyNumber": company_number,
                    "reason": str(exc) or "Unexpected sync failure.",
                }
            )

    return {
        "summary": {
            "targetCount": len(company_numbers),
            "syncedCount": len(synced),
            "failedCount": len(failed),
        },
        "synced": synced,
        "failed": failed,
    }


def _normalise_header(header: str) -> str:
    text = str(header or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[_/\\-]+", " ", text)
    text = re.sub(r"[^\w\s]+", " ", text)
    text = re.sub(r"\be\s*mail\b", "email", text)
    return re.sub(r"\s+", " ", text).strip()


def _resolve_header_map(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lowered = [_normalise_header(header) for header in headers]
    for canonical, aliases in CLIENT_IMPORT_HEADER_ALIASES.items():
        canonical_header = _normalise_header(canonical.replace("_", " "))
        normalised_aliases = {_normalise_header(alias) for alias in aliases}
        for index, header in enumerate(lowered):
            if not header:
                continue
            if header == canonical_header or header in normalised_aliases:
                mapping[canonical] = index
                break
    return mapping


def _load_last_import_header_profile() -> dict[str, str]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT summary
                FROM ch_imports
                WHERE import_type = 'clients'
                  AND status = 'completed'
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone() or {}
        connection.commit()
    summary = row.get("summary") or {}
    profile = summary.get("headerProfile") if isinstance(summary, dict) else {}
    if not isinstance(profile, dict):
        return {}
    return {str(key): str(value) for key, value in profile.items() if key in CLIENT_IMPORT_HEADER_ALIASES and value}


def _apply_header_profile(headers: list[str], mapping: dict[str, int], profile: dict[str, str]) -> dict[str, int]:
    if not profile:
        return mapping
    header_index = {_normalise_header(header): idx for idx, header in enumerate(headers)}
    output = dict(mapping)
    for canonical, header_name in profile.items():
        if canonical in output:
            continue
        idx = header_index.get(_normalise_header(header_name))
        if idx is not None:
            output[canonical] = idx
    return output


def _ai_resolve_header_map(headers: list[str], current_map: dict[str, int]) -> dict[str, int]:
    settings = get_settings()
    if not settings.openai_api_key:
        return current_map
    unresolved = [key for key in CLIENT_IMPORT_HEADER_ALIASES.keys() if key not in current_map]
    if not unresolved:
        return current_map
    try:
        started = time.monotonic()
        request_body = {
            "model": settings.openai_model or "gpt-4.1-mini",
            "input": [
                {
                    "role": "system",
                    "content": (
                        "Map CSV headers to canonical Companies House client import fields. "
                        "Return strict JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "canonicalFields": unresolved,
                            "headers": headers,
                            "rules": [
                                "Return object with key 'mapping'.",
                                "Each mapping value must be exact header text from provided headers.",
                                "Do not guess when unclear; omit that field.",
                            ],
                        }
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "csv_header_mapping",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "mapping": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            }
                        },
                        "required": ["mapping"],
                        "additionalProperties": False,
                    },
                    "strict": True,
                }
            },
        }
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        feature, page = infer_openai_feature_page("companies house header mapping")
        request_bytes = len(json.dumps(request_body))
        response_bytes = len(response.content or b"")
        if response.is_error:
            record_usage_event(
                provider="openai",
                user_id=None,
                feature=feature,
                page=page,
                operation="companies house header mapping",
                endpoint="/v1/responses",
                model=str(request_body.get("model") or settings.openai_model or ""),
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                status_code=response.status_code,
                success=False,
                error_message=str(response.text or "")[:500],
                duration_ms=elapsed_ms,
            )
            return current_map
        payload = response.json()
        input_tokens, output_tokens, total_tokens = parse_openai_usage_tokens(payload)
        record_usage_event(
            provider="openai",
            user_id=None,
            feature=feature,
            page=page,
            operation="companies house header mapping",
            endpoint="/v1/responses",
            model=str(request_body.get("model") or settings.openai_model or ""),
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimate_openai_cost_usd(str(request_body.get("model") or settings.openai_model or ""), input_tokens, output_tokens),
            status_code=response.status_code,
            success=True,
            duration_ms=elapsed_ms,
        )
        output_text = ""
        for item in payload.get("output") or []:
            for content in item.get("content") or []:
                text_value = content.get("text")
                if text_value:
                    output_text += text_value
        if not output_text.strip():
            return current_map
        parsed = json.loads(output_text)
        mapping = dict(current_map)
        header_to_index = {_normalise_header(header): idx for idx, header in enumerate(headers)}
        for canonical, header in (parsed.get("mapping") or {}).items():
            if canonical in mapping:
                continue
            idx = header_to_index.get(_normalise_header(header))
            if idx is not None and canonical in CLIENT_IMPORT_HEADER_ALIASES:
                mapping[canonical] = idx
        return mapping
    except Exception:
        return current_map


def _parse_date_from_text(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _company_import_classification(row_payload: dict) -> str:
    company_type = str(row_payload.get("company_type") or "").strip().lower()
    company_name = str(row_payload.get("company_name") or row_payload.get("client_name") or "").strip().lower()
    combined = f"{company_type} {company_name}".strip()
    exclude_terms = ("sole trader", "self employed", "self-employed", "individual", "partnership", "llp")
    if any(term in combined for term in exclude_terms):
        return "exclude"
    if "private limited" in combined:
        return "include"
    if re.search(r"\bltd\b", company_name) or "limited" in company_name:
        return "include"
    if "limited company" in company_type or "ltd company" in company_type:
        return "include"
    return "review"


def _looks_private_limited(row_payload: dict) -> bool:
    return _company_import_classification(row_payload) == "include"


def _coerce_text(value: object, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").strip()
    return text[:limit]


def _decode_upload(content: bytes) -> str:
    if not content:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _existing_companies_by_number(numbers: list[str]) -> dict[str, dict]:
    if not numbers:
        return {}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.*,
                       (a.id IS NOT NULL) AS auth_code_on_file
                FROM ch_companies c
                LEFT JOIN ch_auth_codes a ON a.company_id = c.id
                WHERE c.company_number = ANY(%s)
                """,
                (numbers,),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return {row["company_number"]: row for row in rows}


def _import_preview_deadline_sort_key(row: dict) -> tuple[int, date, int]:
    data = row.get("data") or {}
    ch_due = _parse_date_from_text(data.get("ch_due_date_iso") or data.get("ch_due_date"))
    file_due = _parse_date_from_text(data.get("due_date_iso") or data.get("due_date"))
    deadline = ch_due or file_due
    line_number = int(row.get("lineNumber") or 0)
    return (0 if deadline else 1, deadline or date.max, line_number)


def _is_blank_text(value: object) -> bool:
    return not str(value or "").strip()


def _import_set_date_fields(payload: dict[str, str], field_prefix: str, value: date | None) -> None:
    iso_key = f"{field_prefix}_iso"
    text_key = field_prefix
    if not isinstance(value, date):
        return
    if _is_blank_text(payload.get(iso_key)):
        payload[iso_key] = value.isoformat()
    if _is_blank_text(payload.get(text_key)):
        payload[text_key] = value.isoformat()


def _apply_import_enrichment_from_existing(row_payload: dict[str, str], existing_row: dict | None) -> None:
    if not isinstance(existing_row, dict):
        return
    if _is_blank_text(row_payload.get("company_name")):
        row_payload["company_name"] = _coerce_text(existing_row.get("company_name"), 250)
    if _is_blank_text(row_payload.get("client_name")):
        row_payload["client_name"] = _coerce_text(existing_row.get("client_name"), 250)
    if _is_blank_text(row_payload.get("assigned_staff")):
        row_payload["assigned_staff"] = _coerce_text(existing_row.get("assigned_staff_name"), 250)
    if _is_blank_text(row_payload.get("client_address")):
        row_payload["client_address"] = _coerce_text(existing_row.get("client_address"), 1000)
    _import_set_date_fields(row_payload, "period_end", existing_row.get("next_made_up_to_date"))
    _import_set_date_fields(row_payload, "due_date", existing_row.get("next_due_date"))
    next_due_date = existing_row.get("next_due_date")
    row_payload["ch_due_date_iso"] = next_due_date.isoformat() if isinstance(next_due_date, date) else ""


def _apply_import_enrichment_from_snapshot(row_payload: dict[str, str], snapshot: dict | None) -> None:
    if not isinstance(snapshot, dict):
        return
    if _is_blank_text(row_payload.get("company_name")):
        row_payload["company_name"] = _coerce_text(snapshot.get("companyName"), 250)
    _import_set_date_fields(row_payload, "period_end", snapshot.get("nextMadeUpToDate"))
    _import_set_date_fields(row_payload, "due_date", snapshot.get("nextDueDate"))
    next_due_date = snapshot.get("nextDueDate")
    row_payload["ch_due_date_iso"] = next_due_date.isoformat() if isinstance(next_due_date, date) else ""


def parse_clients_import(content: bytes, filename: str) -> dict:
    text = _decode_upload(content)
    if not text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The upload was empty. Save the BM Client export as CSV and try again.",
        )

    try:
        reader = csv.reader(io.StringIO(text))
        header_row = next(reader, [])
        rows = list(reader)
    except csv.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unable to read the CSV: {exc}",
        ) from exc

    headers = [_coerce_text(value, 120) for value in header_row]
    prior_profile = _load_last_import_header_profile()
    column_map = _resolve_header_map(headers)
    column_map = _apply_header_profile(headers, column_map, prior_profile)
    column_map = _ai_resolve_header_map(headers, column_map)
    if "company_number" not in column_map:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "The CSV must include a 'Company number' column. "
                "Header was: " + ", ".join(headers[:8])
            ),
        )

    normalised_headers = [_normalise_header(header) for header in headers]
    auth_fallback_indexes = [
        idx
        for idx, header in enumerate(normalised_headers)
        if ("auth" in header or "authentication" in header) and "code" in header
    ]

    parsed_rows: list[dict] = []
    errors: list[dict] = []
    seen_numbers: set[str] = set()
    duplicate_numbers: set[str] = set()

    for index, raw_row in enumerate(rows, start=2):
        row_payload: dict[str, str] = {}
        for canonical, column_index in column_map.items():
            value = raw_row[column_index] if column_index < len(raw_row) else ""
            row_payload[canonical] = _coerce_text(value, 2000 if canonical == "notes" else 250)
        if _is_blank_text(row_payload.get("auth_code")) and auth_fallback_indexes:
            for fallback_index in auth_fallback_indexes:
                value = raw_row[fallback_index] if fallback_index < len(raw_row) else ""
                candidate = _coerce_text(value, 250)
                if candidate:
                    row_payload["auth_code"] = candidate
                    break
        # BM export contract: confirmation statement period end is always JN, deadline is always JO.
        period_end_from_jn = raw_row[CLIENT_IMPORT_PERIOD_END_COLUMN_INDEX] if CLIENT_IMPORT_PERIOD_END_COLUMN_INDEX < len(raw_row) else ""
        deadline_from_jo = raw_row[CLIENT_IMPORT_DEADLINE_COLUMN_INDEX] if CLIENT_IMPORT_DEADLINE_COLUMN_INDEX < len(raw_row) else ""
        row_payload["period_end"] = _coerce_text(period_end_from_jn, 250)
        row_payload["due_date"] = _coerce_text(deadline_from_jo, 250)
        row_payload["assigned_staff"] = row_payload.get("assigned_staff") or row_payload.get("manager_reference") or ""
        company_number = normalise_company_number(row_payload.get("company_number"))
        row_payload["company_number"] = company_number
        period_end = _parse_date_from_text(row_payload.get("period_end"))
        due_date = _parse_date_from_text(row_payload.get("due_date"))
        row_payload["period_end_iso"] = period_end.isoformat() if period_end else ""
        row_payload["due_date_iso"] = due_date.isoformat() if due_date else ""

        row_errors: list[str] = []
        if not company_number:
            row_errors.append("Missing company number.")
        elif not _is_valid_company_number(company_number):
            row_errors.append("Company number must be 8 alphanumeric characters.")

        if not row_payload.get("company_name") and not row_payload.get("client_name"):
            row_errors.append("Provide either a company name or a client name.")

        if company_number and company_number in seen_numbers:
            duplicate_numbers.add(company_number)
        if company_number:
            seen_numbers.add(company_number)

        parsed_rows.append({
            "lineNumber": index,
            "data": row_payload,
            "errors": row_errors,
            "warnings": [],
        })

    valid_numbers = [
        row["data"]["company_number"]
        for row in parsed_rows
        if row["data"]["company_number"] and _is_valid_company_number(row["data"]["company_number"])
    ]
    existing = _existing_companies_by_number(valid_numbers)
    snapshot_cache: dict[str, dict | None] = {}
    snapshot_lookup_blocked = False

    for row in parsed_rows:
        data = row["data"]
        company_number = data.get("company_number") or ""
        if not company_number or not _is_valid_company_number(company_number):
            data["ch_due_date_iso"] = ""
            continue

        _apply_import_enrichment_from_existing(data, existing.get(company_number))
        needs_snapshot = (
            _is_blank_text(data.get("company_name"))
            or _is_blank_text(data.get("period_end_iso"))
            or _is_blank_text(data.get("due_date_iso"))
            or _is_blank_text(data.get("ch_due_date_iso"))
        )
        if needs_snapshot and not snapshot_lookup_blocked:
            if company_number not in snapshot_cache:
                try:
                    snapshot_cache[company_number] = _fetch_ch_company_snapshot(company_number)
                except HTTPException as exc:
                    detail_text = str(exc.detail or "").lower()
                    if "configure a companies house api key" in detail_text:
                        snapshot_lookup_blocked = True
                    snapshot_cache[company_number] = None
                except Exception:
                    snapshot_cache[company_number] = None
            _apply_import_enrichment_from_snapshot(data, snapshot_cache.get(company_number))

        period_end = _parse_date_from_text(data.get("period_end_iso") or data.get("period_end"))
        due_date = _parse_date_from_text(data.get("due_date_iso") or data.get("due_date"))
        data["period_end_iso"] = period_end.isoformat() if period_end else ""
        data["due_date_iso"] = due_date.isoformat() if due_date else ""
        if _is_blank_text(data.get("ch_due_date_iso")):
            data["ch_due_date_iso"] = data["due_date_iso"] or ""
        has_name = bool(str(data.get("company_name") or data.get("client_name") or "").strip())
        row["errors"] = [message for message in (row.get("errors") or []) if message != "Provide either a company name or a client name."]
        if not has_name:
            row["errors"].append("Provide either a company name or a client name.")

    create_count = 0
    update_count = 0
    skip_count = 0
    error_count = 0
    auth_codes_in_file = 0
    selected_count = 0
    excluded_non_ltd_count = 0
    review_required_count = 0
    auth_code_backfill_count = 0

    for row in parsed_rows:
        data = row["data"]
        company_number = data["company_number"]
        if company_number and company_number in duplicate_numbers and not row["errors"]:
            row["errors"].append("Duplicate company number within this file.")
        if data.get("auth_code"):
            auth_codes_in_file += 1
        company_classification = _company_import_classification(data)
        row["classification"] = company_classification
        row["included"] = company_classification == "include"
        if row["errors"]:
            error_count += 1
            row["action"] = "error"
            continue
        if company_classification == "exclude":
            excluded_non_ltd_count += 1
            row["action"] = "skip"
            row["warnings"].append("Auto-excluded: this row is not a private limited company.")
            continue
        if company_classification == "review":
            review_required_count += 1
            row["warnings"].append("Review required: unable to confidently classify this row as a private limited company.")
        if company_number in existing:
            update_count += 1
            row["action"] = "update"
            row["existingCompany"] = {
                "id": str(existing[company_number]["id"]),
                "companyName": existing[company_number].get("company_name") or "",
            }
            has_auth_in_file = bool((data.get("auth_code") or "").strip())
            has_auth_on_record = bool(existing[company_number].get("auth_code_on_file"))
            if has_auth_in_file and not has_auth_on_record:
                auth_code_backfill_count += 1
                row["warnings"].append("Authentication code will be saved onto the existing company record.")
        else:
            create_count += 1
            row["action"] = "create"
        if row["included"]:
            selected_count += 1

    if not parsed_rows:
        skip_count = 0
    visible_rows = [row for row in parsed_rows if row.get("included") or row.get("errors") or row.get("classification") == "review"]
    visible_rows.sort(key=_import_preview_deadline_sort_key)

    return {
        "filename": filename,
        "totalRows": len(parsed_rows),
        "visibleRows": len(visible_rows),
        "createCount": create_count,
        "updateCount": update_count,
        "skipCount": skip_count,
        "errorCount": error_count,
        "authCodesInFile": auth_codes_in_file,
        "selectedCount": selected_count,
        "excludedNonLtdCount": excluded_non_ltd_count,
        "reviewRequiredCount": review_required_count,
        "authCodeBackfillCount": auth_code_backfill_count,
        "rows": visible_rows,
        "headers": headers,
        "headerProfile": {
            canonical: headers[index]
            for canonical, index in column_map.items()
            if 0 <= index < len(headers)
        },
        "duplicateNumbers": sorted(duplicate_numbers),
    }


def _upsert_company(cursor, data: dict, user_id: str | None) -> tuple[str, str]:
    company_number = data["company_number"]
    cursor.execute(
        """
        INSERT INTO ch_companies (
            company_number,
            company_name,
            client_id,
            client_name,
            contact_email,
            contact_phone,
            client_address,
            assigned_staff_name,
            filing_authority_status,
            notes,
            next_made_up_to_date,
            next_due_date
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'authorised', %s, NULLIF(%s, '')::date, NULLIF(%s, '')::date)
        ON CONFLICT (company_number) DO UPDATE
        SET company_name = COALESCE(NULLIF(EXCLUDED.company_name, ''), ch_companies.company_name),
            client_id = COALESCE(NULLIF(EXCLUDED.client_id, ''), ch_companies.client_id),
            client_name = COALESCE(NULLIF(EXCLUDED.client_name, ''), ch_companies.client_name),
            contact_email = COALESCE(NULLIF(EXCLUDED.contact_email, ''), ch_companies.contact_email),
            contact_phone = COALESCE(NULLIF(EXCLUDED.contact_phone, ''), ch_companies.contact_phone),
            client_address = COALESCE(NULLIF(EXCLUDED.client_address, ''), ch_companies.client_address),
            assigned_staff_name = COALESCE(NULLIF(EXCLUDED.assigned_staff_name, ''), ch_companies.assigned_staff_name),
            notes = COALESCE(NULLIF(EXCLUDED.notes, ''), ch_companies.notes),
            next_made_up_to_date = COALESCE(EXCLUDED.next_made_up_to_date, ch_companies.next_made_up_to_date),
            next_due_date = COALESCE(EXCLUDED.next_due_date, ch_companies.next_due_date),
            updated_at = NOW()
        RETURNING id, company_number, (xmax = 0) AS created
        """,
        (
            company_number,
            data.get("company_name") or "",
            data.get("client_id") or "",
            data.get("client_name") or "",
            data.get("contact_email") or "",
            data.get("contact_phone") or "",
            data.get("client_address") or "",
            data.get("assigned_staff") or "",
            data.get("notes") or "",
            data.get("period_end_iso") or "",
            data.get("due_date_iso") or "",
        ),
    )
    row = cursor.fetchone()
    return str(row["id"]), "create" if row["created"] else "update"


def _save_company_auth_code(cursor, company_id: str, code: str, user_id: str | None) -> None:
    code = (code or "").strip()
    if not code:
        return
    encrypted = encrypt_secret(code, f"{CH_COMPANY_AUTH_LABEL}:{company_id}")
    hint = _mask(code)
    cursor.execute(
        """
        INSERT INTO ch_auth_codes (company_id, code_encrypted, code_hint, status, uploaded_by_user_id, uploaded_at)
        VALUES (%s, %s, %s, 'active', %s, NOW())
        ON CONFLICT (company_id) DO UPDATE
        SET code_encrypted = EXCLUDED.code_encrypted,
            code_hint = EXCLUDED.code_hint,
            status = 'active',
            uploaded_by_user_id = EXCLUDED.uploaded_by_user_id,
            uploaded_at = NOW(),
            updated_at = NOW()
        """,
        (company_id, encrypted, hint, user_id),
    )


def _encrypt_register_auth_code(code: str, register_key: str) -> str:
    return encrypt_secret(code, f"{CH_COMPANY_AUTH_LABEL}:register:{register_key}")


def _decrypt_register_auth_code(encrypted: str, register_id: str, company_number: str, normalised_name: str) -> str:
    candidates = [
        f"{CH_COMPANY_AUTH_LABEL}:register:{register_id}",
        f"{CH_COMPANY_AUTH_LABEL}:register:{company_number}",
        f"{CH_COMPANY_AUTH_LABEL}:register:{normalised_name}",
    ]
    for label in candidates:
        try:
            return decrypt_secret(encrypted, label)
        except Exception:
            continue
    return ""


def _auth_register_name(row_payload: dict[str, str]) -> str:
    return _coerce_text(
        row_payload.get("company_name")
        or row_payload.get("client_name")
        or "",
        250,
    )


def _normalise_client_type(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "self assessment" in text or "individual" in text or "sole trader" in text:
        return "Individual"
    if "private limited" in text or "ltd" in text or "limited company" in text or "company" in text:
        return "Company"
    return _coerce_text(value, 80)


def _upsert_auth_code_register_row(
    cursor,
    *,
    company_number: str,
    display_name: str,
    client_type: str,
    client_manager: str,
    client_id: str,
    normalised_name: str,
    auth_code: str,
    filename: str,
    user_id: str | None,
) -> tuple[str, str]:
    register_key = company_number or normalised_name
    encrypted = _encrypt_register_auth_code(auth_code, register_key)
    hint = _mask(auth_code)
    updated_row = None
    if company_number:
        cursor.execute(
            """
            UPDATE ch_auth_code_register
            SET client_name = %s,
                company_name = %s,
                client_type = COALESCE(NULLIF(%s, ''), client_type),
                client_manager = COALESCE(NULLIF(%s, ''), client_manager),
                client_id = COALESCE(NULLIF(%s, ''), client_id),
                normalised_name = %s,
                code_encrypted = %s,
                code_hint = %s,
                source_filename = %s,
                uploaded_by_user_id = %s,
                uploaded_at = NOW(),
                updated_at = NOW()
            WHERE company_number = %s
            RETURNING id
            """,
            (
                display_name,
                display_name,
                client_type,
                client_manager,
                client_id,
                normalised_name,
                encrypted,
                hint,
                filename,
                user_id,
                company_number,
            ),
        )
        updated_row = cursor.fetchone()
    else:
        cursor.execute(
            """
            UPDATE ch_auth_code_register
            SET client_name = %s,
                company_name = %s,
                client_type = COALESCE(NULLIF(%s, ''), client_type),
                client_manager = COALESCE(NULLIF(%s, ''), client_manager),
                client_id = COALESCE(NULLIF(%s, ''), client_id),
                code_encrypted = %s,
                code_hint = %s,
                source_filename = %s,
                uploaded_by_user_id = %s,
                uploaded_at = NOW(),
                updated_at = NOW()
            WHERE company_number = ''
              AND normalised_name = %s
            RETURNING id
            """,
            (
                display_name,
                display_name,
                client_type,
                client_manager,
                client_id,
                encrypted,
                hint,
                filename,
                user_id,
                normalised_name,
            ),
        )
        updated_row = cursor.fetchone()
    if updated_row:
        return str(updated_row.get("id") or ""), "updated"
    cursor.execute(
        """
        INSERT INTO ch_auth_code_register (
            company_number,
            client_name,
            company_name,
            client_type,
            client_manager,
            client_id,
            normalised_name,
            code_encrypted,
            code_hint,
            source_filename,
            uploaded_by_user_id,
            uploaded_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        RETURNING id
        """,
        (
            company_number,
            display_name,
            display_name,
            client_type,
            client_manager,
            client_id,
            normalised_name,
            encrypted,
            hint,
            filename,
            user_id,
        ),
    )
    inserted = cursor.fetchone() or {}
    return str(inserted.get("id") or ""), "created"


def _sync_auth_register_contacts_to_company(
    cursor,
    *,
    company_number: str,
    display_name: str,
    client_id: str,
    client_manager: str,
    contact_email: str,
    contact_phone: str,
    client_address: str,
    user_id: str | None,
) -> None:
    safe_company_number = normalise_company_number(company_number)
    if not safe_company_number:
        return
    if not any(
        (
            str(contact_email or "").strip(),
            str(contact_phone or "").strip(),
            str(client_address or "").strip(),
            str(client_id or "").strip(),
            str(client_manager or "").strip(),
        )
    ):
        return
    _upsert_company(
        cursor,
        {
            "company_number": safe_company_number,
            "company_name": _coerce_text(display_name, 250),
            "client_name": _coerce_text(display_name, 250),
            "client_id": _coerce_text(client_id, 80),
            "contact_email": _coerce_text(contact_email, 250),
            "contact_phone": _coerce_text(contact_phone, 120),
            "client_address": _coerce_text(client_address, 1000),
            "assigned_staff": _coerce_text(client_manager, 120),
            "notes": "",
            "period_end_iso": "",
            "due_date_iso": "",
        },
        user_id,
    )


def _parse_auth_code_register_csv(content: bytes) -> tuple[list[dict], list[dict]]:
    text = _decode_upload(content)
    if not text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The uploaded file is empty.")
    try:
        reader = csv.reader(io.StringIO(text))
        header_row = next(reader, [])
        rows = list(reader)
    except csv.Error as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unable to read CSV: {exc}") from exc
    headers = [_coerce_text(value, 120) for value in header_row]
    mapping = _resolve_header_map(headers)
    mapping = _apply_header_profile(headers, mapping, _load_last_import_header_profile())
    mapping = _ai_resolve_header_map(headers, mapping)
    if "auth_code" not in mapping:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CSV must include an auth code column (for example 'Auth Code' or 'Authentication Code').",
        )
    output_rows: list[dict] = []
    errors: list[dict] = []
    # BM export fallback: client type in column B and client ID in column E for some files.
    bm_client_type_column_index = 1
    bm_client_id_column_index = 4
    for idx, raw_row in enumerate(rows, start=2):
        row_payload: dict[str, str] = {}
        for canonical, column_index in mapping.items():
            row_payload[canonical] = _coerce_text(raw_row[column_index] if column_index < len(raw_row) else "", 250)
        if not _coerce_text(row_payload.get("company_type"), 80) and bm_client_type_column_index < len(raw_row):
            row_payload["company_type"] = _coerce_text(raw_row[bm_client_type_column_index], 80)
        if not _coerce_text(row_payload.get("client_id"), 80) and bm_client_id_column_index < len(raw_row):
            row_payload["client_id"] = _coerce_text(raw_row[bm_client_id_column_index], 80)
        auth_code = _coerce_text(row_payload.get("auth_code"), 80)
        company_number = normalise_company_number(row_payload.get("company_number"))
        display_name = _auth_register_name(row_payload)
        client_type = _normalise_client_type(row_payload.get("company_type"))
        normalised_name = _normalise_company_name_for_match(display_name)
        if not auth_code:
            continue
        if not company_number and not normalised_name:
            errors.append({"lineNumber": idx, "reason": "Missing company number and name; cannot match this auth code."})
            continue
        output_rows.append(
            {
                "lineNumber": idx,
                "companyNumber": company_number,
                "displayName": display_name,
                "clientType": client_type,
                "clientManager": _coerce_text(row_payload.get("manager_reference") or row_payload.get("assigned_staff"), 120),
                "clientId": _coerce_text(row_payload.get("client_id"), 80),
                "contactEmail": _coerce_text(row_payload.get("contact_email"), 250),
                "contactPhone": _coerce_text(row_payload.get("contact_phone"), 120),
                "clientAddress": _coerce_text(row_payload.get("client_address"), 1000),
                "normalisedName": normalised_name,
                "authCode": auth_code,
            }
        )
    return output_rows, errors


def upload_auth_code_register_csv(user: dict, content: bytes, filename: str) -> dict:
    rows, parse_errors = _parse_auth_code_register_csv(content)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No auth codes found in file. Include client/company name and auth code columns.",
        )
    user_id = user.get("id") if isinstance(user, dict) else None
    created_count = 0
    updated_count = 0
    with get_connection() as connection:
        with connection.cursor() as cursor:
            for row in rows:
                _, action = _upsert_auth_code_register_row(
                    cursor,
                    company_number=row["companyNumber"],
                    display_name=row["displayName"],
                    client_type=row.get("clientType") or "",
                    client_manager=row.get("clientManager") or "",
                    client_id=row.get("clientId") or "",
                    normalised_name=row["normalisedName"],
                    auth_code=row["authCode"],
                    filename=_coerce_text(filename, 250),
                    user_id=user_id,
                )
                _sync_auth_register_contacts_to_company(
                    cursor,
                    company_number=row["companyNumber"],
                    display_name=row["displayName"],
                    client_id=row.get("clientId") or "",
                    client_manager=row.get("clientManager") or "",
                    contact_email=row.get("contactEmail") or "",
                    contact_phone=row.get("contactPhone") or "",
                    client_address=row.get("clientAddress") or "",
                    user_id=user_id,
                )
                if action == "created":
                    created_count += 1
                else:
                    updated_count += 1
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_auth_code_register', 'bulk', 'auth_code_register_uploaded', %s::jsonb, %s)
                """,
                (
                    json.dumps(
                        {
                            "filename": _coerce_text(filename, 250),
                            "rows": len(rows),
                            "created": created_count,
                            "updated": updated_count,
                            "errors": len(parse_errors),
                        }
                    ),
                    user_id,
                ),
            )
        connection.commit()
    return {
        "filename": _coerce_text(filename, 250),
        "rowCount": len(rows),
        "createdCount": created_count,
        "updatedCount": updated_count,
        "errorCount": len(parse_errors),
        "errors": parse_errors[:100],
    }


def _auth_register_match_key(company_number: str, normalised_name: str) -> str:
    number = normalise_company_number(company_number)
    name = _coerce_text(normalised_name, 250)
    if number:
        return f"number:{number}"
    return f"name:{name}"


def preview_auth_code_register_csv(content: bytes, filename: str) -> dict:
    rows, parse_errors = _parse_auth_code_register_csv(content)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No auth codes found in file. Include client/company name and auth code columns.",
        )

    incoming_by_key: dict[str, dict] = {}
    for row in rows:
        key = _auth_register_match_key(row.get("companyNumber") or "", row.get("normalisedName") or "")
        if key in incoming_by_key:
            parse_errors.append(
                {
                    "lineNumber": row.get("lineNumber"),
                    "reason": "Duplicate auth code entry in file. Keep only one row per company number/name.",
                }
            )
            continue
        incoming_by_key[key] = row

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id,
                       company_number,
                       normalised_name,
                       COALESCE(NULLIF(company_name, ''), client_name, '') AS display_name,
                       client_type,
                       client_id,
                       client_manager,
                       source_filename,
                       uploaded_at
                FROM ch_auth_code_register
                ORDER BY uploaded_at DESC
                """
            )
            existing_rows = cursor.fetchall() or []
        connection.commit()

    existing_by_key: dict[str, dict] = {}
    for row in existing_rows:
        key = _auth_register_match_key(row.get("company_number") or "", row.get("normalised_name") or "")
        if key and key not in existing_by_key:
            existing_by_key[key] = row

    create_rows: list[dict] = []
    update_rows: list[dict] = []
    rows_to_upsert: list[dict] = []
    for key, row in incoming_by_key.items():
        existing = existing_by_key.get(key)
        row_payload = {
            "lineNumber": row.get("lineNumber"),
            "companyNumber": row.get("companyNumber") or "",
            "displayName": row.get("displayName") or "",
            "clientType": _normalise_client_type(row.get("clientType")),
            "clientManager": row.get("clientManager") or "",
            "clientId": row.get("clientId") or "",
            "contactEmail": row.get("contactEmail") or "",
            "contactPhone": row.get("contactPhone") or "",
            "clientAddress": row.get("clientAddress") or "",
            "normalisedName": row.get("normalisedName") or "",
            "authCode": row.get("authCode") or "",
        }
        rows_to_upsert.append(row_payload)
        if existing:
            update_rows.append(
                {
                    "lineNumber": row_payload["lineNumber"],
                    "companyNumber": row_payload["companyNumber"],
                    "displayName": row_payload["displayName"],
                    "clientType": row_payload["clientType"],
                    "clientManager": row_payload["clientManager"],
                    "clientId": row_payload["clientId"],
                    "contactEmail": row_payload["contactEmail"],
                    "contactPhone": row_payload["contactPhone"],
                    "clientAddress": row_payload["clientAddress"],
                    "existingDisplayName": existing.get("display_name") or "",
                    "existingClientType": existing.get("client_type") or "",
                    "existingClientManager": existing.get("client_manager") or "",
                    "existingClientId": existing.get("client_id") or "",
                }
            )
        else:
            create_rows.append(
                {
                    "lineNumber": row_payload["lineNumber"],
                    "companyNumber": row_payload["companyNumber"],
                    "displayName": row_payload["displayName"],
                    "clientType": row_payload["clientType"],
                    "clientManager": row_payload["clientManager"],
                    "clientId": row_payload["clientId"],
                    "contactEmail": row_payload["contactEmail"],
                    "contactPhone": row_payload["contactPhone"],
                    "clientAddress": row_payload["clientAddress"],
                }
            )

    delete_rows: list[dict] = []
    delete_ids: list[str] = []
    incoming_keys = set(incoming_by_key.keys())
    for key, existing in existing_by_key.items():
        if key in incoming_keys:
            continue
        row_id = str(existing.get("id") or "")
        if row_id:
            delete_ids.append(row_id)
        delete_rows.append(
            {
                "id": row_id,
                "companyNumber": existing.get("company_number") or "",
                "displayName": existing.get("display_name") or "",
                "clientType": existing.get("client_type") or "",
                "clientManager": existing.get("client_manager") or "",
                "clientId": existing.get("client_id") or "",
                "sourceFilename": existing.get("source_filename") or "",
                "uploadedAt": existing.get("uploaded_at").isoformat() if existing.get("uploaded_at") else None,
            }
        )

    return {
        "filename": _coerce_text(filename, 250),
        "rowCount": len(rows_to_upsert),
        "createCount": len(create_rows),
        "updateCount": len(update_rows),
        "deleteCount": len(delete_rows),
        "errorCount": len(parse_errors),
        "errors": parse_errors[:200],
        "creates": create_rows[:1000],
        "updates": update_rows[:1000],
        "deletes": delete_rows[:1000],
        "rowsToUpsert": rows_to_upsert[:5000],
        "deleteIds": delete_ids[:5000],
    }


def commit_auth_code_register_import(user: dict, preview: dict, *, apply_deletes: bool = True) -> dict:
    rows_to_upsert = preview.get("rowsToUpsert") or []
    delete_ids = preview.get("deleteIds") or []
    if not isinstance(rows_to_upsert, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid preview payload: rowsToUpsert must be a list.")
    if not isinstance(delete_ids, list):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid preview payload: deleteIds must be a list.")
    if not rows_to_upsert and not (apply_deletes and delete_ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No directory changes to commit.")

    user_id = user.get("id") if isinstance(user, dict) else None
    filename = _coerce_text(preview.get("filename"), 250) or "auth-code-register.csv"
    created_count = 0
    updated_count = 0
    deleted_count = 0

    with get_connection() as connection:
        with connection.cursor() as cursor:
            for row in rows_to_upsert:
                _, action = _upsert_auth_code_register_row(
                    cursor,
                    company_number=normalise_company_number(row.get("companyNumber")),
                    display_name=_coerce_text(row.get("displayName"), 250),
                    client_type=_normalise_client_type(row.get("clientType")),
                    client_manager=_coerce_text(row.get("clientManager"), 120),
                    client_id=_coerce_text(row.get("clientId"), 80),
                    normalised_name=_coerce_text(row.get("normalisedName"), 250),
                    auth_code=_coerce_text(row.get("authCode"), 80),
                    filename=filename,
                    user_id=user_id,
                )
                _sync_auth_register_contacts_to_company(
                    cursor,
                    company_number=normalise_company_number(row.get("companyNumber")),
                    display_name=_coerce_text(row.get("displayName"), 250),
                    client_id=_coerce_text(row.get("clientId"), 80),
                    client_manager=_coerce_text(row.get("clientManager"), 120),
                    contact_email=_coerce_text(row.get("contactEmail"), 250),
                    contact_phone=_coerce_text(row.get("contactPhone"), 120),
                    client_address=_coerce_text(row.get("clientAddress"), 1000),
                    user_id=user_id,
                )
                if action == "created":
                    created_count += 1
                else:
                    updated_count += 1
            if apply_deletes:
                for delete_id in [str(value or "").strip() for value in delete_ids]:
                    if not delete_id:
                        continue
                    cursor.execute("DELETE FROM ch_auth_code_register WHERE id = %s", (delete_id,))
                    if (cursor.rowcount or 0) > 0:
                        deleted_count += 1
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_auth_code_register', 'bulk', 'auth_code_register_committed', %s::jsonb, %s)
                """,
                (
                    json.dumps(
                        {
                            "filename": filename,
                            "upsertRows": len(rows_to_upsert),
                            "applyDeletes": bool(apply_deletes),
                            "deleteCandidates": len(delete_ids),
                            "created": created_count,
                            "updated": updated_count,
                            "deleted": deleted_count,
                        }
                    ),
                    user_id,
                ),
            )
        connection.commit()

    return {
        "filename": filename,
        "createdCount": created_count,
        "updatedCount": updated_count,
        "deletedCount": deleted_count,
        "upsertCount": len(rows_to_upsert),
        "deleteRequested": len(delete_ids) if apply_deletes else 0,
        "applyDeletes": bool(apply_deletes),
    }


def list_auth_code_register(limit: int = 300) -> dict:
    safe_limit = max(20, min(int(limit or 300), 1000))
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.id,
                       r.company_number,
                       COALESCE(NULLIF(r.company_name, ''), r.client_name, '') AS display_name,
                       r.client_type,
                       r.client_manager,
                       r.client_id,
                       c.contact_email,
                       c.contact_phone,
                       c.client_address,
                       r.code_hint,
                       r.source_filename,
                       r.uploaded_at
                FROM ch_auth_code_register r
                LEFT JOIN ch_companies c
                  ON c.company_number = r.company_number
                ORDER BY r.uploaded_at DESC
                LIMIT %s
                """,
                (safe_limit,),
            )
            rows = cursor.fetchall() or []
            cursor.execute("SELECT COUNT(*) AS total FROM ch_auth_code_register")
            total_row = cursor.fetchone() or {}
        connection.commit()
    return {
        "totalCount": int(total_row.get("total") or 0),
        "rows": [
            {
                "id": str(row.get("id") or ""),
                "companyNumber": row.get("company_number") or "",
                "displayName": row.get("display_name") or "",
                "clientType": row.get("client_type") or "",
                "clientManager": row.get("client_manager") or "",
                "clientId": row.get("client_id") or "",
                "clientEmail": row.get("contact_email") or "",
                "clientPhone": row.get("contact_phone") or "",
                "clientAddress": row.get("client_address") or "",
                "authCodeHint": row.get("code_hint") or "",
                "sourceFilename": row.get("source_filename") or "",
                "uploadedAt": row.get("uploaded_at").isoformat() if row.get("uploaded_at") else None,
            }
            for row in rows
        ],
    }


COMPANY_SECRETARIAL_ALLOWED_MODES = {"api", "assisted", "manual"}
COMPANY_SECRETARIAL_ALLOWED_STATUSES = {
    "DRAFT",
    "VALIDATION_FAILED",
    "AWAITING_CLIENT_APPROVAL",
    "AWAITING_INTERNAL_REVIEW",
    "READY_TO_SUBMIT",
    "SUBMITTED",
    "COMPLETED",
    "REJECTED",
}
COMPANY_SECRETARIAL_APPROVAL_STATUSES = {"not_required", "requested", "pending", "approved"}
COMPANY_SECRETARIAL_TYPE_DEFAULTS = {
    "AD01": {"name": "Registered office change", "risk": "medium", "mode": "api", "fee": Decimal("0.00"), "clientApprovalRequired": False, "internalApprovalRequired": True},
    "NM01": {"name": "Company name change", "risk": "high", "mode": "api", "fee": Decimal("10.00"), "clientApprovalRequired": True, "internalApprovalRequired": True},
    "DS01": {"name": "Strike-off application", "risk": "high", "mode": "assisted", "fee": Decimal("8.00"), "clientApprovalRequired": True, "internalApprovalRequired": True},
    "INCORPORATION": {"name": "New company incorporation", "risk": "high", "mode": "assisted", "fee": Decimal("50.00"), "clientApprovalRequired": True, "internalApprovalRequired": True},
    "AP01": {"name": "Director appointment", "risk": "medium", "mode": "api", "fee": Decimal("0.00"), "clientApprovalRequired": False, "internalApprovalRequired": True},
    "TM01": {"name": "Director resignation", "risk": "medium", "mode": "api", "fee": Decimal("0.00"), "clientApprovalRequired": False, "internalApprovalRequired": True},
    "PSC_CHANGE": {"name": "PSC update", "risk": "high", "mode": "assisted", "fee": Decimal("0.00"), "clientApprovalRequired": True, "internalApprovalRequired": True},
    "SH01": {"name": "Share allotment", "risk": "high", "mode": "assisted", "fee": Decimal("0.00"), "clientApprovalRequired": True, "internalApprovalRequired": True},
    "CH01": {"name": "Director detail change", "risk": "medium", "mode": "api", "fee": Decimal("0.00"), "clientApprovalRequired": False, "internalApprovalRequired": True},
}
# Live XML Gateway schemas currently available for this workflow.
SECRETARIAL_XML_SUPPORTED_TYPES = {"AD01", "NM01"}
SECRETARIAL_XML_FORM_CONFIG = {
    "AD01": {
        "class": "ChangeRegisteredOfficeAddress",
        "formIdentifier": "ChangeRegisteredOfficeAddress",
        "schemaLocation": "http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/ChangeRegisteredOfficeAddress-v2-7.xsd",
    },
    "NM01": {
        "class": "ChangeOfName",
        "formIdentifier": "ChangeOfName",
        "schemaLocation": "http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/ChangeOfName-v2-6.xsd",
    },
}
UK_POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.IGNORECASE)


def _secretarial_hash_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _secretarial_form_data(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _secretarial_list_of_dicts(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _secretarial_form_config(filing_type: str) -> dict:
    return SECRETARIAL_XML_FORM_CONFIG.get(_xml_text(filing_type).upper(), {})


def _secretarial_country_code(raw_country: str, *, company_number: str = "") -> str:
    country = _xml_text(raw_country).upper().replace(" ", "").replace("_", "-")
    direct_map = {
        "GB": "GBR",
        "UK": "GBR",
        "GBR": "GBR",
        "UNITEDKINGDOM": "GBR",
        "ENGLAND": "GB-ENG",
        "WALES": "GB-WLS",
        "SCOTLAND": "GB-SCT",
        "NORTHERNIRELAND": "GB-NIR",
        "GB-ENG": "GB-ENG",
        "GB-WLS": "GB-WLS",
        "GB-SCT": "GB-SCT",
        "GB-NIR": "GB-NIR",
    }
    if country in direct_map:
        return direct_map[country]
    _, number_digits = _ch_split_company_number(company_number)
    if number_digits:
        prefix = _xml_text(company_number).upper().replace(number_digits, "", 1)
        if prefix in {"SC"}:
            return "GB-SCT"
        if prefix in {"NI"}:
            return "GB-NIR"
        if prefix in {"", "OC", "LP", "SO", "SL", "NC", "NL", "R0", "R1", "R2", "R3"}:
            return "GB-ENG"
    return "GBR"


def _validate_secretarial_form_payload(
    *,
    filing_type: str,
    form_data: dict,
    effective_date: date | None,
    company_number: str = "",
) -> list[str]:
    issues: list[str] = []
    if effective_date is None:
        issues.append("Effective date is required.")
    today = date.today()
    if isinstance(effective_date, date) and effective_date > today:
        issues.append("Effective date cannot be in the future.")

    if filing_type == "AD01":
        address = form_data.get("newRegisteredOffice") if isinstance(form_data.get("newRegisteredOffice"), dict) else {}
        line1 = _coerce_text(address.get("line1"), 150)
        line2 = _coerce_text(address.get("line2"), 150)
        city = _coerce_text(address.get("city"), 120)
        postcode = _coerce_text(address.get("postcode"), 20).upper()
        country = _coerce_text(address.get("country"), 100)
        county = _coerce_text(address.get("county"), 120)
        if not line1:
            issues.append("AD01 requires new registered office address line 1.")
        if not city:
            issues.append("AD01 requires post town/city.")
        if not postcode:
            issues.append("AD01 requires postcode for UK filings.")
        if postcode and not UK_POSTCODE_RE.fullmatch(postcode):
            issues.append("AD01 postcode is not in a valid UK format.")
        if not country:
            issues.append("AD01 requires country.")
        country_code = _secretarial_country_code(country, company_number=company_number)
        if country_code not in {"GB-ENG", "GB-WLS", "GB-SCT", "GB-NIR", "GBR", "UNDEF"}:
            issues.append("AD01 country must be a UK country code (GB-ENG/GB-WLS/GB-SCT/GB-NIR/GBR).")
        if "PO BOX" in f"{line1} {line2} {county}".upper() and not postcode:
            issues.append("AD01 PO Box addresses must include a postcode.")
        return issues

    if filing_type == "NM01":
        proposed_name = _coerce_text(form_data.get("proposedCompanyName"), 200)
        resolution_date = _secretarial_parse_date(form_data.get("resolutionDate"), "resolutionDate")
        resolution_method = _coerce_text(form_data.get("resolutionMethod"), 30).lower()
        authorising_name = _coerce_text(form_data.get("authorisingPersonName"), 120)
        authorising_status = _coerce_text(form_data.get("authorisingPersonStatus"), 120)
        if not proposed_name:
            issues.append("NM01 requires proposed company name.")
        elif not re.search(r"\b(LIMITED|LTD|PLC|LLP)\b$", proposed_name.upper()):
            issues.append("NM01 proposed company name must include a valid legal suffix (Limited/Ltd/PLC/LLP).")
        if resolution_date is None:
            issues.append("NM01 requires resolution date.")
        elif resolution_date > today:
            issues.append("NM01 resolution date cannot be in the future.")
        if resolution_method and resolution_method not in {"written", "meeting"}:
            issues.append("NM01 resolution method must be 'written' or 'meeting'.")
        if not authorising_name:
            issues.append("NM01 requires authorising person name.")
        if not authorising_status:
            issues.append("NM01 requires authorising person status.")
        return issues

    if filing_type == "DS01":
        application_date = _secretarial_parse_date(form_data.get("applicationDate"), "applicationDate")
        directors = _secretarial_list_of_dicts(form_data.get("applicantDirectors"))
        statements = form_data.get("statements") if isinstance(form_data.get("statements"), dict) else {}
        if application_date is None:
            issues.append("DS01 requires application date.")
        elif application_date > today:
            issues.append("DS01 application date cannot be in the future.")
        if not directors:
            issues.append("DS01 requires at least one applicant director.")
        for index, director in enumerate(directors, start=1):
            if not _coerce_text(director.get("forename"), 100):
                issues.append(f"DS01 director {index} requires forename.")
            if not _coerce_text(director.get("surname"), 100):
                issues.append(f"DS01 director {index} requires surname.")
        required_true_statements = {
            "eligible": "DS01 eligibility confirmation is required.",
            "statementTradingCeased": "DS01 must confirm trading has ceased.",
            "statementNoInsolvencyProceedings": "DS01 must confirm no insolvency proceedings.",
            "statementInterestedPartiesWillBeNotified": "DS01 must confirm interested parties will be notified.",
            "statementNoImproperNameChange": "DS01 must confirm no improper recent name change.",
        }
        for key, message in required_true_statements.items():
            if _first_bool_from_sources(statements.get(key)) is not True:
                issues.append(message)
        return issues

    return issues


def _build_secretarial_form_xml_node(
    *,
    filing_type: str,
    form_data: dict,
    namespace: str,
    company_number: str = "",
) -> ET.Element:
    config = _secretarial_form_config(filing_type)
    node_name = config.get("formIdentifier") or filing_type
    form_node = ET.Element(f"{{{namespace}}}{node_name}")
    if filing_type == "AD01":
        address = form_data.get("newRegisteredOffice") if isinstance(form_data.get("newRegisteredOffice"), dict) else {}
        line1 = _coerce_text(address.get("line1"), 150)
        line2 = _coerce_text(address.get("line2"), 150)
        city = _coerce_text(address.get("city"), 120)
        county = _coerce_text(address.get("county"), 120)
        postcode = _coerce_text(address.get("postcode"), 20).upper()
        country = _secretarial_country_code(_coerce_text(address.get("country"), 100), company_number=company_number)
        premise = line1.split(" ", 1)[0] if line1 else ""
        street = line1.split(" ", 1)[1] if " " in line1 else line1
        new_address = ET.SubElement(form_node, f"{{{namespace}}}Address")
        if premise:
            ET.SubElement(new_address, f"{{{namespace}}}Premise").text = premise
        if street:
            ET.SubElement(new_address, f"{{{namespace}}}Street").text = street
        if line2:
            ET.SubElement(new_address, f"{{{namespace}}}Thoroughfare").text = line2
        if city:
            ET.SubElement(new_address, f"{{{namespace}}}PostTown").text = city
        if county:
            ET.SubElement(new_address, f"{{{namespace}}}County").text = county
        if postcode:
            ET.SubElement(new_address, f"{{{namespace}}}Postcode").text = postcode
        if country:
            ET.SubElement(new_address, f"{{{namespace}}}Country").text = country
        ET.SubElement(form_node, f"{{{namespace}}}AcceptAppropriateOfficeAddressStatement").text = "true"
        return form_node

    if filing_type == "NM01":
        proposed_name = _coerce_text(form_data.get("proposedCompanyName"), 200)
        resolution_date = _secretarial_parse_date(form_data.get("resolutionDate"), "resolutionDate")
        resolution_method = _coerce_text(form_data.get("resolutionMethod"), 30).lower()
        method_map = {"written": "RESOLUTION", "meeting": "RESOLUTION"}
        method_value = method_map.get(resolution_method, "RESOLUTION")
        same_day = _first_bool_from_sources(form_data.get("sameDay"))
        ET.SubElement(form_node, f"{{{namespace}}}MethodOfChange").text = method_value
        ET.SubElement(form_node, f"{{{namespace}}}ProposedCompanyName").text = proposed_name
        if resolution_date:
            ET.SubElement(form_node, f"{{{namespace}}}MeetingDate").text = resolution_date.isoformat()
        ET.SubElement(form_node, f"{{{namespace}}}SameDay").text = "true" if same_day else "false"
        ET.SubElement(form_node, f"{{{namespace}}}NoticeGiven").text = "true"
        return form_node

    if filing_type == "DS01":
        application_date = _secretarial_parse_date(form_data.get("applicationDate"), "applicationDate")
        directors = _secretarial_list_of_dicts(form_data.get("applicantDirectors"))
        statements = form_data.get("statements") if isinstance(form_data.get("statements"), dict) else {}
        if application_date:
            ET.SubElement(form_node, f"{{{namespace}}}ApplicationDate").text = application_date.isoformat()
        directors_node = ET.SubElement(form_node, f"{{{namespace}}}ApplicantDirectors")
        for director in directors:
            director_node = ET.SubElement(directors_node, f"{{{namespace}}}Director")
            ET.SubElement(director_node, f"{{{namespace}}}Forename").text = _coerce_text(director.get("forename"), 100)
            ET.SubElement(director_node, f"{{{namespace}}}Surname").text = _coerce_text(director.get("surname"), 100)
        statements_node = ET.SubElement(form_node, f"{{{namespace}}}Statements")
        for key, node_name in (
            ("statementTradingCeased", "StatementTradingCeased"),
            ("statementNoInsolvencyProceedings", "StatementNoInsolvencyProceedings"),
            ("statementInterestedPartiesWillBeNotified", "StatementInterestedPartiesWillBeNotified"),
            ("statementNoImproperNameChange", "StatementNoImproperNameChange"),
            ("eligible", "Eligible"),
        ):
            bool_value = _first_bool_from_sources(statements.get(key))
            if bool_value is not None:
                ET.SubElement(statements_node, f"{{{namespace}}}{node_name}").text = "true" if bool_value else "false"
        return form_node

    return form_node


def _build_secretarial_submission_xml(
    *,
    presenter_id: str,
    presenter_auth: str,
    environment: str,
    company_number: str,
    company_name: str,
    company_auth_code: str,
    filing_type: str,
    submission_number: str,
    transaction_id: str,
    package_reference: str,
    form_data: dict,
    effective_date: date,
) -> bytes:
    config = _secretarial_form_config(filing_type)
    message_class = _xml_text(config.get("class"), filing_type)
    form_identifier = _xml_text(config.get("formIdentifier"), filing_type)
    form_schema_location = _xml_text(config.get("schemaLocation"))
    if not form_schema_location:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{filing_type} is not configured for XML software filing.",
        )
    gov = ET.Element(
        "GovTalkMessage",
        {
            "xmlns": GOVTALK_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{GOVTALK_NS} http://xmlgw.companieshouse.gov.uk/v2-1/schema/Egov_ch-v2-0.xsd",
        },
    )
    ET.SubElement(gov, "EnvelopeVersion").text = "1.0"
    header = ET.SubElement(gov, "Header")
    message_details = ET.SubElement(header, "MessageDetails")
    ET.SubElement(message_details, "Class").text = message_class
    ET.SubElement(message_details, "Qualifier").text = "request"
    ET.SubElement(message_details, "TransactionID").text = transaction_id
    ET.SubElement(message_details, "GatewayTest").text = _ch_gateway_test_flag(environment)
    sender_details = ET.SubElement(header, "SenderDetails")
    id_auth = ET.SubElement(sender_details, "IDAuthentication")
    ET.SubElement(id_auth, "SenderID").text = presenter_id
    auth = ET.SubElement(id_auth, "Authentication")
    _ch_auth_method_value = _ch_auth_method()
    ET.SubElement(auth, "Method").text = _ch_auth_method_value
    ET.SubElement(auth, "Value").text = _ch_auth_value(_ch_auth_method_value, presenter_auth)
    govtalk_details = ET.SubElement(gov, "GovTalkDetails")
    keys = ET.SubElement(govtalk_details, "Keys")
    ET.SubElement(keys, "Key", {"Type": "CompanyNumber"}).text = company_number

    body = ET.SubElement(gov, "Body")
    form_submission = ET.SubElement(
        body,
        f"{{{CH_HEADER_NS}}}FormSubmission",
        {
            "xmlns": CH_HEADER_NS,
            "xmlns:bs": CH_FORMS_NS,
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:schemaLocation": f"{CH_HEADER_NS} http://xmlgw.companieshouse.gov.uk/v1-0/schema/forms/FormSubmission-v2-11.xsd",
        },
    )
    form_header = ET.SubElement(form_submission, f"{{{CH_HEADER_NS}}}FormHeader")
    company_type, company_number_digits = _ch_split_company_number(company_number)
    if not company_number_digits:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported company number format for XML gateway: {company_number}.",
        )
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}CompanyNumber").text = company_number_digits
    if company_type != "EW":
        ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}CompanyType").text = company_type
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}CompanyName").text = _xml_text(company_name, "UNKNOWN COMPANY")
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}CompanyAuthenticationCode").text = company_auth_code
    package_reference_value = _xml_text(package_reference)
    if not package_reference_value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Companies House PackageReference is not configured. "
                "Set COMPANIES_HOUSE_PACKAGE_REFERENCE to the package reference "
                "issued by Companies House for your software filing account before submitting."
            ),
        )
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}PackageReference").text = package_reference_value
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}Language").text = "EN"
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}FormIdentifier").text = form_identifier
    ET.SubElement(form_header, f"{{{CH_HEADER_NS}}}SubmissionNumber").text = submission_number
    ET.SubElement(form_submission, f"{{{CH_HEADER_NS}}}DateSigned").text = effective_date.isoformat()
    form = ET.SubElement(form_submission, f"{{{CH_HEADER_NS}}}Form")
    form_node = _build_secretarial_form_xml_node(
        filing_type=filing_type,
        form_data=form_data,
        namespace=CH_FORMS_NS,
        company_number=company_number,
    )
    form_node.set("xmlns", CH_FORMS_NS)
    form_node.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    form_node.set("xsi:schemaLocation", f"{CH_FORMS_NS} {form_schema_location}")
    form.append(form_node)
    return ET.tostring(gov, encoding="utf-8", xml_declaration=True)


def _secretarial_parse_date(value: object, field: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date() if "T" in text else date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field}. Expected YYYY-MM-DD.") from exc


def _secretarial_parse_datetime(value: object, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field}. Expected ISO-8601 datetime.") from exc


def _secretarial_decimal(value: object, field: str, default: Decimal = Decimal("0.00")) -> Decimal:
    if value in (None, ""):
        return default
    decimal_value = _coerce_decimal(value, field)
    try:
        return decimal_value.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid {field}.") from exc


def _secretarial_validation_messages(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        text = _coerce_text(item, 500)
        if text:
            cleaned.append(text)
    return cleaned


def _derive_secretarial_status(
    *,
    current_status: str | None = None,
    has_validation_issues: bool = False,
    client_required: bool = False,
    client_status: str = "not_required",
    internal_required: bool = False,
    internal_status: str = "not_required",
) -> str:
    status_value = str(current_status or "").strip().upper()
    if status_value in {"SUBMITTED", "COMPLETED", "REJECTED"}:
        return status_value
    if has_validation_issues:
        return "VALIDATION_FAILED"
    if client_required and client_status != "approved":
        return "AWAITING_CLIENT_APPROVAL"
    if internal_required and internal_status != "approved":
        return "AWAITING_INTERNAL_REVIEW"
    return "READY_TO_SUBMIT"


def _serialise_secretarial_filing(row: dict | None) -> dict:
    row = row or {}
    fee = row.get("fee_amount")
    try:
        fee_value = float(fee) if fee is not None else 0.0
    except Exception:
        fee_value = 0.0
    return {
        "id": str(row.get("id") or ""),
        "filingType": row.get("filing_type") or "",
        "filingName": row.get("filing_name") or "",
        "companyId": str(row.get("company_id") or "") if row.get("company_id") else "",
        "companyName": row.get("company_name") or "",
        "companyNumber": row.get("company_number") or "",
        "clientId": row.get("client_id") or "",
        "status": row.get("status") or "DRAFT",
        "risk": row.get("risk") or "medium",
        "mode": row.get("mode") or "manual",
        "dueDate": _date_or_none(row.get("due_date")),
        "effectiveDate": _date_or_none(row.get("effective_date")),
        "clientApprovalRequired": bool(row.get("client_approval_required")),
        "clientApprovalStatus": row.get("client_approval_status") or "not_required",
        "internalApprovalRequired": bool(row.get("internal_approval_required")),
        "internalApprovalStatus": row.get("internal_approval_status") or "not_required",
        "evidenceAttached": bool(row.get("evidence_attached")),
        "submittedAt": row.get("submitted_at").isoformat() if row.get("submitted_at") else "",
        "companiesHouseStatus": row.get("companies_house_status") or "Not submitted",
        "companiesHouseRef": row.get("companies_house_ref") or "",
        "feeAmount": fee_value,
        "assignee": row.get("assignee") or "",
        "notes": row.get("notes") or "",
        "clientEmail": row.get("client_email") or "",
        "clientPhone": row.get("client_phone") or "",
        "clientAddress": row.get("client_address") or "",
        "authCodeHint": row.get("auth_code_hint") or "",
        "sourceFilename": row.get("source_filename") or "",
        "uploadedAt": row.get("uploaded_at").isoformat() if row.get("uploaded_at") else "",
        "formData": row.get("form_data") if isinstance(row.get("form_data"), dict) else {},
        "preparedSubmission": row.get("prepared_submission") if isinstance(row.get("prepared_submission"), dict) else {},
        "validationMessages": row.get("validation_messages") if isinstance(row.get("validation_messages"), list) else [],
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else "",
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else "",
    }


def _load_secretarial_filing(cursor, filing_id: str) -> dict | None:
    cursor.execute(
        """
        SELECT *
        FROM ch_secretarial_filings
        WHERE id = %s
        """,
        (filing_id,),
    )
    return cursor.fetchone() or None


def _find_secretarial_company(cursor, payload: dict) -> dict | None:
    company_id = str(payload.get("companyId") or "").strip()
    company_number = normalise_company_number(payload.get("companyNumber"))
    if company_id:
        try:
            UUID(company_id)
        except (TypeError, ValueError):
            company_id = ""
    if company_id:
        cursor.execute(
            """
            SELECT id, company_number, company_name, client_id, contact_email, contact_phone, client_address, assigned_staff_name
            FROM ch_companies
            WHERE id = %s
            """,
            (company_id,),
        )
        company = cursor.fetchone() or None
        if company:
            return company
    if company_number:
        cursor.execute(
            """
            SELECT id, company_number, company_name, client_id, contact_email, contact_phone, client_address, assigned_staff_name
            FROM ch_companies
            WHERE company_number = %s
            """,
            (company_number,),
        )
        return cursor.fetchone() or None
    return None


def _find_secretarial_register_row(cursor, company_number: str) -> dict | None:
    if not company_number:
        return None
    cursor.execute(
        """
        SELECT company_number, client_manager, client_id, code_hint, source_filename, uploaded_at
        FROM ch_auth_code_register
        WHERE company_number = %s
        ORDER BY uploaded_at DESC
        LIMIT 1
        """,
        (company_number,),
    )
    return cursor.fetchone() or None


def list_company_secretarial_filings(limit: int = 500) -> dict:
    safe_limit = max(20, min(int(limit or 500), 2000))
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM ch_secretarial_filings
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (safe_limit,),
            )
            rows = cursor.fetchall() or []
            cursor.execute("SELECT COUNT(*) AS total FROM ch_secretarial_filings")
            total_row = cursor.fetchone() or {}
        connection.commit()
    return {
        "totalCount": int(total_row.get("total") or 0),
        "filings": [_serialise_secretarial_filing(row) for row in rows],
    }


def create_company_secretarial_filing(user: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    filing_type = _coerce_text(payload.get("filingType"), 40).upper()
    if not filing_type:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="filingType is required.")
    defaults = COMPANY_SECRETARIAL_TYPE_DEFAULTS.get(filing_type, {})
    filing_name = _coerce_text(payload.get("filingName"), 200) or defaults.get("name") or "Company secretarial filing"
    mode = _coerce_text(payload.get("mode"), 20).lower() or str(defaults.get("mode") or "manual")
    if mode not in COMPANY_SECRETARIAL_ALLOWED_MODES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid mode.")
    risk = _coerce_text(payload.get("risk"), 20).lower() or str(defaults.get("risk") or "medium")
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    client_required = bool(payload.get("clientApprovalRequired", defaults.get("clientApprovalRequired", False)))
    internal_required = bool(payload.get("internalApprovalRequired", defaults.get("internalApprovalRequired", True)))
    client_status = _coerce_text(payload.get("clientApprovalStatus"), 30).lower() or ("requested" if client_required else "not_required")
    if client_status not in COMPANY_SECRETARIAL_APPROVAL_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid clientApprovalStatus.")
    internal_status = _coerce_text(payload.get("internalApprovalStatus"), 30).lower() or ("pending" if internal_required else "not_required")
    if internal_status not in COMPANY_SECRETARIAL_APPROVAL_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid internalApprovalStatus.")
    fee_amount = _secretarial_decimal(payload.get("feeAmount"), "feeAmount", defaults.get("fee") or Decimal("0.00"))
    due_date = _secretarial_parse_date(payload.get("dueDate"), "dueDate")
    effective_date = _secretarial_parse_date(payload.get("effectiveDate"), "effectiveDate")
    validation_messages = _secretarial_validation_messages(payload.get("validationIssues") or payload.get("validationMessages") or [])
    form_data = _secretarial_form_data(payload.get("formData"))
    company_number = normalise_company_number(payload.get("companyNumber"))
    validation_messages.extend(
        _validate_secretarial_form_payload(
            filing_type=filing_type,
            form_data=form_data,
            effective_date=effective_date,
            company_number=company_number,
        )
    )
    if validation_messages:
        # Keep insertion deterministic if duplicate checks are triggered by UI + backend.
        validation_messages = list(dict.fromkeys(validation_messages))
    status_value = _coerce_text(payload.get("status"), 40).upper()
    if status_value and status_value not in COMPANY_SECRETARIAL_ALLOWED_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid status.")
    derived_status = _derive_secretarial_status(
        current_status=status_value or None,
        has_validation_issues=bool(validation_messages),
        client_required=client_required,
        client_status=client_status,
        internal_required=internal_required,
        internal_status=internal_status,
    )
    companies_house_status = _coerce_text(payload.get("companiesHouseStatus"), 200) or (
        "Validation failed" if derived_status == "VALIDATION_FAILED" else "Draft prepared for Companies House"
    )
    user_id = user.get("id") if isinstance(user, dict) else None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            company = _find_secretarial_company(cursor, payload) or {}
            company_id = company.get("id")
            company_number = normalise_company_number(payload.get("companyNumber") or company.get("company_number"))
            company_name = _coerce_text(payload.get("companyName"), 200) or _coerce_text(company.get("company_name"), 200)
            client_id = _coerce_text(payload.get("clientId"), 100) or _coerce_text(company.get("client_id"), 100)
            if not company_number:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Company number is required.")
            register_row = _find_secretarial_register_row(cursor, company_number) or {}
            uploaded_at = _secretarial_parse_datetime(payload.get("uploadedAt"), "uploadedAt") or register_row.get("uploaded_at")
            cursor.execute(
                """
                INSERT INTO ch_secretarial_filings (
                    company_id, company_number, company_name, client_id,
                    filing_type, filing_name, status, risk, mode, due_date, effective_date,
                    client_approval_required, client_approval_status,
                    internal_approval_required, internal_approval_status,
                    evidence_attached, submitted_at, companies_house_status, companies_house_ref,
                    fee_amount, assignee, notes, client_email, client_phone, client_address,
                    auth_code_hint, source_filename, uploaded_at, form_data, prepared_submission,
                    validation_messages, created_by_user_id, updated_by_user_id
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s
                )
                RETURNING id
                """,
                (
                    company_id,
                    company_number,
                    company_name,
                    client_id,
                    filing_type,
                    filing_name,
                    derived_status,
                    risk,
                    mode,
                    due_date,
                    effective_date,
                    client_required,
                    client_status,
                    internal_required,
                    internal_status,
                    bool(payload.get("evidenceAttached")),
                    _secretarial_parse_datetime(payload.get("submittedAt"), "submittedAt"),
                    companies_house_status,
                    _coerce_text(payload.get("companiesHouseRef"), 120),
                    fee_amount,
                    _coerce_text(payload.get("assignee"), 120) or _coerce_text(register_row.get("client_manager"), 120) or _coerce_text(company.get("assigned_staff_name"), 120),
                    _coerce_text(payload.get("notes"), 4000),
                    _coerce_text(payload.get("clientEmail"), 320) or _coerce_text(company.get("contact_email"), 320),
                    _coerce_text(payload.get("clientPhone"), 120) or _coerce_text(company.get("contact_phone"), 120),
                    _coerce_text(payload.get("clientAddress"), 2000) or _coerce_text(company.get("client_address"), 2000),
                    _coerce_text(payload.get("authCodeHint"), 120) or _coerce_text(register_row.get("code_hint"), 120),
                    _coerce_text(payload.get("sourceFilename"), 255) or _coerce_text(register_row.get("source_filename"), 255),
                    uploaded_at,
                    json.dumps(form_data),
                    json.dumps(payload.get("preparedSubmission") if isinstance(payload.get("preparedSubmission"), dict) else {}),
                    json.dumps(validation_messages),
                    user_id,
                    user_id,
                ),
            )
            result = cursor.fetchone() or {}
            filing_id = str(result.get("id") or "")
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_secretarial_filing', %s, 'created', %s::jsonb, %s)
                """,
                (filing_id, json.dumps({"filingType": filing_type, "status": derived_status}), user_id),
            )
            filing = _load_secretarial_filing(cursor, filing_id)
        connection.commit()
    return _serialise_secretarial_filing(filing)


def patch_company_secretarial_filing(user: dict, filing_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    user_id = user.get("id") if isinstance(user, dict) else None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            row = _load_secretarial_filing(cursor, filing_id)
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing not found.")
            client_status = _coerce_text(payload.get("clientApprovalStatus"), 30).lower() or row.get("client_approval_status") or "not_required"
            internal_status = _coerce_text(payload.get("internalApprovalStatus"), 30).lower() or row.get("internal_approval_status") or "not_required"
            if client_status not in COMPANY_SECRETARIAL_APPROVAL_STATUSES:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid clientApprovalStatus.")
            if internal_status not in COMPANY_SECRETARIAL_APPROVAL_STATUSES:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid internalApprovalStatus.")
            client_required = bool(payload.get("clientApprovalRequired")) if "clientApprovalRequired" in payload else bool(row.get("client_approval_required"))
            internal_required = bool(payload.get("internalApprovalRequired")) if "internalApprovalRequired" in payload else bool(row.get("internal_approval_required"))
            current_validation = row.get("validation_messages") if isinstance(row.get("validation_messages"), list) else []
            next_status = _derive_secretarial_status(
                current_status=row.get("status"),
                has_validation_issues=bool(current_validation),
                client_required=client_required,
                client_status=client_status,
                internal_required=internal_required,
                internal_status=internal_status,
            )
            cursor.execute(
                """
                UPDATE ch_secretarial_filings
                SET client_approval_required = %s,
                    client_approval_status = %s,
                    internal_approval_required = %s,
                    internal_approval_status = %s,
                    evidence_attached = %s,
                    notes = %s,
                    assignee = %s,
                    fee_amount = %s,
                    status = %s,
                    companies_house_status = %s,
                    updated_by_user_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    client_required,
                    client_status,
                    internal_required,
                    internal_status,
                    bool(payload.get("evidenceAttached")) if "evidenceAttached" in payload else bool(row.get("evidence_attached")),
                    _coerce_text(payload.get("notes"), 4000) if "notes" in payload else row.get("notes"),
                    _coerce_text(payload.get("assignee"), 120) if "assignee" in payload else row.get("assignee"),
                    _secretarial_decimal(payload.get("feeAmount"), "feeAmount", _secretarial_decimal(row.get("fee_amount"), "feeAmount")) if "feeAmount" in payload else row.get("fee_amount"),
                    next_status,
                    "Client approved" if client_status == "approved" else ("Internal review approved" if internal_status == "approved" else row.get("companies_house_status")),
                    user_id,
                    filing_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_secretarial_filing', %s, 'updated', %s::jsonb, %s)
                """,
                (filing_id, json.dumps({"status": next_status}), user_id),
            )
            updated = _load_secretarial_filing(cursor, filing_id)
        connection.commit()
    return _serialise_secretarial_filing(updated)


def validate_company_secretarial_filing(user: dict, filing_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    validation_messages = _secretarial_validation_messages(payload.get("validationIssues") or payload.get("validationMessages") or [])
    user_id = user.get("id") if isinstance(user, dict) else None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            row = _load_secretarial_filing(cursor, filing_id)
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing not found.")
            filing_type = _coerce_text(row.get("filing_type"), 40).upper()
            form_data = _secretarial_form_data(row.get("form_data"))
            effective_date = row.get("effective_date") if isinstance(row.get("effective_date"), date) else None
            validation_messages.extend(
                _validate_secretarial_form_payload(
                    filing_type=filing_type,
                    form_data=form_data,
                    effective_date=effective_date,
                    company_number=_coerce_text(row.get("company_number"), 20),
                )
            )
            if validation_messages:
                validation_messages = list(dict.fromkeys(validation_messages))
            status_value = _derive_secretarial_status(
                current_status=row.get("status"),
                has_validation_issues=bool(validation_messages),
                client_required=bool(row.get("client_approval_required")),
                client_status=row.get("client_approval_status") or "not_required",
                internal_required=bool(row.get("internal_approval_required")),
                internal_status=row.get("internal_approval_status") or "not_required",
            )
            if validation_messages:
                ch_status = "Validation failed"
            elif bool(row.get("client_approval_required")) and (row.get("client_approval_status") or "") != "approved":
                ch_status = "Awaiting client approval"
            elif bool(row.get("internal_approval_required")) and (row.get("internal_approval_status") or "") != "approved":
                ch_status = "Awaiting internal review"
            else:
                ch_status = "Validated and ready"
            cursor.execute(
                """
                UPDATE ch_secretarial_filings
                SET validation_messages = %s::jsonb,
                    status = %s,
                    companies_house_status = %s,
                    updated_by_user_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (json.dumps(validation_messages), status_value, ch_status, user_id, filing_id),
            )
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_secretarial_filing', %s, 'validated', %s::jsonb, %s)
                """,
                (filing_id, json.dumps({"status": status_value, "validationMessages": validation_messages}), user_id),
            )
            updated = _load_secretarial_filing(cursor, filing_id)
        connection.commit()
    return _serialise_secretarial_filing(updated)


def submit_company_secretarial_filing(user: dict, filing_id: str, payload: dict | None = None) -> dict:
    payload = payload or {}
    user_id = user.get("id") if isinstance(user, dict) else None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            row = _load_secretarial_filing(cursor, filing_id)
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing not found.")
            status_value = str(row.get("status") or "")
            if status_value not in {"READY_TO_SUBMIT", "SUBMITTED"}:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Filing must be ready before submission.")
            filing_type = _coerce_text(row.get("filing_type"), 40).upper()
            reference = _coerce_text(row.get("companies_house_ref"), 120) or _next_unique_submission_number()
            mode = _coerce_text(row.get("mode"), 20).lower()
            effective_date = row.get("effective_date") if isinstance(row.get("effective_date"), date) else None
            form_data = _secretarial_form_data(row.get("form_data"))
            validation_issues = _validate_secretarial_form_payload(
                filing_type=filing_type,
                form_data=form_data,
                effective_date=effective_date,
                company_number=_coerce_text(row.get("company_number"), 20),
            )
            if validation_issues:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Filing validation failed: " + " | ".join(validation_issues[:5]),
                )

            approval_payload = payload.get("approval") if isinstance(payload.get("approval"), dict) else {}
            request_meta = payload.get("_requestMeta") if isinstance(payload.get("_requestMeta"), dict) else {}
            preview_text = json.dumps(row.get("prepared_submission") if isinstance(row.get("prepared_submission"), dict) else {}, sort_keys=True)
            preview_hash = _secretarial_hash_text(preview_text)
            audit_bundle = {
                "submittedByUserId": user_id,
                "submittedAt": utcnow().isoformat(),
                "clientApprovalStatement": _coerce_text(approval_payload.get("clientApprovalStatement"), 4000),
                "internalApprovalStatement": _coerce_text(approval_payload.get("internalApprovalStatement"), 4000),
                "authCodeConfirmed": bool(approval_payload.get("authCodeConfirmed")),
                "feeConfirmed": bool(approval_payload.get("feeConfirmed")),
                "requestMeta": {
                    "ip": _coerce_text(request_meta.get("ip"), 120),
                    "forwardedFor": _coerce_text(request_meta.get("forwardedFor"), 250),
                    "userAgent": _coerce_text(request_meta.get("userAgent"), 500),
                    "device": _coerce_text(request_meta.get("device"), 250),
                },
                "previewHash": preview_hash,
            }
            if bool(row.get("client_approval_required")) and (row.get("client_approval_status") or "") != "approved":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Client approval is required before submission.")
            if bool(row.get("internal_approval_required")) and (row.get("internal_approval_status") or "") != "approved":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Internal approval is required before submission.")
            if bool(row.get("client_approval_required")) and not audit_bundle["clientApprovalStatement"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Client approval statement is required before submission.")
            if bool(row.get("internal_approval_required")) and not audit_bundle["internalApprovalStatement"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Internal approval statement is required before submission.")
            if not audit_bundle["authCodeConfirmed"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Submission requires explicit authentication-code confirmation.")
            if Decimal(str(row.get("fee_amount") or "0")) > Decimal("0") and not audit_bundle["feeConfirmed"]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Fee confirmation is required before submission.")

            ch_status = "Manual filing pack generated" if mode == "manual" else "Submission sent"
            response_payload: dict = {
                "submissionReference": reference,
                "filingType": filing_type,
                "audit": audit_bundle,
            }
            request_xml = ""
            response_xml = ""
            request_xml_hash = ""
            response_xml_hash = ""
            gateway_status = "submitted"
            rejection_reason = ""

            if filing_type in SECRETARIAL_XML_SUPPORTED_TYPES and mode in {"api", "assisted"}:
                settings_row = _ensure_settings_row()
                environment = _xml_text(settings_row.get("environment"), "sandbox")
                presenter_id = configured_presenter_id(settings_row)
                presenter_auth = decrypt_presenter_auth()
                package_reference = configured_package_reference()
                if not presenter_id or not presenter_auth:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Presenter ID/authentication are required for software filing.",
                    )
                if not package_reference:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=(
                            "Companies House PackageReference is not configured. "
                            "Set COMPANIES_HOUSE_PACKAGE_REFERENCE to the package reference issued by "
                            "Companies House for your software filing account before submitting."
                        ),
                    )
                if not row.get("company_id"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"{filing_type} software filing requires a linked Companies House company record.",
                    )
                company_auth_code = _load_company_auth_code(str(row.get("company_id")))
                if not re.fullmatch(r"[A-Z0-9]{6}", company_auth_code or ""):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Valid 6-character company authentication code is required before submission.",
                    )
                transaction_id = _ch_txn_id()
                request_xml_bytes = _build_secretarial_submission_xml(
                    presenter_id=presenter_id,
                    presenter_auth=presenter_auth,
                    environment=environment,
                    company_number=_coerce_text(row.get("company_number"), 20),
                    company_name=_coerce_text(row.get("company_name"), 200),
                    company_auth_code=company_auth_code,
                    filing_type=filing_type,
                    submission_number=reference,
                    transaction_id=transaction_id,
                    package_reference=package_reference,
                    form_data=form_data,
                    effective_date=effective_date or date.today(),
                )
                xml_validation_errors = _validate_ch_submission_xml_against_xsd(request_xml_bytes)
                if xml_validation_errors:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Generated {filing_type} XML failed CH XSD validation: {' | '.join(xml_validation_errors[:3])}",
                    )
                request_xml = request_xml_bytes.decode("utf-8", errors="replace")
                request_xml_hash = _secretarial_hash_text(request_xml)
                response_xml, response_root = _post_ch_gateway(request_xml_bytes)
                response_xml_hash = _secretarial_hash_text(response_xml)
                parsed = _parse_ch_submission_response(
                    response_text=response_xml,
                    response_root=response_root,
                    requested_submission_number=reference,
                )
                gateway_status = _xml_text(parsed.get("status"), "submitted")
                rejection_reason = _xml_text(parsed.get("rejectionReason"))
                response_payload = {
                    **response_payload,
                    "transactionId": transaction_id,
                    "statusCode": parsed.get("statusCode"),
                    "gatewayStatus": gateway_status,
                    "rejectionReason": rejection_reason,
                    "gatewayErrors": parsed.get("errors") or [],
                    "gatewayStatuses": parsed.get("statuses") or [],
                    "rawResponse": parsed.get("rawResponse") or "",
                }
                if gateway_status == "accepted":
                    ch_status = "Submission accepted"
                elif gateway_status == "rejected":
                    ch_status = rejection_reason or "Submission rejected"
                else:
                    ch_status = "Submission sent"
            elif filing_type == "DS01" and mode in {"api", "assisted"}:
                ch_status = (
                    "DS01 is not configured for XML software submission in this workflow. "
                    "Use manual/assisted filing and record CH acknowledgement."
                )
                response_payload = {
                    **response_payload,
                    "statusCode": "MANUAL_REQUIRED",
                    "gatewayStatus": "manual_required",
                    "rejectionReason": "",
                    "gatewayErrors": [],
                    "gatewayStatuses": [],
                    "rawResponse": "",
                }
                gateway_status = "manual_required"

            next_status = "SUBMITTED"
            if gateway_status == "accepted":
                next_status = "COMPLETED"
            elif gateway_status == "rejected":
                next_status = "REJECTED"
            elif gateway_status == "manual_required":
                next_status = "READY_TO_SUBMIT"
            cursor.execute(
                """
                UPDATE ch_secretarial_filings
                SET status = %s,
                    submitted_at = COALESCE(submitted_at, NOW()),
                    companies_house_status = %s,
                    companies_house_ref = %s,
                    prepared_submission = COALESCE(prepared_submission, '{}'::jsonb) || %s::jsonb,
                    updated_by_user_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    next_status,
                    ch_status,
                    reference,
                    json.dumps(
                        {
                            "lastSubmission": {
                                "requestXmlHash": request_xml_hash,
                                "responseXmlHash": response_xml_hash,
                                "requestXml": request_xml[:80000],
                                "responseXml": response_xml[:80000],
                                "responsePayload": response_payload,
                                "audit": audit_bundle,
                            }
                        }
                    ),
                    user_id,
                    filing_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_secretarial_filing', %s, 'submitted', %s::jsonb, %s)
                """,
                (
                    filing_id,
                    json.dumps(
                        {
                            "companiesHouseRef": reference,
                            "status": next_status,
                            "companiesHouseStatus": ch_status,
                            "requestXmlHash": request_xml_hash,
                            "responseXmlHash": response_xml_hash,
                            "audit": audit_bundle,
                            "rejectionReason": rejection_reason,
                        }
                    ),
                    user_id,
                ),
            )
            updated = _load_secretarial_filing(cursor, filing_id)
        connection.commit()
    return _serialise_secretarial_filing(updated)


def complete_company_secretarial_filing(user: dict, filing_id: str) -> dict:
    user_id = user.get("id") if isinstance(user, dict) else None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            row = _load_secretarial_filing(cursor, filing_id)
            if not row:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filing not found.")
            status_value = str(row.get("status") or "")
            if status_value not in {"SUBMITTED", "COMPLETED"}:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Submit filing before marking complete.")
            cursor.execute(
                """
                UPDATE ch_secretarial_filings
                SET status = 'COMPLETED',
                    companies_house_status = 'Accepted and archived',
                    updated_by_user_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (user_id, filing_id),
            )
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_secretarial_filing', %s, 'completed', %s::jsonb, %s)
                """,
                (filing_id, json.dumps({"status": "COMPLETED"}), user_id),
            )
            updated = _load_secretarial_filing(cursor, filing_id)
        connection.commit()
    return _serialise_secretarial_filing(updated)


def populate_auth_codes_from_register(user: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    company_ids = _chunk_company_ids(payload.get("companyIds") or [])
    force_overwrite = bool(payload.get("force"))
    include_auth_code = bool(payload.get("includeAuthCode"))
    user_id = user.get("id") if isinstance(user, dict) else None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            if company_ids:
                cursor.execute(
                    """
                    SELECT c.id, c.company_number, c.company_name, c.client_name,
                           c.client_id, c.assigned_staff_name,
                           (a.id IS NOT NULL) AS auth_code_on_file
                    FROM ch_companies c
                    LEFT JOIN ch_auth_codes a ON a.company_id = c.id
                    WHERE c.id = ANY(%s)
                    ORDER BY c.company_name ASC
                    """,
                    (company_ids,),
                )
            elif force_overwrite:
                cursor.execute(
                    """
                    SELECT c.id, c.company_number, c.company_name, c.client_name,
                           c.client_id, c.assigned_staff_name,
                           (a.id IS NOT NULL) AS auth_code_on_file
                    FROM ch_companies c
                    LEFT JOIN ch_auth_codes a ON a.company_id = c.id
                    ORDER BY c.company_name ASC
                    LIMIT 1000
                    """
                )
            else:
                cursor.execute(
                    """
                    SELECT c.id, c.company_number, c.company_name, c.client_name,
                           c.client_id, c.assigned_staff_name,
                           (a.id IS NOT NULL) AS auth_code_on_file
                    FROM ch_companies c
                    LEFT JOIN ch_auth_codes a ON a.company_id = c.id
                    WHERE a.id IS NULL
                    ORDER BY c.company_name ASC
                    LIMIT 1000
                    """
                )
            companies = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT id, company_number, normalised_name, code_encrypted, client_manager, client_id, uploaded_at
                FROM ch_auth_code_register
                ORDER BY uploaded_at DESC
                """
            )
            register_rows = cursor.fetchall() or []
        connection.commit()
    by_number: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in register_rows:
        number = normalise_company_number(row.get("company_number"))
        name = _coerce_text(row.get("normalised_name"), 250)
        if number and number not in by_number:
            by_number[number] = row
        if name and name not in by_name:
            by_name[name] = row
    populated: list[dict] = []
    skipped: list[dict] = []
    overwritten_count = 0
    for company in companies:
        company_id = str(company.get("id") or "")
        company_number = normalise_company_number(company.get("company_number"))
        company_name = _coerce_text(company.get("company_name") or company.get("client_name"), 250)
        had_existing_auth = bool(company.get("auth_code_on_file"))
        if had_existing_auth and not force_overwrite:
            skipped.append({"companyId": company_id, "companyNumber": company_number, "companyName": company_name, "reason": "Auth code already on file."})
            continue
        matched = by_number.get(company_number) if company_number else None
        match_type = "company_number"
        if not matched:
            normalised_company_name = _normalise_company_name_for_match(company_name)
            matched = by_name.get(normalised_company_name)
            match_type = "name"
        if not matched:
            skipped.append({"companyId": company_id, "companyNumber": company_number, "companyName": company_name, "reason": "No auth code register match found."})
            continue
        register_id = str(matched.get("id") or "")
        encrypted = _coerce_text(matched.get("code_encrypted"), 2000)
        register_number = normalise_company_number(matched.get("company_number"))
        register_name = _coerce_text(matched.get("normalised_name"), 250)
        register_client_manager = _coerce_text(matched.get("client_manager"), 200)
        register_client_id = _coerce_text(matched.get("client_id"), 80)
        existing_client_manager = _coerce_text(company.get("assigned_staff_name"), 200)
        existing_client_id = _coerce_text(company.get("client_id"), 80)
        auth_code = _decrypt_register_auth_code(encrypted, register_id, register_number, register_name)
        if not auth_code:
            skipped.append({"companyId": company_id, "companyNumber": company_number, "companyName": company_name, "reason": "Matched register entry could not be decrypted."})
            continue
        next_client_manager = register_client_manager if register_client_manager and (force_overwrite or not existing_client_manager) else existing_client_manager
        next_client_id = register_client_id if register_client_id and (force_overwrite or not existing_client_id) else existing_client_id
        with get_connection() as connection:
            with connection.cursor() as cursor:
                _save_company_auth_code(cursor, company_id, auth_code, user_id)
                if next_client_manager != existing_client_manager or next_client_id != existing_client_id:
                    cursor.execute(
                        """
                        UPDATE ch_companies
                        SET assigned_staff_name = %s,
                            client_id = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (next_client_manager, next_client_id, company_id),
                    )
                cursor.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                    VALUES ('ch_auth_code', %s, 'auth_code_populated_from_register', %s::jsonb, %s)
                    """,
                    (
                        company_id,
                        json.dumps({"matchType": match_type, "registerId": register_id}),
                        user_id,
                    ),
                )
            connection.commit()
        if had_existing_auth:
            overwritten_count += 1
        populated.append(
            {
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": company_name,
                "matchType": match_type,
                "clientManager": next_client_manager,
                "clientId": next_client_id,
                "authCode": auth_code if include_auth_code else "",
            }
        )
    return {
        "targetCount": len(companies),
        "populatedCount": len(populated),
        "overwrittenCount": overwritten_count,
        "skippedCount": len(skipped),
        "populated": populated,
        "skipped": skipped,
    }


def commit_clients_import(user: dict, preview: dict) -> dict:
    rows = preview.get("rows") or []
    if not isinstance(rows, list) or not rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No rows to import. Run the preview first.",
        )

    user_id = user.get("id") if isinstance(user, dict) else None
    filename = _coerce_text(preview.get("filename"), 250)
    created_count = 0
    updated_count = 0
    auth_codes_saved = 0
    errors_committed: list[dict] = []
    skipped_count = 0

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ch_imports (
                    import_type, filename, total_rows, uploaded_by_user_id, status
                )
                VALUES ('clients', %s, %s, %s, 'running')
                RETURNING id
                """,
                (filename, len(rows), user_id),
            )
            import_id = str(cursor.fetchone()["id"])

            for row in rows:
                data = row.get("data") or {}
                if row.get("included") is False:
                    skipped_count += 1
                    continue
                if not _looks_private_limited(data):
                    skipped_count += 1
                    errors_committed.append({
                        "lineNumber": row.get("lineNumber"),
                        "errors": ["Excluded: non-Ltd entity."],
                        "companyNumber": data.get("company_number"),
                    })
                    continue
                if row.get("errors"):
                    skipped_count += 1
                    errors_committed.append({
                        "lineNumber": row.get("lineNumber"),
                        "errors": row.get("errors"),
                        "companyNumber": data.get("company_number"),
                    })
                    continue
                company_number = normalise_company_number(data.get("company_number"))
                if not _is_valid_company_number(company_number):
                    skipped_count += 1
                    errors_committed.append({
                        "lineNumber": row.get("lineNumber"),
                        "errors": ["Invalid company number at commit time."],
                        "companyNumber": company_number,
                    })
                    continue
                try:
                    company_id, action = _upsert_company(cursor, {**data, "company_number": company_number}, user_id)
                except Exception as exc:  # pragma: no cover - DB level failure
                    skipped_count += 1
                    errors_committed.append({
                        "lineNumber": row.get("lineNumber"),
                        "errors": [str(exc) or exc.__class__.__name__],
                        "companyNumber": company_number,
                    })
                    continue

                if action == "create":
                    created_count += 1
                else:
                    updated_count += 1

                if data.get("auth_code"):
                    _save_company_auth_code(cursor, company_id, data["auth_code"], user_id)
                    auth_codes_saved += 1
                    cursor.execute(
                        """
                        INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                        VALUES ('ch_auth_code', %s, 'auth_code_uploaded', %s::jsonb, %s)
                        """,
                        (company_id, json.dumps({"via": "client_import", "import_id": import_id}), user_id),
                    )

                cursor.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                    VALUES ('ch_company', %s, %s, %s::jsonb, %s)
                    """,
                    (
                        company_id,
                        "company_created" if action == "create" else "company_updated",
                        json.dumps({"import_id": import_id, "companyNumber": company_number}),
                        user_id,
                    ),
                )

            summary = {
                "filename": filename,
                "createCount": created_count,
                "updateCount": updated_count,
                "skipCount": skipped_count,
                "authCodesSaved": auth_codes_saved,
                "headerProfile": preview.get("headerProfile") or {},
                "excludedNonLtdCount": int(preview.get("excludedNonLtdCount") or 0),
            }
            cursor.execute(
                """
                UPDATE ch_imports
                SET created_count = %s,
                    updated_count = %s,
                    skipped_count = %s,
                    error_count = %s,
                    errors = %s::jsonb,
                    summary = %s::jsonb,
                    status = 'completed',
                    completed_at = NOW()
                WHERE id = %s
                """,
                (
                    created_count,
                    updated_count,
                    skipped_count,
                    len(errors_committed),
                    json.dumps(errors_committed),
                    json.dumps(summary),
                    import_id,
                ),
            )
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_import', %s, 'bulk_import_run', %s::jsonb, %s)
                """,
                (import_id, json.dumps({"type": "clients", **summary}), user_id),
            )
        connection.commit()

    return {
        "importId": import_id,
        "createCount": created_count,
        "updateCount": updated_count,
        "skipCount": skipped_count,
        "errorCount": len(errors_committed),
        "authCodesSaved": auth_codes_saved,
        "errors": errors_committed,
    }


def _date_or_none(value):
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return None
    try:
        return str(value)
    except Exception:
        return None


def _normalise_workflow_review(value: object, *, user_id: object | None = None) -> dict:
    source = value if isinstance(value, dict) else {}
    raw_sections = source.get("sections") if isinstance(source.get("sections"), dict) else {}
    sections = {
        key: bool(raw_sections.get(key))
        for key in CH_WORKFLOW_REVIEW_SECTIONS
    }
    complete = all(sections.values())
    notes = _coerce_text(source.get("notes"), 2000) if "notes" in source else _coerce_text("", 2000)
    if source.get("completedAt"):
        completed_at = _xml_text(source.get("completedAt"))
    else:
        completed_at = utcnow().isoformat() if complete else None
    return {
        "sections": sections,
        "isComplete": complete,
        "completedAt": completed_at,
        "updatedAt": utcnow().isoformat(),
        "updatedByUserId": str(user_id) if user_id is not None else "",
        "notes": notes,
    }


def _workflow_review_complete(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    sections = value.get("sections") if isinstance(value.get("sections"), dict) else {}
    if not sections:
        return False
    return all(bool(sections.get(key)) for key in CH_WORKFLOW_REVIEW_SECTIONS)


def _serialise_company_row(row: dict, *, include_auth: bool = True) -> dict:
    today = date.today()
    filing_history = row.get("filing_history") if isinstance(row.get("filing_history"), list) else []
    next_due = row.get("next_due_date")
    next_made_up_to = row.get("next_made_up_to_date")
    last_filed = _latest_confirmation_statement_filed_date(filing_history) if filing_history else row.get("last_filed_date")
    if isinstance(next_due, date):
        due_in_days = (next_due - today).days
    else:
        due_in_days = None
    latest_submission_status = row.get("latest_submission_status") or ""
    latest_submission_invoice_id = row.get("latest_submission_xero_invoice_id") or ""
    latest_submission_completed_at = row.get("latest_submission_completed_at")
    internal_status = row.get("internal_status") or "active"
    blocked_internal = internal_status in {"paused", "do_not_file", "inactive"}
    has_due_date = isinstance(next_due, date)
    has_made_up_to_date = isinstance(next_made_up_to, date)
    has_auth = bool(row.get("auth_code_on_file"))
    submission_issues: list[str] = []
    if not has_due_date:
        submission_issues.append("Missing next due date.")
    if not has_made_up_to_date:
        submission_issues.append("Missing made-up-to date.")
    if not has_auth:
        submission_issues.append("Missing authentication code.")
    if blocked_internal:
        submission_issues.append(f"Blocked by internal status ({internal_status.replace('_', ' ')}).")
    filing_authority_status = str(row.get("filing_authority_status") or "pending")
    workflow_review = row.get("workflow_review") if isinstance(row.get("workflow_review"), dict) else {}
    workflow_review_complete = _workflow_review_complete(workflow_review)
    if filing_authority_status != "authorised" and not has_auth:
        submission_issues.append("Filing authority is not authorised.")
    authority_expires_at = row.get("filing_authority_expires_at")
    if authority_expires_at is not None:
        if not isinstance(authority_expires_at, datetime) or authority_expires_at.date() < today:
            submission_issues.append("Filing authority has expired.")
    cs01_validation_errors: list[str] = []
    if isinstance(next_made_up_to, date):
        try:
            cs_payload = _build_cs01_payload(row, include_change_sections=False)
            cs01_validation_errors = _validate_cs01_payload(
                row,
                next_made_up_to,
                cs_payload=cs_payload,
                include_change_sections=False,
            )
        except Exception as exc:  # pragma: no cover - defensive
            cs01_validation_errors = [f"Unable to prepare CS01 payload: {str(exc) or exc.__class__.__name__}"]
    for message in cs01_validation_errors:
        if message not in submission_issues:
            submission_issues.append(message)
    filed_within_last_12_months = bool(
        isinstance(last_filed, date)
        and last_filed >= (today - timedelta(days=365))
    )
    submission_warnings: list[str] = []
    if filed_within_last_12_months:
        submission_warnings.append(
            f"Recently filed on {last_filed.isoformat()}. Usually not due yet; submit early only if changes are required."
        )
    eligible_for_submission = bool(
        has_due_date
        and has_made_up_to_date
        and not blocked_internal
        and has_auth
        and not cs01_validation_errors
        and (
            row.get("filing_authority_expires_at") is None
            or (
                isinstance(row.get("filing_authority_expires_at"), datetime)
                and row.get("filing_authority_expires_at").date() >= today
            )
        )
    )
    eligible_for_invoicing = bool(
        latest_submission_status == "accepted"
        and latest_submission_completed_at is not None
        and not latest_submission_invoice_id
    )
    xero_contact_id = str(row.get("xero_link_contact_id") or "").strip()
    return {
        "id": str(row.get("id")) if row.get("id") else None,
        "companyNumber": row.get("company_number") or "",
        "companyName": row.get("company_name") or "",
        "clientId": row.get("client_id") or "",
        "clientName": row.get("client_name") or "",
        "contactEmail": row.get("contact_email") or "",
        "contactPhone": row.get("contact_phone") or "",
        "clientAddress": row.get("client_address") or "",
        "assignedStaffName": row.get("assigned_staff_name") or "",
        "registeredOffice": row.get("registered_office") or "",
        "companyStatus": row.get("company_status") or "",
        "incorporationDate": _date_or_none(row.get("incorporation_date")),
        "sicCodes": row.get("sic_codes") or [],
        "officers": row.get("officers") or [],
        "pscs": row.get("pscs") or [],
        "shareCapital": row.get("share_capital") or {},
        "workflowReview": workflow_review,
        "workflowReviewComplete": workflow_review_complete,
        "nextMadeUpToDate": _date_or_none(row.get("next_made_up_to_date")),
        "nextDueDate": _date_or_none(row.get("next_due_date")),
        "lastFiledDate": _date_or_none(last_filed),
        "filedWithinLast12Months": filed_within_last_12_months,
        "submissionWarnings": submission_warnings,
        "submissionIssues": submission_issues,
        "recommendedWorkflowAction": "changes-required" if submission_issues else "no-changes",
        "filingHistory": filing_history,
        "internalStatus": row.get("internal_status") or "active",
        "filingAuthorityStatus": row.get("filing_authority_status") or "pending",
        "filingAuthorityReference": row.get("filing_authority_reference") or "",
        "filingAuthorityReceivedAt": row.get("filing_authority_received_at").isoformat()
        if row.get("filing_authority_received_at")
        else None,
        "filingAuthorityExpiresAt": row.get("filing_authority_expires_at").isoformat()
        if row.get("filing_authority_expires_at")
        else None,
        "notes": row.get("notes") or "",
        "lastSyncedAt": row.get("last_synced_at").isoformat() if row.get("last_synced_at") else None,
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        "authCodeOnFile": bool(row.get("auth_code_on_file")) if include_auth else None,
        "authCodeHint": row.get("auth_code_hint") or "" if include_auth else "",
        "authCodeUploadedAt": row.get("auth_code_uploaded_at").isoformat() if include_auth and row.get("auth_code_uploaded_at") else None,
        "latestSubmissionId": str(row.get("latest_submission_id")) if row.get("latest_submission_id") else "",
        "latestSubmissionStatus": latest_submission_status,
        "latestSubmissionAt": row.get("latest_submission_at").isoformat() if row.get("latest_submission_at") else None,
        "latestSubmissionReference": row.get("latest_submission_reference") or "",
        "latestSubmissionXeroInvoiceId": latest_submission_invoice_id,
        "latestSubmissionCompletedAt": latest_submission_completed_at.isoformat() if latest_submission_completed_at else None,
        "dueInDays": due_in_days,
        "eligibleForSubmission": eligible_for_submission,
        "eligibleForInvoicing": eligible_for_invoicing,
        "xeroConnected": bool(xero_contact_id),
        "xeroContactId": xero_contact_id,
        "xeroCustomerId": str(row.get("xero_link_customer_id") or ""),
    }


def list_companies(filters: dict | None = None) -> list[dict]:
    filters = filters or {}
    where_clauses: list[str] = []
    params: list = []

    search = (filters.get("search") or "").strip().lower()
    if search:
        where_clauses.append(
            "(LOWER(c.company_name) LIKE %s OR LOWER(c.client_name) LIKE %s OR LOWER(c.company_number) LIKE %s OR LOWER(c.contact_email) LIKE %s OR LOWER(c.contact_phone) LIKE %s OR LOWER(c.client_address) LIKE %s)"
        )
        like = f"%{search}%"
        params.extend([like, like, like, like, like, like])

    internal_status = (filters.get("internalStatus") or "").strip()
    if internal_status and internal_status != "all":
        where_clauses.append("c.internal_status = %s")
        params.append(internal_status)

    only_missing_auth = bool(filters.get("missingAuth"))
    if only_missing_auth:
        where_clauses.append("a.id IS NULL")

    only_due_soon = bool(filters.get("dueSoon"))
    if only_due_soon:
        where_clauses.append("c.next_due_date IS NOT NULL AND c.next_due_date <= (CURRENT_DATE + INTERVAL '30 days')")

    only_overdue = bool(filters.get("overdue"))
    if only_overdue:
        where_clauses.append("c.next_due_date IS NOT NULL AND c.next_due_date < CURRENT_DATE")

    only_xero_connected = bool(filters.get("xeroConnected"))
    if only_xero_connected:
        where_clauses.append("NULLIF(TRIM(COALESCE(xero_link.xero_contact_id, '')), '') IS NOT NULL")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    sql = f"""
        SELECT c.*,
               (a.id IS NOT NULL) AS auth_code_on_file,
               a.code_hint AS auth_code_hint,
               a.uploaded_at AS auth_code_uploaded_at,
               latest.id AS latest_submission_id,
               latest.status AS latest_submission_status,
               latest.submitted_at AS latest_submission_at,
               latest.submission_reference AS latest_submission_reference,
               latest.xero_invoice_id AS latest_submission_xero_invoice_id,
               latest.completed_at AS latest_submission_completed_at,
               xero_link.customer_id AS xero_link_customer_id,
               xero_link.xero_contact_id AS xero_link_contact_id
        FROM ch_companies c
        LEFT JOIN ch_auth_codes a ON a.company_id = c.id
        LEFT JOIN LATERAL (
            SELECT s.id, s.status, s.submitted_at, s.submission_reference, s.xero_invoice_id, s.completed_at
            FROM ch_submissions s
            WHERE s.company_id = c.id
            ORDER BY s.submitted_at DESC
            LIMIT 1
        ) latest ON TRUE
        LEFT JOIN LATERAL (
            SELECT cust.id AS customer_id,
                   cust.xero_contact_id
            FROM customers cust
            WHERE (
                (
                    NULLIF(TRIM(COALESCE(c.client_id, '')), '') IS NOT NULL
                    AND (
                        LOWER(COALESCE(cust.account_number, '')) = LOWER(c.client_id)
                        OR LOWER(COALESCE(cust.id::text, '')) = LOWER(c.client_id)
                    )
                )
                OR (
                    NULLIF(TRIM(COALESCE(c.company_name, '')), '') IS NOT NULL
                    AND LOWER(COALESCE(cust.name, '')) = LOWER(c.company_name)
                )
                OR (
                    NULLIF(TRIM(COALESCE(c.contact_email, '')), '') IS NOT NULL
                    AND LOWER(COALESCE(cust.email, '')) = LOWER(c.contact_email)
                )
            )
            ORDER BY
                CASE
                    WHEN (
                        NULLIF(TRIM(COALESCE(c.client_id, '')), '') IS NOT NULL
                        AND (
                            LOWER(COALESCE(cust.account_number, '')) = LOWER(c.client_id)
                            OR LOWER(COALESCE(cust.id::text, '')) = LOWER(c.client_id)
                        )
                    ) THEN 0
                    WHEN (
                        NULLIF(TRIM(COALESCE(c.company_name, '')), '') IS NOT NULL
                        AND LOWER(COALESCE(cust.name, '')) = LOWER(c.company_name)
                    ) THEN 1
                    ELSE 2
                END,
                cust.updated_at DESC NULLS LAST
            LIMIT 1
        ) xero_link ON TRUE
        {where_sql}
        ORDER BY
            CASE WHEN c.next_due_date IS NULL THEN 1 ELSE 0 END,
            c.next_due_date ASC,
            c.company_name ASC
        LIMIT 1000
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall() or []
        connection.commit()

    serialised_rows = [_serialise_company_row(row) for row in rows]
    if only_overdue:
        serialised_rows = [
            row for row in serialised_rows
            if isinstance(row.get("dueInDays"), int) and row["dueInDays"] < 0 and not bool(row.get("filedWithinLast12Months"))
        ]
    return serialised_rows


def _chunk_company_ids(company_ids: list[str]) -> list[str]:
    normalised = [str(company_id or "").strip() for company_id in company_ids]
    cleaned = [company_id for company_id in normalised if company_id]
    if not cleaned:
        return []
    invalid_ids: list[str] = []
    valid_ids: list[str] = []
    for company_id in cleaned:
        try:
            valid_ids.append(str(UUID(company_id)))
        except (ValueError, TypeError, AttributeError):
            invalid_ids.append(company_id)
    if invalid_ids:
        invalid_preview = ", ".join(invalid_ids[:3])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid companyIds supplied. Use UUID values only. Invalid values: {invalid_preview}",
        )
    return valid_ids


def _submission_idempotency_key(company_id: str, review_date: date) -> str:
    raw = f"{company_id}:{review_date.isoformat()}:cs01"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_submission_candidates(company_ids: list[str]) -> list[dict]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.*,
                       (a.id IS NOT NULL) AS auth_code_on_file,
                       a.code_hint AS auth_code_hint,
                       a.uploaded_at AS auth_code_uploaded_at
                FROM ch_companies c
                LEFT JOIN ch_auth_codes a ON a.company_id = c.id
                WHERE c.id = ANY(%s)
                ORDER BY
                    CASE WHEN c.next_due_date IS NULL THEN 1 ELSE 0 END,
                    c.next_due_date ASC,
                    c.company_name ASC
                """,
                (company_ids,),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return rows


def bulk_submit_confirmation_statements(
    user: dict,
    payload: dict | None = None,
    *,
    preflight_only: bool = False,
    progress_callback=None,
) -> dict:
    payload = payload or {}
    company_ids = _chunk_company_ids(payload.get("companyIds") or [])
    raw_workflow_actions = payload.get("workflowActions") if isinstance(payload.get("workflowActions"), dict) else {}
    raw_auth_code_overrides = payload.get("authCodeOverrides") if isinstance(payload.get("authCodeOverrides"), dict) else {}
    workflow_actions_by_company_id = {
        str(company_id or "").strip(): (
            "changes-required"
            if str(action or "").strip().lower() == "changes-required"
            else "no-changes"
        )
        for company_id, action in (raw_workflow_actions or {}).items()
        if str(company_id or "").strip()
    }
    auth_code_overrides_by_company_id = {
        str(company_id or "").strip(): re.sub(r"[^A-Z0-9]", "", str(code or "").strip().upper())
        for company_id, code in (raw_auth_code_overrides or {}).items()
        if str(company_id or "").strip()
    }
    if not company_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one company.")
    if len(company_ids) > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Maximum 500 companies per bulk submission.")
    settings_row = _ensure_settings_row()
    environment = _xml_text(settings_row.get("environment"), "sandbox")
    presenter_id = configured_presenter_id()
    presenter_auth = decrypt_presenter_auth()
    credit_account_number = _xml_text(settings_row.get("credit_account_number"))
    package_reference = configured_package_reference()
    auth_method = _ch_auth_method()
    configured_fee_amount = _coerce_settings_amount(
        settings_row.get("xero_invoice_unit_amount"),
        "xeroInvoiceUnitAmount",
    )
    if configured_fee_amount <= Decimal("0.00"):
        configured_fee_amount = Decimal("13.00")
    preflight_errors: list[str] = []
    if not presenter_id:
        preflight_errors.append("Set Presenter ID in Companies House settings.")
    if not presenter_auth:
        preflight_errors.append("Set Presenter authentication code in Companies House settings.")
    if not credit_account_number:
        preflight_errors.append("Set Companies House credit account number in settings.")
    if not package_reference:
        preflight_errors.append(
            "Set COMPANIES_HOUSE_PACKAGE_REFERENCE to the package reference issued by Companies House "
            "for your software filing account (do not reuse the Presenter ID)."
        )
    if auth_method not in {"CHMD5", "MD5", "clear"}:
        preflight_errors.append(
            "Set COMPANIES_HOUSE_AUTH_METHOD to one of: MD5 (default), CHMD5, or clear."
        )
    auth_code_missing: list[str] = []
    for company_id in company_ids:
        override = auth_code_overrides_by_company_id.get(company_id)
        if override:
            continue
        stored = _load_company_auth_code(company_id)
        if not stored:
            auth_code_missing.append(company_id)
    if auth_code_missing:
        preflight_errors.append(
            "Provide a 6-character Companies House company authentication code for each selected company. "
            f"Missing for {len(auth_code_missing)} company/companies — open the row and enter the code, "
            "or supply it via the auth code override."
        )
    if preflight_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Cannot submit CS01 yet. Resolve the following before retrying: "
                + " ".join(f"({index}) {message}" for index, message in enumerate(preflight_errors, start=1))
            ),
        )

    if preflight_only:
        return {"preflight": "ok", "totalCount": len(company_ids)}

    user_id = user.get("id") if isinstance(user, dict) else None
    companies = _resolve_submission_candidates(company_ids)
    found_ids = {str(row.get("id")) for row in companies if row.get("id")}
    missing_ids = [company_id for company_id in company_ids if company_id not in found_ids]

    submitted: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    now = utcnow()
    today = date.today()

    def _submission_support_report_path(submission_id: str | None = None) -> str:
        if submission_id:
            return (
                "/api/companies-house/submissions/support-report.txt"
                f"?status=all&limit=1&submission_id={submission_id}"
            )
        return "/api/companies-house/submissions/support-report.txt?status=rejected&limit=50"

    def _record_submission_skip(*, company_id: str, company_number: str, reason: str) -> None:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                    VALUES ('ch_company', %s, 'bulk_submission_skipped', %s::jsonb, %s)
                    """,
                    (
                        company_id,
                        json.dumps(
                            {
                                "companyNumber": company_number,
                                "workflow": "confirmation_statement_bulk",
                                "reason": reason,
                            }
                        ),
                        user_id,
                    ),
                )
            connection.commit()

    total_companies = len(companies)
    for company_index, row in enumerate(companies, start=1):
        company_id = str(row.get("id") or "")
        company_number = row.get("company_number") or ""
        company_name = row.get("company_name") or row.get("client_name") or ""
        if progress_callback is not None:
            try:
                progress_callback(
                    {
                        "processed": company_index - 1,
                        "total": total_companies,
                        "currentCompanyId": company_id,
                        "currentCompanyName": company_name,
                        "currentCompanyNumber": company_number,
                        "submittedCount": len(submitted),
                        "skippedCount": len(skipped),
                        "failedCount": len(failed),
                    }
                )
            except Exception:
                logger.exception("bulk_submit progress_callback raised; continuing")
        internal_status = row.get("internal_status") or "active"
        filing_authority_status = str(row.get("filing_authority_status") or "pending").strip().lower()
        filing_authority_expires_at = row.get("filing_authority_expires_at")
        next_due = row.get("next_due_date")
        has_auth = bool(row.get("auth_code_on_file"))
        made_up_to = row.get("next_made_up_to_date")
        if internal_status in {"paused", "do_not_file", "inactive"}:
            reason = f"Internal status is '{internal_status}'."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append({
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": company_name,
                "reason": reason,
            })
            continue
        if filing_authority_status != "authorised" and not has_auth:
            reason = "Client filing authority is not recorded as authorised."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append({
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": company_name,
                "reason": reason,
            })
            continue
        if isinstance(filing_authority_expires_at, datetime) and filing_authority_expires_at.date() < today:
            reason = "Client filing authority has expired."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append({
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": company_name,
                "reason": reason,
            })
            continue
        if not has_auth:
            reason = "Missing Companies House authentication code."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append({
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": company_name,
                "reason": reason,
            })
            continue
        if not isinstance(made_up_to, date):
            reason = "Missing made up to date. Sync Companies House company data before submitting CS01."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append({
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": company_name,
                "reason": reason,
            })
            continue

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, status, submitted_at, xero_invoice_id
                    FROM ch_submissions
                    WHERE company_id = %s
                    ORDER BY submitted_at DESC NULLS LAST, created_at DESC
                    LIMIT 1
                    """,
                    (company_id,),
                )
                latest_submission = cursor.fetchone() or {}
            connection.commit()
        latest_status = str(latest_submission.get("status") or "").strip().lower()
        latest_submitted_at = latest_submission.get("submitted_at")
        latest_invoice_id = str(latest_submission.get("xero_invoice_id") or "").strip()
        if latest_status in {"queued", "submitted"}:
            reason = f"Latest submission is already {latest_status}."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append(
                {
                    "companyId": company_id,
                    "companyNumber": company_number,
                    "companyName": company_name,
                    "reason": reason,
                }
            )
            continue
        if (
            latest_status in {"accepted"}
            and not latest_invoice_id
            and isinstance(latest_submitted_at, datetime)
            and (today - latest_submitted_at.date()).days < 330
        ):
            reason = "Latest accepted submission is still pending invoicing."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append(
                {
                    "companyId": company_id,
                    "companyNumber": company_number,
                    "companyName": company_name,
                    "reason": reason,
                }
            )
            continue

        company_auth_code = auth_code_overrides_by_company_id.get(company_id) or _load_company_auth_code(company_id)
        if not company_auth_code:
            reason = "Authentication code could not be decrypted for this company."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append(
                {
                    "companyId": company_id,
                    "companyNumber": company_number,
                    "companyName": company_name,
                    "reason": reason,
                }
            )
            continue
        if not re.fullmatch(r"[A-Z0-9]{6}", company_auth_code):
            reason = "Authentication code must be 6 alphanumeric characters."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append(
                {
                    "companyId": company_id,
                    "companyNumber": company_number,
                    "companyName": company_name,
                    "reason": reason,
                }
            )
            continue

        submission_reference = _next_unique_submission_number()
        transaction_id = _ch_txn_id()
        review_date = made_up_to
        workflow_action = workflow_actions_by_company_id.get(company_id, "no-changes")
        include_change_sections = workflow_action == "changes-required"
        if include_change_sections and not _workflow_review_complete(row.get("workflow_review")):
            reason = (
                "Changes required selected but full confirmation statement walkthrough is not complete. "
                "Open workflow review and complete all sections first."
            )
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append(
                {
                    "companyId": company_id,
                    "companyNumber": company_number,
                    "companyName": company_name,
                    "reason": reason,
                }
            )
            continue
        cs_payload = _build_cs01_payload(row, include_change_sections=include_change_sections)
        if not include_change_sections:
            cs_payload = _prefill_no_changes_cs01_payload(row, cs_payload, review_date)
        validation_errors = _validate_cs01_payload(
            row,
            review_date,
            cs_payload=cs_payload,
            include_change_sections=include_change_sections,
        )
        if validation_errors:
            reason = " | ".join(validation_errors[:5])
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append(
                {
                    "companyId": company_id,
                    "companyNumber": company_number,
                    "companyName": company_name,
                    "reason": reason,
                }
            )
            continue
        idempotency_key = _submission_idempotency_key(company_id, review_date)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ch_submissions (
                        company_id,
                        idempotency_key,
                        attempt_type,
                        submission_reference,
                        transaction_id,
                        fee_amount,
                        payment_reference,
                        status,
                        response_payload,
                        submitted_by_user_id,
                        submitted_at,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, 'submit', %s, %s, %s, %s, 'queued', %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        company_id,
                        idempotency_key,
                        submission_reference,
                        transaction_id,
                        configured_fee_amount,
                        credit_account_number,
                        json.dumps(
                            {
                                "queuedAt": now.isoformat(),
                                "source": "bulk_workflow",
                                "mode": "live_gateway",
                                "companyNumber": company_number,
                                "workflowAction": workflow_action,
                                "csPayload": cs_payload,
                            }
                        ),
                        user_id,
                        now,
                        now,
                        now,
                    ),
                )
                queued_row = cursor.fetchone()
                if not queued_row:
                    cursor.execute(
                        """
                        UPDATE ch_submissions
                        SET attempt_type = 'submit',
                            submission_reference = %s,
                            transaction_id = %s,
                            fee_amount = %s,
                            payment_reference = %s,
                            status = 'queued',
                            rejection_reason = '',
                            response_payload = %s::jsonb,
                            submitted_by_user_id = %s,
                            submitted_at = %s,
                            updated_at = %s,
                            completed_at = NULL,
                            dead_letter = FALSE,
                            dead_letter_reason = ''
                        WHERE idempotency_key = %s
                          AND status = 'rejected'
                        RETURNING id
                        """,
                        (
                            submission_reference,
                            transaction_id,
                            configured_fee_amount,
                            credit_account_number,
                            json.dumps(
                                {
                                    "queuedAt": now.isoformat(),
                                    "source": "bulk_workflow",
                                    "mode": "live_gateway",
                                    "companyNumber": company_number,
                                    "workflowAction": workflow_action,
                                }
                            ),
                            user_id,
                            now,
                            now,
                            idempotency_key,
                        ),
                    )
                    queued_row = cursor.fetchone()
            connection.commit()
        if not queued_row:
            reason = "Latest submission is not retryable yet (already queued/submitted/accepted for this period)."
            _record_submission_skip(company_id=company_id, company_number=company_number, reason=reason)
            skipped.append(
                {
                    "companyId": company_id,
                    "companyNumber": company_number,
                    "companyName": company_name,
                    "reason": reason,
                }
            )
            continue
        submission_id = str(queued_row["id"])
        def _record_submission_failure(rejection_reason: str, failure_stage: str) -> None:
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE ch_submissions
                        SET fee_amount = %s,
                            payment_reference = %s,
                            status = %s,
                            rejection_reason = %s,
                            response_payload = %s::jsonb,
                            updated_at = %s,
                            completed_at = %s,
                            dead_letter = TRUE,
                            dead_letter_reason = %s
                        WHERE id = %s
                        """,
                        (
                            configured_fee_amount,
                            credit_account_number,
                            "rejected",
                            rejection_reason,
                            json.dumps(
                                {
                                    "queuedAt": now.isoformat(),
                                    "source": "bulk_workflow",
                                    "mode": "live_gateway",
                                    "companyNumber": company_number,
                                    "workflowAction": workflow_action,
                                    "failureStage": failure_stage,
                                }
                            ),
                            now,
                            now,
                            rejection_reason,
                            submission_id,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                        VALUES ('ch_submission', %s, 'bulk_submission_failed_live', %s::jsonb, %s)
                        """,
                        (
                            submission_id,
                            json.dumps(
                                {
                                    "companyId": company_id,
                                    "companyNumber": company_number,
                                    "workflow": "confirmation_statement_bulk",
                                    "reason": rejection_reason,
                                }
                            ),
                            user_id,
                        ),
                    )
                connection.commit()
            _record_dead_letter(
                company_id=company_id,
                submission_id=submission_id,
                stage=failure_stage,
                reason=rejection_reason,
                payload={"companyNumber": company_number, "transactionId": transaction_id},
            )
            failed.append(
                {
                    "submissionId": submission_id,
                    "companyId": company_id,
                    "companyNumber": company_number,
                    "companyName": company_name,
                    "reason": rejection_reason,
                    "supportReportPath": _submission_support_report_path(submission_id),
                }
            )

        try:
            request_xml = _build_ch_submission_xml(
                presenter_id=presenter_id,
                presenter_auth=presenter_auth,
                environment=environment,
                company_number=company_number,
                company_name=_xml_text(row.get("company_name"), row.get("client_name") or "UNKNOWN COMPANY"),
                company_auth_code=company_auth_code,
                review_date=review_date,
                registered_email=_xml_text(row.get("contact_email")),
                package_reference=package_reference,
                transaction_id=transaction_id,
                submission_number=submission_reference,
                cs_payload=cs_payload,
            )
            xml_validation_errors = _validate_ch_submission_xml_against_xsd(request_xml)
            if xml_validation_errors:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Generated CS01 XML failed CH XSD validation: {' | '.join(xml_validation_errors[:3])}",
                )
            response_text, response_root = _post_ch_gateway(request_xml)
            parsed_submission = _parse_ch_submission_response(
                response_text=response_text,
                response_root=response_root,
                requested_submission_number=submission_reference,
            )
        except HTTPException as exc:
            rejection_reason = _enhance_authorisation_failure_reason(
                str(exc.detail),
                environment=environment,
                presenter_id=presenter_id,
                presenter_auth=presenter_auth,
                company_auth_code=company_auth_code,
                company_number=company_number,
            )
            _record_submission_failure(rejection_reason, "gateway_submission")
            continue
        except Exception as exc:  # pragma: no cover - defensive guard to avoid aborting full bulk run
            rejection_reason = (
                "Unexpected error while submitting to Companies House. "
                f"{str(exc) or exc.__class__.__name__}"
            )
            logger.exception(
                "Unexpected Companies House bulk submission failure for %s (%s)",
                company_number,
                company_id,
            )
            _record_submission_failure(rejection_reason, "gateway_submission_unexpected")
            continue

        try:
            status_value = _xml_text(parsed_submission.get("status"), "submitted")
            rejection_reason = _enhance_authorisation_failure_reason(
                _xml_text(parsed_submission.get("rejectionReason")),
                environment=environment,
                presenter_id=presenter_id,
                presenter_auth=presenter_auth,
                company_auth_code=company_auth_code,
                company_number=company_number,
            )
            fee_amount = configured_fee_amount
            payment_evidence = parsed_submission.get("paymentEvidence") or {}
            payment_reconciliation: dict | None = None
            if status_value == "accepted" and not _payment_evidence_complete(payment_evidence):
                payment_reconciliation = _status_poll_payment_reconciliation(
                    presenter_id=presenter_id,
                    presenter_auth=presenter_auth,
                    environment=environment,
                    submission_number=submission_reference,
                    now=now,
                )
                payment_evidence = {
                    **payment_evidence,
                    **(payment_reconciliation.get("paymentEvidence") or {}),
                }
            if status_value == "accepted" and not _payment_evidence_complete(payment_evidence):
                payment_evidence = {
                    **payment_evidence,
                    **_payment_confirmation_fallback_evidence(
                        source="gateway_accept_without_payment_fields",
                        status_code=_xml_text(parsed_submission.get("statusCode"), "ACCEPT"),
                        now=now,
                    ),
                }
            payment_confirmed = True if status_value == "accepted" else None
            response_payload = {
                "queuedAt": now.isoformat(),
                "source": "bulk_workflow",
                "mode": "live_gateway",
                "companyNumber": company_number,
                "workflowAction": workflow_action,
                "gatewayStatusCode": parsed_submission.get("statusCode"),
                "gatewayStatuses": parsed_submission.get("statuses") or [],
                "gatewayErrors": parsed_submission.get("errors") or [],
                "paymentEvidence": payment_evidence,
                "paymentReconciliation": payment_reconciliation or {},
                "csPayload": cs_payload,
                "rawResponse": parsed_submission.get("rawResponse") or "",
            }

            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE ch_submissions
                        SET fee_amount = %s,
                            payment_reference = %s,
                            status = %s,
                            rejection_reason = %s,
                            payment_confirmed = %s,
                            payment_evidence = %s::jsonb,
                            response_payload = %s::jsonb,
                            updated_at = %s,
                            completed_at = %s
                        WHERE id = %s
                        """,
                        (
                            fee_amount,
                            credit_account_number,
                            status_value,
                            rejection_reason,
                            payment_confirmed,
                            json.dumps(payment_evidence),
                            json.dumps(response_payload),
                            now,
                            now if status_value in {"accepted", "rejected"} else None,
                            submission_id,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                        VALUES ('ch_submission', %s, %s, %s::jsonb, %s)
                        """,
                        (
                            submission_id,
                            "bulk_submission_sent_live" if status_value != "rejected" else "bulk_submission_rejected_live",
                            json.dumps({
                                "companyId": company_id,
                                "companyNumber": company_number,
                                "workflow": "confirmation_statement_bulk",
                                "statusCode": parsed_submission.get("statusCode"),
                                "errors": parsed_submission.get("errors") or [],
                            }),
                            user_id,
                        ),
                    )
                connection.commit()
            if status_value == "rejected":
                _record_dead_letter(
                    company_id=company_id,
                    submission_id=submission_id,
                    stage="gateway_submission",
                    reason=rejection_reason or "Gateway rejected submission",
                    payload={"statusCode": parsed_submission.get("statusCode"), "paymentEvidence": payment_evidence},
                )

            submitted.append({
                "submissionId": submission_id,
                "companyId": company_id,
                "companyName": row.get("company_name") or "",
                "companyNumber": company_number,
                "status": status_value,
                "submittedAt": now.isoformat(),
                "submissionReference": submission_reference,
                "transactionId": transaction_id,
                "rejectionReason": rejection_reason,
                "supportReportPath": _submission_support_report_path(submission_id),
            })
        except Exception as exc:  # pragma: no cover - defensive guard to avoid aborting full bulk run
            rejection_reason = (
                "Unexpected error while finalising Companies House submission. "
                f"{str(exc) or exc.__class__.__name__}"
            )
            logger.exception(
                "Unexpected Companies House bulk submission post-processing failure for %s (%s)",
                company_number,
                company_id,
            )
            _record_submission_failure(rejection_reason, "gateway_postprocess_unexpected")
            continue

    if progress_callback is not None:
        try:
            progress_callback(
                {
                    "processed": total_companies,
                    "total": total_companies,
                    "currentCompanyId": "",
                    "currentCompanyName": "",
                    "currentCompanyNumber": "",
                    "submittedCount": len(submitted),
                    "skippedCount": len(skipped),
                    "failedCount": len(failed),
                    "stage": "reconciliation",
                }
            )
        except Exception:
            logger.exception("bulk_submit progress_callback raised; continuing")

    for missing_id in missing_ids:
        skipped.append({
            "companyId": missing_id,
            "companyNumber": "",
            "companyName": "",
            "reason": "Company not found.",
        })

    pending_submission_numbers = [
        item.get("submissionReference")
        for item in submitted
        if item.get("status") == "submitted" and item.get("submissionReference")
    ]
    reconciliation = {}
    if pending_submission_numbers:
        try:
            reconciliation = run_companies_house_submission_reconciliation(
                {"submissionNumbers": pending_submission_numbers, "limit": len(pending_submission_numbers)}
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Post-submit reconciliation failed")
            reconciliation = {"error": str(exc) or exc.__class__.__name__}

    return {
        "submittedCount": len(submitted),
        "skippedCount": len(skipped),
        "failedCount": len(failed),
        "submitted": submitted,
        "skipped": skipped,
        "failed": failed,
        "reconciliation": reconciliation,
        "supportReportPath": _submission_support_report_path(),
    }


def _coerce_user_uuid(user_id) -> str | None:
    if not user_id:
        return None
    try:
        return str(UUID(str(user_id)))
    except (ValueError, TypeError, AttributeError):
        return None


def create_bulk_submission_job(user: dict, payload: dict) -> str:
    user_id = _coerce_user_uuid(user.get("id") if isinstance(user, dict) else None)
    company_ids = payload.get("companyIds") if isinstance(payload, dict) else []
    total = len(company_ids) if isinstance(company_ids, list) else 0
    initial_progress = {
        "processed": 0,
        "total": total,
        "currentCompanyName": "",
        "currentCompanyNumber": "",
        "submittedCount": 0,
        "skippedCount": 0,
        "failedCount": 0,
        "stage": "queued",
    }
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ch_bulk_jobs (user_id, job_type, status, payload, progress)
                VALUES (%s, 'confirmation_statement_bulk', 'queued', %s::jsonb, %s::jsonb)
                RETURNING id
                """,
                (user_id, json.dumps(payload or {}), json.dumps(initial_progress)),
            )
            row = cursor.fetchone()
        connection.commit()
    return str(row["id"])


def _update_bulk_job_progress(job_id: str, progress: dict) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ch_bulk_jobs
                SET progress = %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (json.dumps(progress or {}), job_id),
            )
        connection.commit()


def _mark_bulk_job_running(job_id: str) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ch_bulk_jobs
                SET status = 'running',
                    started_at = COALESCE(started_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (job_id,),
            )
        connection.commit()


def _finalise_bulk_job(job_id: str, *, status_value: str, result: dict | None = None, error: str = "") -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE ch_bulk_jobs
                SET status = %s,
                    result = %s::jsonb,
                    error = %s,
                    finished_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (status_value, json.dumps(result or {}), error or "", job_id),
            )
        connection.commit()


def get_bulk_submission_job(job_id: str) -> dict:
    reference = (job_id or "").strip()
    if not reference:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Job id is required.")
    try:
        UUID(reference)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job id.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, user_id, job_type, status, progress, result, error,
                       started_at, finished_at, created_at, updated_at
                FROM ch_bulk_jobs
                WHERE id = %s
                """,
                (reference,),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Bulk job {reference} not found.")
    progress = row.get("progress") or {}
    result = row.get("result") or {}
    if isinstance(progress, str):
        try:
            progress = json.loads(progress)
        except ValueError:
            progress = {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {}
    return {
        "jobId": str(row.get("id")),
        "status": _xml_text(row.get("status"), "queued"),
        "jobType": _xml_text(row.get("job_type")),
        "progress": progress if isinstance(progress, dict) else {},
        "result": result if isinstance(result, dict) else {},
        "error": _xml_text(row.get("error")),
        "startedAt": row.get("started_at").isoformat() if row.get("started_at") else None,
        "finishedAt": row.get("finished_at").isoformat() if row.get("finished_at") else None,
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


def run_bulk_submission_job(job_id: str, user: dict, payload: dict) -> None:
    """Background worker entry point. Runs bulk_submit_confirmation_statements
    and writes status/progress/result to ch_bulk_jobs."""
    try:
        _mark_bulk_job_running(job_id)

        def _on_progress(progress: dict) -> None:
            try:
                _update_bulk_job_progress(job_id, progress)
            except Exception:
                logger.exception("Failed to persist bulk job progress for %s", job_id)

        result = bulk_submit_confirmation_statements(user, payload, progress_callback=_on_progress)
        _finalise_bulk_job(job_id, status_value="completed", result=result)
    except HTTPException as exc:
        _finalise_bulk_job(
            job_id,
            status_value="failed",
            error=str(exc.detail) or exc.__class__.__name__,
        )
    except Exception as exc:
        logger.exception("Unexpected bulk job failure for %s", job_id)
        _finalise_bulk_job(
            job_id,
            status_value="failed",
            error=str(exc) or exc.__class__.__name__,
        )


def _resolve_company_contact_for_invoice(cursor, company: dict) -> dict:
    client_id = str(company.get("client_id") or "").strip()
    client_name = str(company.get("client_name") or "").strip()
    contact_email = str(company.get("contact_email") or "").strip().lower()
    candidates: list[dict] = []

    if client_id:
        cursor.execute(
            """
            SELECT id, xero_contact_id, name, email
            FROM customers
            WHERE id::text = %s OR xero_contact_id = %s
            LIMIT 1
            """,
            (client_id, client_id),
        )
        row = cursor.fetchone()
        if row:
            candidates.append(row)

    if not candidates and client_name:
        cursor.execute(
            """
            SELECT id, xero_contact_id, name, email
            FROM customers
            WHERE LOWER(name) = LOWER(%s)
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
            """,
            (client_name,),
        )
        row = cursor.fetchone()
        if row:
            candidates.append(row)

    if not candidates and contact_email:
        cursor.execute(
            """
            SELECT id, xero_contact_id, name, email
            FROM customers
            WHERE LOWER(COALESCE(email, '')) = %s
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
            """,
            (contact_email,),
        )
        row = cursor.fetchone()
        if row:
            candidates.append(row)

    return candidates[0] if candidates else {}


def _workflow_bcc_email() -> str:
    settings = get_settings()
    return str(settings.me_report_bcc_email or CH_WORKFLOW_DEFAULT_BCC_EMAIL).strip() or CH_WORKFLOW_DEFAULT_BCC_EMAIL


def _workflow_invoice_pdf_filename(company_number: str, invoice_number: str, company_name: str) -> str:
    if invoice_number:
        safe_invoice_number = re.sub(r"[^A-Za-z0-9_-]+", "-", invoice_number).strip("-")
        if safe_invoice_number:
            return f"{safe_invoice_number}.pdf"
    if company_number:
        safe_company_number = re.sub(r"[^A-Za-z0-9_-]+", "-", company_number).strip("-")
        if safe_company_number:
            return f"confirmation-statement-invoice-{safe_company_number}.pdf"
    safe_company_name = re.sub(r"[^A-Za-z0-9_-]+", "-", company_name or "client").strip("-").lower() or "client"
    return f"confirmation-statement-invoice-{safe_company_name}.pdf"


def _workflow_email_subject(company_name: str, company_number: str, invoice_number: str) -> str:
    base = f"Confirmation statement submitted for {company_name or 'your company'}"
    if company_number:
        base = f"{base} ({company_number})"
    if invoice_number:
        return f"{base} · Invoice {invoice_number}"
    return base


def _workflow_email_body(company_name: str, company_number: str, invoice_number: str) -> str:
    company_label = company_name or "your company"
    number_suffix = f" ({company_number})" if company_number else ""
    invoice_suffix = f" {invoice_number}" if invoice_number else ""
    return (
        f"Hello,\n\n"
        f"We have prepared and submitted your confirmation statement for {company_label}{number_suffix}.\n"
        f"Please find attached invoice{invoice_suffix} for the Companies House filing disbursement.\n\n"
        f"Kind regards,\n"
        f"Jaccountancy"
    )


def _send_workflow_email_smtp(recipient: str, subject: str, body: str, pdf_bytes: bytes, pdf_filename: str, bcc_email: str) -> None:
    settings = get_settings()
    if not settings.smtp_host or not settings.smtp_from_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SMTP is not configured. Add SMTP_HOST and SMTP_FROM_EMAIL before sending workflow emails.",
        )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((settings.smtp_from_name, settings.smtp_from_email))
    message["To"] = recipient
    message.set_content(body)
    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_filename)
    recipients = [recipient]
    if bcc_email:
        recipients.append(bcc_email)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username and settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message, to_addrs=recipients)
    except OSError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"SMTP send failed: {exc}") from exc
    except smtplib.SMTPException as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"SMTP send failed: {exc}") from exc


async def _send_workflow_email_gmail(
    user: dict,
    recipient: str,
    subject: str,
    body: str,
    pdf_bytes: bytes,
    pdf_filename: str,
    bcc_email: str,
) -> None:
    connection_row = gmail_connection_for_user(user)
    if not connection_row:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Gmail is not connected for this user.")
    connection_row = await refresh_gmail_connection(connection_row)
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr(("Jaccountancy", connection_row.get("gmail_email") or user.get("email") or ""))
    message["To"] = recipient
    if bcc_email:
        message["Bcc"] = bcc_email
    message.set_content(body)
    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_filename)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f'Bearer {connection_row["access_token"]}', "Content-Type": "application/json"},
            json={"raw": raw},
        )
    if response.is_error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Gmail send failed: {response.text[:500]}")


async def _send_confirmation_statement_invoice_email(
    user: dict,
    recipient: str,
    company_name: str,
    company_number: str,
    invoice_number: str,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> dict:
    recipient_email = str(recipient or "").strip()
    if not recipient_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No recipient email available for this client.")
    bcc_email = _workflow_bcc_email()
    subject = _workflow_email_subject(company_name, company_number, invoice_number)
    body = _workflow_email_body(company_name, company_number, invoice_number)
    if gmail_connection_for_user(user):
        await _send_workflow_email_gmail(user, recipient_email, subject, body, pdf_bytes, pdf_filename, bcc_email)
        return {"provider": "gmail", "recipient": recipient_email, "bccEmail": bcc_email}
    _send_workflow_email_smtp(recipient_email, subject, body, pdf_bytes, pdf_filename, bcc_email)
    return {"provider": "smtp", "recipient": recipient_email, "bccEmail": bcc_email}


async def bulk_raise_submission_invoices(user: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    company_ids = _chunk_company_ids(payload.get("companyIds") or [])
    if not company_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one company.")
    if len(company_ids) > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Maximum 500 companies per bulk invoice run.")

    settings_row = _ensure_settings_row()
    account_code = str(settings_row.get("xero_invoice_account_code") or "").strip()
    item_code = str(settings_row.get("xero_invoice_item_code") or "").strip()
    description = str(settings_row.get("xero_invoice_description") or "Companies House confirmation statement filing").strip()
    tax_type = str(settings_row.get("xero_invoice_tax_type") or "NONE").strip() or "NONE"
    configured_unit_amount = _coerce_settings_amount(
        settings_row.get("xero_invoice_unit_amount"),
        "xeroInvoiceUnitAmount",
    )
    preflight_errors: list[str] = []
    if not account_code:
        preflight_errors.append("Set a Xero sales account code in Companies House settings before raising invoices.")
    if configured_unit_amount <= Decimal("0.00"):
        preflight_errors.append("Set a non-zero default unit amount in Companies House settings before raising invoices.")
    if not description:
        preflight_errors.append("Set a line description in Companies House settings before raising invoices.")
    if preflight_errors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=" ".join(preflight_errors))

    connection_row = get_xero_connection_for_user(user["id"])
    user_id = user.get("id") if isinstance(user, dict) else None

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.id AS company_id,
                       c.company_number,
                       c.company_name,
                       c.client_id,
                       c.client_name,
                       c.contact_email,
                       s.id AS submission_id,
                       s.status AS submission_status,
                       s.submission_reference,
                       s.submitted_at,
                       s.completed_at AS submission_completed_at,
                       s.fee_amount,
                       s.xero_invoice_id
                FROM ch_companies c
                JOIN LATERAL (
                    SELECT *
                    FROM ch_submissions s
                    WHERE s.company_id = c.id
                    ORDER BY s.submitted_at DESC
                    LIMIT 1
                ) s ON TRUE
                WHERE c.id = ANY(%s)
                """,
                (company_ids,),
            )
            targets = cursor.fetchall() or []
        connection.commit()

    created: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    now = utcnow()
    invoice_date = now.date().isoformat()

    for row in targets:
        company_id = str(row.get("company_id") or "")
        submission_id = str(row.get("submission_id") or "")
        status_value = str(row.get("submission_status") or "")
        submitted_completed_at = row.get("submission_completed_at")
        company_name = row.get("company_name") or row.get("client_name") or "Client"
        company_number = row.get("company_number") or ""
        existing_invoice_id = row.get("xero_invoice_id") or ""

        if not submission_id:
            skipped.append({"companyId": company_id, "companyName": company_name, "companyNumber": company_number, "reason": "No submission found."})
            continue
        if status_value != "accepted":
            skipped.append({"companyId": company_id, "companyName": company_name, "companyNumber": company_number, "reason": f"Latest submission status is '{status_value}'."})
            continue
        if submitted_completed_at is None:
            skipped.append(
                {
                    "companyId": company_id,
                    "companyName": company_name,
                    "companyNumber": company_number,
                    "reason": "Latest submission has not completed delivery yet.",
                }
            )
            continue
        if existing_invoice_id:
            skipped.append({"companyId": company_id, "companyName": company_name, "companyNumber": company_number, "reason": f"Invoice already linked ({existing_invoice_id})."})
            continue

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM ch_companies WHERE id = %s", (company_id,))
                company_row = cursor.fetchone() or {}
                contact_row = _resolve_company_contact_for_invoice(cursor, company_row)
            connection.commit()

        xero_contact_id = str(contact_row.get("xero_contact_id") or "").strip()
        line_amount = Decimal(str(row.get("fee_amount") or 0)).quantize(Decimal("0.01"))
        if line_amount <= Decimal("0.00"):
            line_amount = configured_unit_amount

        line_item = {
            "Description": f"{description} ({company_number})" if company_number else description,
            "Quantity": 1,
            "UnitAmount": float(line_amount),
            "TaxType": tax_type,
        }
        if account_code:
            line_item["AccountCode"] = account_code
        if item_code:
            line_item["ItemCode"] = item_code

        contact_payload = {"ContactID": xero_contact_id} if xero_contact_id else {"Name": company_name}
        invoice_payload = {
            "Type": "ACCREC",
            "Contact": contact_payload,
            "Date": invoice_date,
            "DueDate": invoice_date,
            "Reference": f"CH CS filing {company_number}" if company_number else "CH CS filing",
            "Status": "AUTHORISED",
            "LineAmountTypes": "Exclusive",
            "LineItems": [line_item],
        }
        idempotency_seed = json.dumps(
            {"submissionId": submission_id, "companyId": company_id, "amount": str(line_amount)},
            sort_keys=True,
        )
        idempotency_key = f"ch-cs-invoice-{hashlib.sha256(idempotency_seed.encode()).hexdigest()[:32]}"

        try:
            xero_response = await create_sales_invoice(connection_row, invoice_payload, idempotency_key=idempotency_key)
            created_invoice = ((xero_response or {}).get("Invoices") or [{}])[0]
            xero_invoice_id = created_invoice.get("InvoiceID") or created_invoice.get("ID") or ""
            xero_invoice_number = created_invoice.get("InvoiceNumber") or ""
            if not xero_invoice_id:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Xero created an invoice for {company_name} but did not return InvoiceID.",
                )
        except HTTPException as exc:
            failed.append({
                "companyId": company_id,
                "companyName": company_name,
                "companyNumber": company_number,
                "submissionId": submission_id,
                "reason": str(exc.detail) if isinstance(exc.detail, str) else str((exc.detail or {}).get("message") or "Xero invoice creation failed."),
            })
            continue

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE ch_submissions
                    SET xero_invoice_id = %s,
                        updated_at = %s
                    WHERE id = %s
                    """,
                    (xero_invoice_id, now, submission_id),
                )
                cursor.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                    VALUES ('ch_submission', %s, 'xero_invoice_created', %s::jsonb, %s)
                    """,
                    (
                        submission_id,
                        json.dumps(
                            {
                                "companyId": company_id,
                                "companyNumber": company_number,
                                "xeroInvoiceId": xero_invoice_id,
                                "xeroInvoiceNumber": xero_invoice_number,
                            }
                        ),
                        user_id,
                    ),
                )
            connection.commit()

        email_result: dict | None = None
        email_error = ""
        recipient_email = str(company_row.get("contact_email") or contact_row.get("email") or "").strip()
        try:
            invoice_pdf_bytes = await fetch_invoice_pdf(connection_row, str(xero_invoice_id))
            invoice_pdf_filename = _workflow_invoice_pdf_filename(company_number, str(xero_invoice_number), company_name)
            email_result = await _send_confirmation_statement_invoice_email(
                user=user,
                recipient=recipient_email,
                company_name=company_name,
                company_number=company_number,
                invoice_number=str(xero_invoice_number),
                pdf_bytes=invoice_pdf_bytes,
                pdf_filename=invoice_pdf_filename,
            )
        except HTTPException as exc:
            email_error = str(exc.detail) if isinstance(exc.detail, str) else str((exc.detail or {}).get("message") or "Unable to send client email.")
        except Exception as exc:  # noqa: BLE001 - defensive guard for workflow completion.
            logger.exception("Unable to send confirmation statement workflow email for company %s", company_id)
            email_error = str(exc) or "Unable to send client email."

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                    VALUES ('ch_submission', %s, %s, %s::jsonb, %s)
                    """,
                    (
                        submission_id,
                        "client_invoice_email_sent" if not email_error else "client_invoice_email_failed",
                        json.dumps(
                            {
                                "companyId": company_id,
                                "companyNumber": company_number,
                                "xeroInvoiceId": xero_invoice_id,
                                "xeroInvoiceNumber": xero_invoice_number,
                                "recipient": (email_result or {}).get("recipient") or recipient_email,
                                "bccEmail": (email_result or {}).get("bccEmail") or _workflow_bcc_email(),
                                "provider": (email_result or {}).get("provider") or "",
                                "error": email_error,
                            }
                        ),
                        user_id,
                    ),
                )
            connection.commit()

        created.append({
            "companyId": company_id,
            "companyName": company_name,
            "companyNumber": company_number,
            "submissionId": submission_id,
            "xeroInvoiceId": xero_invoice_id,
            "xeroInvoiceNumber": xero_invoice_number,
            "amount": float(line_amount),
            "emailSent": not bool(email_error),
            "emailRecipient": (email_result or {}).get("recipient") or recipient_email,
            "emailBcc": (email_result or {}).get("bccEmail") or _workflow_bcc_email(),
            "emailProvider": (email_result or {}).get("provider") or "",
            "emailError": email_error,
        })

    found_company_ids = {str(row.get("company_id")) for row in targets if row.get("company_id")}
    for company_id in company_ids:
        if company_id in found_company_ids:
            continue
        skipped.append({"companyId": company_id, "companyName": "", "companyNumber": "", "reason": "Company not found or no submissions exist."})

    return {
        "createdCount": len(created),
        "skippedCount": len(skipped),
        "failedCount": len(failed),
        "created": created,
        "skipped": skipped,
        "failed": failed,
    }


def get_company_detail(company_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.*,
                       (a.id IS NOT NULL) AS auth_code_on_file,
                       a.code_hint AS auth_code_hint,
                       a.uploaded_at AS auth_code_uploaded_at
                FROM ch_companies c
                LEFT JOIN ch_auth_codes a ON a.company_id = c.id
                WHERE c.id = %s
                """,
                (company_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")
            cursor.execute(
                """
                SELECT s.*
                FROM ch_submissions s
                WHERE s.company_id = %s
                ORDER BY s.submitted_at DESC
                LIMIT 25
                """,
                (company_id,),
            )
            submissions = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT id, made_up_to_date, status, prepared_at, approved_at
                FROM ch_drafts
                WHERE company_id = %s
                ORDER BY created_at DESC
                LIMIT 25
                """,
                (company_id,),
            )
            drafts = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT created_at, event_type, payload, user_id
                FROM audit_events
                WHERE entity_type IN ('ch_company', 'ch_auth_code')
                  AND entity_id = %s
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (company_id,),
            )
            audit = cursor.fetchall() or []
        connection.commit()

    serialised = _serialise_company_row(row)
    serialised["submissions"] = [
        {
            "id": str(submission["id"]),
            "status": submission.get("status") or "",
            "submissionReference": submission.get("submission_reference") or "",
            "transactionId": submission.get("transaction_id") or "",
            "feeAmount": float(submission.get("fee_amount") or 0),
            "submittedAt": submission["submitted_at"].isoformat() if submission.get("submitted_at") else None,
            "rejectionReason": submission.get("rejection_reason") or "",
            "xeroInvoiceId": submission.get("xero_invoice_id") or "",
        }
        for submission in submissions
    ]
    serialised["drafts"] = [
        {
            "id": str(draft["id"]),
            "madeUpToDate": _date_or_none(draft.get("made_up_to_date")),
            "status": draft.get("status") or "draft",
            "preparedAt": draft["prepared_at"].isoformat() if draft.get("prepared_at") else None,
            "approvedAt": draft["approved_at"].isoformat() if draft.get("approved_at") else None,
        }
        for draft in drafts
    ]
    serialised["auditTrail"] = [
        {
            "at": event["created_at"].isoformat() if event.get("created_at") else None,
            "eventType": event.get("event_type") or "",
            "payload": event.get("payload") or {},
        }
        for event in audit
    ]
    return serialised


def update_company(company_id: str, payload: dict, user: dict) -> dict:
    user_id = user.get("id") if isinstance(user, dict) else None
    current_company = get_company_detail(company_id)
    current_company_number = normalise_company_number(current_company.get("companyNumber"))
    current_auth_code_on_file = bool(current_company.get("authCodeOnFile"))
    updates: dict[str, object] = {}
    json_columns: set[str] = set()
    auth_code_value = _coerce_text(payload.get("authCode"), 200) if "authCode" in payload else ""
    if auth_code_value:
        auth_code_value = re.sub(r"[^A-Z0-9]", "", auth_code_value.upper())
        if not re.fullmatch(r"[A-Z0-9]{6}", auth_code_value):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Authentication code must be 6 alphanumeric characters.",
            )

    if "internalStatus" in payload:
        internal_status = str(payload.get("internalStatus") or "").strip()
        if internal_status not in VALID_INTERNAL_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid internal status. Allowed: {', '.join(sorted(VALID_INTERNAL_STATUSES))}.",
            )
        updates["internal_status"] = internal_status
    if "assignedStaffName" in payload:
        updates["assigned_staff_name"] = _coerce_text(payload.get("assignedStaffName"), 200)
    if "notes" in payload:
        updates["notes"] = _coerce_text(payload.get("notes"), 4000)
    if "contactEmail" in payload:
        updates["contact_email"] = _coerce_text(payload.get("contactEmail"), 250)
    if "contactPhone" in payload:
        updates["contact_phone"] = _coerce_text(payload.get("contactPhone"), 80)
    if "clientAddress" in payload:
        updates["client_address"] = _coerce_text(payload.get("clientAddress"), 1000)
    if "clientName" in payload:
        updates["client_name"] = _coerce_text(payload.get("clientName"), 250)
    if "clientId" in payload:
        updates["client_id"] = _coerce_text(payload.get("clientId"), 80)
    if "filingAuthorityStatus" in payload:
        filing_authority_status = str(payload.get("filingAuthorityStatus") or "").strip().lower()
        if filing_authority_status not in VALID_FILING_AUTHORITY_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid filing authority status. Allowed: {', '.join(sorted(VALID_FILING_AUTHORITY_STATUSES))}.",
            )
        updates["filing_authority_status"] = filing_authority_status
    if "filingAuthorityReference" in payload:
        filing_reference = _coerce_text(payload.get("filingAuthorityReference"), 200)
        updates["filing_authority_reference"] = filing_reference
        if filing_reference and not auth_code_value:
            auth_code_value = filing_reference
    if "filingAuthorityReceivedAt" in payload:
        received_value = payload.get("filingAuthorityReceivedAt")
        if received_value in (None, ""):
            updates["filing_authority_received_at"] = None
        else:
            try:
                parsed = datetime.fromisoformat(str(received_value).replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filingAuthorityReceivedAt timestamp.") from exc
            updates["filing_authority_received_at"] = parsed
    if "filingAuthorityExpiresAt" in payload:
        expires_value = payload.get("filingAuthorityExpiresAt")
        if expires_value in (None, ""):
            updates["filing_authority_expires_at"] = None
        else:
            try:
                parsed = datetime.fromisoformat(str(expires_value).replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filingAuthorityExpiresAt timestamp.") from exc
            updates["filing_authority_expires_at"] = parsed

    if "sicCodes" in payload:
        raw_codes = payload.get("sicCodes")
        if isinstance(raw_codes, list):
            sic_candidates = [str(code or "").strip() for code in raw_codes]
        else:
            sic_candidates = re.split(r"[,\n;]+", str(raw_codes or ""))
        sic_codes: list[str] = []
        seen_codes: set[str] = set()
        for candidate in sic_candidates:
            code = re.sub(r"\s+", "", str(candidate or "").upper())
            if not code:
                continue
            if not re.fullmatch(r"[0-9A-Z]{4,8}", code):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid SIC code '{candidate}'. Use alphanumeric codes (4-8 characters).",
                )
            if code in seen_codes:
                continue
            seen_codes.add(code)
            sic_codes.append(code)
        updates["sic_codes"] = json.dumps(sic_codes[:20])
        json_columns.add("sic_codes")

    share_capital_patch: dict[str, object] = {}
    if "cs01Flags" in payload:
        raw_flags = payload.get("cs01Flags")
        if not isinstance(raw_flags, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'cs01Flags' must be an object.")
        normalised_flags: dict[str, bool] = {}
        for key in (
            "tradingOnMarket",
            "dtr5Applies",
            "pscExemptAsTradingOnRegulatedMarket",
            "pscExemptAsSharesAdmittedOnMarket",
            "pscExemptAsTradingOnUKRegulatedMarket",
        ):
            value = _first_bool_from_sources(raw_flags.get(key))
            if value is not None:
                normalised_flags[key] = value
        share_capital_patch["cs01Flags"] = normalised_flags
        share_capital_patch["confirmationStatement"] = normalised_flags

    if "statementOfCapital" in payload:
        raw_soc = payload.get("statementOfCapital")
        if raw_soc in (None, ""):
            share_capital_patch["statementOfCapital"] = {}
        elif not isinstance(raw_soc, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'statementOfCapital' must be an object.")
        else:
            normalised_soc: dict[str, str] = {}
            if raw_soc.get("totalNumberOfSharesIssued") not in (None, ""):
                normalised_soc["totalNumberOfSharesIssued"] = _ch_decimal_text(raw_soc.get("totalNumberOfSharesIssued"))
            if raw_soc.get("totalAggregateNominalValue") not in (None, ""):
                normalised_soc["totalAggregateNominalValue"] = _ch_decimal_text(raw_soc.get("totalAggregateNominalValue"))
            share_capital_patch["statementOfCapital"] = normalised_soc

    confirmation_statement_patch = (
        dict(share_capital_patch.get("confirmationStatement"))
        if isinstance(share_capital_patch.get("confirmationStatement"), dict)
        else {}
    )
    if "registeredEmailAddress" in payload:
        confirmation_statement_patch["registeredEmailAddress"] = _coerce_text(payload.get("registeredEmailAddress"), 320)
    if "lawfulPurposeStatement" in payload:
        lawful = _first_bool_from_sources(payload.get("lawfulPurposeStatement"))
        if lawful is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'lawfulPurposeStatement' must be true or false.")
        confirmation_statement_patch["acceptLawfulPurposeStatement"] = lawful
    if "stateConfirmation" in payload:
        state_confirmation = _first_bool_from_sources(payload.get("stateConfirmation"))
        if state_confirmation is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'stateConfirmation' must be true or false.")
        confirmation_statement_patch["stateConfirmation"] = state_confirmation
    if "reviewPeriodStart" in payload:
        value = payload.get("reviewPeriodStart")
        confirmation_statement_patch["reviewPeriodStart"] = _secretarial_parse_date(value, "reviewPeriodStart").isoformat() if value not in (None, "") else ""
    if "reviewPeriodEnd" in payload:
        value = payload.get("reviewPeriodEnd")
        confirmation_statement_patch["reviewPeriodEnd"] = _secretarial_parse_date(value, "reviewPeriodEnd").isoformat() if value not in (None, "") else ""
    if "identityVerification" in payload:
        identity_verification = payload.get("identityVerification")
        if identity_verification in (None, ""):
            confirmation_statement_patch["identityVerification"] = {}
        elif not isinstance(identity_verification, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'identityVerification' must be an object.")
        else:
            confirmation_statement_patch["identityVerification"] = {
                "required": bool(_first_bool_from_sources(identity_verification.get("required"))),
                "directorPersonalCodeSupplied": bool(_first_bool_from_sources(identity_verification.get("directorPersonalCodeSupplied"))),
                "verificationStatementGiven": bool(_first_bool_from_sources(identity_verification.get("verificationStatementGiven"),)),
                "relevantOfficer": _coerce_text(identity_verification.get("relevantOfficer"), 200),
            }
    if confirmation_statement_patch:
        share_capital_patch["confirmationStatement"] = confirmation_statement_patch

    if share_capital_patch:
        current_share_capital = current_company.get("shareCapital") if isinstance(current_company.get("shareCapital"), dict) else {}
        merged_share_capital = {**current_share_capital, **share_capital_patch}
        updates["share_capital"] = json.dumps(merged_share_capital)
        json_columns.add("share_capital")

    if "workflowReview" in payload:
        normalised_review = _normalise_workflow_review(payload.get("workflowReview"), user_id=user_id)
        updates["workflow_review"] = json.dumps(normalised_review)
        json_columns.add("workflow_review")

    if updates.get("internal_status") == "ready_to_file":
        if not _is_valid_company_number(current_company_number):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Cannot include this client in confirmation-statement filing workflow until a valid "
                    "Companies House company number is saved."
                ),
            )
        if not (current_auth_code_on_file or bool(auth_code_value)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Cannot include this client in confirmation-statement filing workflow until a valid "
                    "6-character Companies House authentication code is saved."
                ),
            )

    if not updates and not auth_code_value:
        return get_company_detail(company_id)

    set_clauses = ", ".join(
        f"{column} = %s::jsonb" if column in json_columns else f"{column} = %s"
        for column in updates
    )
    params = list(updates.values()) + [company_id]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            if updates:
                cursor.execute(
                    f"UPDATE ch_companies SET {set_clauses}, updated_at = NOW() WHERE id = %s RETURNING id",
                    params,
                )
                row = cursor.fetchone()
            else:
                cursor.execute("SELECT id FROM ch_companies WHERE id = %s", (company_id,))
                row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")
            if auth_code_value:
                _save_company_auth_code(cursor, company_id, auth_code_value, user_id)
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_company', %s, 'company_updated', %s::jsonb, %s)
                """,
                (
                    company_id,
                    json.dumps(
                        {
                            "fields": list(updates.keys()),
                            "authCodeUpdated": bool(auth_code_value),
                        }
                    ),
                    user_id,
                ),
            )
        connection.commit()

    return get_company_detail(company_id)


def delete_company(company_id: str, user: dict) -> dict:
    user_id = user.get("id") if isinstance(user, dict) else None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, company_number, company_name
                FROM ch_companies
                WHERE id = %s
                """,
                (company_id,),
            )
            existing = cursor.fetchone()
            if not existing:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")

            company_number = normalise_company_number(existing.get("company_number"))
            company_name = existing.get("company_name") or ""
            mapping_delete_count = 0
            if company_number:
                cursor.execute(
                    """
                    DELETE FROM xero_tenant_company_mappings
                    WHERE UPPER(TRIM(company_number)) = %s
                    """,
                    (company_number,),
                )
                mapping_delete_count = cursor.rowcount or 0

            cursor.execute("DELETE FROM ch_companies WHERE id = %s", (company_id,))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")

            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_company', %s, 'company_deleted', %s::jsonb, %s)
                """,
                (
                    company_id,
                    json.dumps(
                        {
                            "companyNumber": company_number,
                            "companyName": company_name,
                            "removedTenantMappings": mapping_delete_count,
                        }
                    ),
                    user_id,
                ),
            )
        connection.commit()
    return {
        "companyId": company_id,
        "companyNumber": company_number,
        "companyName": company_name,
        "removedTenantMappings": mapping_delete_count,
    }


def dashboard_summary() -> dict:
    today = date.today()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_companies,
                    SUM(CASE WHEN c.next_due_date IS NOT NULL AND c.next_due_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + INTERVAL '30 days') THEN 1 ELSE 0 END) AS due_soon,
                    SUM(
                        CASE WHEN c.next_due_date IS NOT NULL
                              AND c.next_due_date < CURRENT_DATE
                              AND (c.last_filed_date IS NULL OR c.last_filed_date < (CURRENT_DATE - INTERVAL '365 days'))
                        THEN 1 ELSE 0 END
                    ) AS overdue,
                    SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END) AS missing_auth,
                    SUM(CASE WHEN c.internal_status = 'ready_to_file' THEN 1 ELSE 0 END) AS ready_to_file,
                    SUM(CASE WHEN c.internal_status IN ('paused', 'do_not_file', 'inactive') THEN 1 ELSE 0 END) AS blocked,
                    MAX(c.last_synced_at) AS last_synced_at
                FROM ch_companies c
                LEFT JOIN ch_auth_codes a ON a.company_id = c.id
                """,
            )
            tile_row = cursor.fetchone() or {}
            cursor.execute(
                """
                SELECT next_due_date, last_filed_date, filing_history
                FROM ch_companies
                """
            )
            overdue_rows = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT s.*, c.company_name, c.company_number
                FROM ch_submissions s
                JOIN ch_companies c ON c.id = s.company_id
                ORDER BY s.submitted_at DESC
                LIMIT 10
                """,
            )
            recent_submissions = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT s.*, c.company_name, c.company_number
                FROM ch_submissions s
                JOIN ch_companies c ON c.id = s.company_id
                WHERE s.status = 'rejected'
                ORDER BY s.submitted_at DESC
                LIMIT 10
                """,
            )
            rejected_submissions = cursor.fetchall() or []
        connection.commit()

    overdue_count = 0
    for row in overdue_rows:
        next_due = row.get("next_due_date")
        if not isinstance(next_due, date) or next_due >= today:
            continue
        filing_history = row.get("filing_history") if isinstance(row.get("filing_history"), list) else []
        last_filed = _latest_confirmation_statement_filed_date(filing_history) if filing_history else row.get("last_filed_date")
        if isinstance(last_filed, date) and last_filed >= (today - timedelta(days=365)):
            continue
        overdue_count += 1

    return {
        "tiles": {
            "totalCompanies": int(tile_row.get("total_companies") or 0),
            "dueSoon": int(tile_row.get("due_soon") or 0),
            "overdue": overdue_count,
            "missingAuth": int(tile_row.get("missing_auth") or 0),
            "readyToFile": int(tile_row.get("ready_to_file") or 0),
            "blocked": int(tile_row.get("blocked") or 0),
            "lastSyncedAt": tile_row["last_synced_at"].isoformat() if tile_row.get("last_synced_at") else None,
        },
        "recentSubmissions": [
            {
                "id": str(s["id"]),
                "companyNumber": s.get("company_number") or "",
                "companyName": s.get("company_name") or "",
                "status": s.get("status") or "",
                "submittedAt": s["submitted_at"].isoformat() if s.get("submitted_at") else None,
                "rejectionReason": s.get("rejection_reason") or "",
            }
            for s in recent_submissions
        ],
        "rejectedSubmissions": [
            {
                "id": str(s["id"]),
                "companyNumber": s.get("company_number") or "",
                "companyName": s.get("company_name") or "",
                "status": s.get("status") or "",
                "submittedAt": s["submitted_at"].isoformat() if s.get("submitted_at") else None,
                "rejectionReason": s.get("rejection_reason") or "",
            }
            for s in rejected_submissions
        ],
    }


def list_imports(limit: int = 25) -> list[dict]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, import_type, filename, total_rows, created_count, updated_count, skipped_count,
                       error_count, status, created_at, completed_at
                FROM ch_imports
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (max(1, min(int(limit or 25), 200)),),
            )
            rows = cursor.fetchall() or []
        connection.commit()

    return [
        {
            "id": str(row["id"]),
            "importType": row.get("import_type") or "",
            "filename": row.get("filename") or "",
            "totalRows": int(row.get("total_rows") or 0),
            "createCount": int(row.get("created_count") or 0),
            "updateCount": int(row.get("updated_count") or 0),
            "skipCount": int(row.get("skipped_count") or 0),
            "errorCount": int(row.get("error_count") or 0),
            "status": row.get("status") or "",
            "createdAt": row["created_at"].isoformat() if row.get("created_at") else None,
            "completedAt": row["completed_at"].isoformat() if row.get("completed_at") else None,
        }
        for row in rows
    ]

def list_submission_attempts(limit: int = 200, company_id: str | None = None) -> list[dict]:
    limit_value = max(1, min(int(limit or 200), 1000))
    company_id_value = str(company_id or "").strip()

    with get_connection() as connection:
        with connection.cursor() as cursor:
            if company_id_value:
                cursor.execute(
                    """
                    SELECT s.*,
                           c.company_number,
                           c.company_name
                    FROM ch_submissions s
                    JOIN ch_companies c ON c.id = s.company_id
                    WHERE s.company_id = %s
                    ORDER BY s.submitted_at DESC NULLS LAST, s.created_at DESC
                    LIMIT %s
                    """,
                    (company_id_value, limit_value),
                )
            else:
                cursor.execute(
                    """
                    SELECT s.*,
                           c.company_number,
                           c.company_name
                    FROM ch_submissions s
                    JOIN ch_companies c ON c.id = s.company_id
                    ORDER BY s.submitted_at DESC NULLS LAST, s.created_at DESC
                    LIMIT %s
                    """,
                    (limit_value,),
                )
            rows = cursor.fetchall() or []
        connection.commit()

    return [
        {
            "id": str(row.get("id")) if row.get("id") else "",
            "companyId": str(row.get("company_id")) if row.get("company_id") else "",
            "companyNumber": row.get("company_number") or "",
            "companyName": row.get("company_name") or "",
            "attemptType": row.get("attempt_type") or "submit",
            "idempotencyKey": row.get("idempotency_key") or "",
            "status": row.get("status") or "",
            "submissionReference": row.get("submission_reference") or "",
            "transactionId": row.get("transaction_id") or "",
            "paymentReference": row.get("payment_reference") or "",
            "paymentConfirmed": bool(row.get("payment_confirmed")) if row.get("payment_confirmed") is not None else None,
            "paymentEvidence": row.get("payment_evidence") or {},
            "deadLetter": bool(row.get("dead_letter")) if row.get("dead_letter") is not None else False,
            "deadLetterReason": row.get("dead_letter_reason") or "",
            "retryCount": int(row.get("retry_count") or 0),
            "feeAmount": float(row.get("fee_amount") or 0),
            "rejectionReason": row.get("rejection_reason") or "",
            "xeroInvoiceId": row.get("xero_invoice_id") or "",
            "submittedAt": row.get("submitted_at").isoformat() if row.get("submitted_at") else None,
            "completedAt": row.get("completed_at").isoformat() if row.get("completed_at") else None,
            "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
            "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        }
        for row in rows
    ]


def submission_reconciliation_report(limit: int = 500) -> dict:
    rows = list_submission_attempts(limit=limit)
    totals = {
        "attempts": len(rows),
        "accepted": 0,
        "rejected": 0,
        "submitted": 0,
        "queued": 0,
        "paymentConfirmed": 0,
        "deadLetters": 0,
    }
    for row in rows:
        status_value = str(row.get("status") or "")
        if status_value in totals:
            totals[status_value] += 1
        if row.get("paymentConfirmed") is True:
            totals["paymentConfirmed"] += 1
        if row.get("deadLetter"):
            totals["deadLetters"] += 1
    return {"totals": totals, "attempts": rows}


def export_submission_attempts_csv(limit: int = 5000) -> str:
    rows = list_submission_attempts(limit=limit)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "submission_id",
            "company_id",
            "company_number",
            "company_name",
            "attempt_type",
            "status",
            "submitted_at",
            "completed_at",
            "submission_reference",
            "transaction_id",
            "payment_reference",
            "payment_confirmed",
            "fee_amount",
            "xero_invoice_id",
            "dead_letter",
            "dead_letter_reason",
            "rejection_reason",
            "retry_count",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.get("id") or "",
                row.get("companyId") or "",
                row.get("companyNumber") or "",
                row.get("companyName") or "",
                row.get("attemptType") or "",
                row.get("status") or "",
                row.get("submittedAt") or "",
                row.get("completedAt") or "",
                row.get("submissionReference") or "",
                row.get("transactionId") or "",
                row.get("paymentReference") or "",
                "true" if row.get("paymentConfirmed") is True else "false" if row.get("paymentConfirmed") is False else "",
                f"{float(row.get('feeAmount') or 0):.2f}",
                row.get("xeroInvoiceId") or "",
                "true" if row.get("deadLetter") else "false",
                row.get("deadLetterReason") or "",
                row.get("rejectionReason") or "",
                int(row.get("retryCount") or 0),
            ]
        )
    return output.getvalue()


def _support_report_text(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _xml_text(value)


def export_companies_house_support_report(
    limit: int = 50,
    status_filter: str = "rejected",
    company_id: str | None = None,
    submission_id: str | None = None,
) -> str:
    limit_value = max(1, min(int(limit or 50), 500))
    status_value = _xml_text(status_filter, "rejected").lower()
    if status_value not in {"rejected", "all"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Status filter must be 'rejected' or 'all'.",
        )

    settings_row = _ensure_settings_row()
    environment = _xml_text(settings_row.get("environment"), "sandbox")
    presenter_id = configured_presenter_id()
    presenter_auth = decrypt_presenter_auth()
    now = utcnow()

    company_id_value = _xml_text(company_id)
    submission_id_value = _xml_text(submission_id)
    where_clauses: list[str] = []
    query_params: list[object] = []
    if status_value == "rejected":
        where_clauses.append("s.status = 'rejected'")
    if company_id_value:
        where_clauses.append("s.company_id::text = %s")
        query_params.append(company_id_value)
    if submission_id_value:
        where_clauses.append("s.id::text = %s")
        query_params.append(submission_id_value)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT s.*,
                       c.company_number,
                       c.company_name,
                       c.client_name,
                       c.next_made_up_to_date,
                       c.next_due_date,
                       c.filing_authority_status,
                       c.filing_authority_reference,
                       c.filing_authority_expires_at,
                       c.contact_email,
                       a.code_hint AS auth_code_hint
                FROM ch_submissions s
                JOIN ch_companies c ON c.id = s.company_id
                LEFT JOIN ch_auth_codes a ON a.company_id = c.id
                {where_sql}
                ORDER BY s.submitted_at DESC NULLS LAST, s.created_at DESC
                LIMIT %s
                """,
                (*query_params, limit_value),
            )
            rows = cursor.fetchall() or []
        connection.commit()

    lines: list[str] = []
    lines.append("COMPANIES HOUSE SUPPORT REPORT")
    lines.append("Generated by: Credit Control Console")
    lines.append(f"Generated at (UTC): {now.isoformat()}")
    lines.append(f"Environment: {environment}")
    lines.append(f"Presenter ID: {presenter_id}")
    lines.append(f"Presenter Auth: {_mask(presenter_auth)}")
    lines.append(f"Credit Account Number: {_xml_text(settings_row.get('credit_account_number'))}")
    lines.append(f"API Key: {_mask(decrypt_api_key())}")
    lines.append(f"Rows Included: {len(rows)}")
    lines.append(f"Status Filter: {status_value}")
    lines.append("")
    lines.append("Include this report when emailing Companies House support.")
    lines.append("")

    for index, row in enumerate(rows, start=1):
        response_payload = row.get("response_payload") if isinstance(row.get("response_payload"), dict) else {}
        payment_evidence = row.get("payment_evidence") if isinstance(row.get("payment_evidence"), dict) else {}
        status_poll = response_payload.get("statusPoll") if isinstance(response_payload.get("statusPoll"), dict) else {}
        raw_response = _xml_text(response_payload.get("rawResponse")) or _xml_text(status_poll.get("rawResponse"))
        raw_response_excerpt = raw_response[:12000]
        truncated = len(raw_response) > 12000

        lines.append(f"----- Submission {index} -----")
        lines.append(f"Submission ID: {_xml_text(row.get('id'))}")
        lines.append(f"Company ID: {_xml_text(row.get('company_id'))}")
        lines.append(f"Company Name: {_xml_text(row.get('company_name'))}")
        lines.append(f"Company Number: {_xml_text(row.get('company_number'))}")
        lines.append(f"Client Name: {_xml_text(row.get('client_name'))}")
        lines.append(f"Contact Email: {_xml_text(row.get('contact_email'))}")
        lines.append(f"Company Auth Hint: {_xml_text(row.get('auth_code_hint'))}")
        lines.append(f"Submission Reference: {_xml_text(row.get('submission_reference'))}")
        lines.append(f"Transaction ID: {_xml_text(row.get('transaction_id'))}")
        lines.append(f"Attempt Type: {_xml_text(row.get('attempt_type'))}")
        lines.append(f"Status: {_xml_text(row.get('status'))}")
        lines.append(f"Submitted At: {_support_report_text(row.get('submitted_at'))}")
        lines.append(f"Completed At: {_support_report_text(row.get('completed_at'))}")
        lines.append(f"Made Up To Date: {_support_report_text(row.get('next_made_up_to_date'))}")
        lines.append(f"Due Date: {_support_report_text(row.get('next_due_date'))}")
        lines.append(f"Filing Authority Status: {_xml_text(row.get('filing_authority_status'))}")
        lines.append(f"Filing Authority Reference: {_xml_text(row.get('filing_authority_reference'))}")
        lines.append(f"Filing Authority Expires At: {_support_report_text(row.get('filing_authority_expires_at'))}")
        lines.append(f"Payment Reference: {_xml_text(row.get('payment_reference'))}")
        lines.append(f"Payment Confirmed: {_xml_text(row.get('payment_confirmed'))}")
        lines.append(f"Fee Amount: {_xml_text(row.get('fee_amount'))}")
        lines.append(f"Rejected Reason: {_xml_text(row.get('rejection_reason'))}")
        lines.append(f"Dead Letter: {_xml_text(row.get('dead_letter'))}")
        lines.append(f"Dead Letter Reason: {_xml_text(row.get('dead_letter_reason'))}")
        lines.append("Gateway Statuses JSON:")
        lines.append(json.dumps(response_payload.get("gatewayStatuses") or [], default=str, indent=2, sort_keys=True))
        lines.append("Gateway Errors JSON:")
        lines.append(json.dumps(response_payload.get("gatewayErrors") or [], default=str, indent=2, sort_keys=True))
        lines.append("Payment Evidence JSON:")
        lines.append(json.dumps(payment_evidence, default=str, indent=2, sort_keys=True))
        lines.append("Raw Gateway Response Excerpt:")
        lines.append(raw_response_excerpt or "")
        if truncated:
            lines.append("[Raw response truncated to first 12000 characters]")
        lines.append("")

    if not rows:
        lines.append("No submission rows matched this filter.")

    return "\n".join(lines).strip() + "\n"

def list_dead_letters(limit: int = 200) -> list[dict]:
    limit_value = max(1, min(int(limit or 200), 1000))
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.*,
                       c.company_number,
                       c.company_name
                FROM ch_dead_letters d
                LEFT JOIN ch_companies c ON c.id = d.company_id
                ORDER BY d.created_at DESC
                LIMIT %s
                """,
                (limit_value,),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return [
        {
            "id": str(row.get("id")) if row.get("id") else "",
            "submissionId": str(row.get("submission_id")) if row.get("submission_id") else "",
            "companyId": str(row.get("company_id")) if row.get("company_id") else "",
            "companyNumber": row.get("company_number") or "",
            "companyName": row.get("company_name") or "",
            "workflow": row.get("workflow") or "",
            "stage": row.get("stage") or "",
            "reason": row.get("reason") or "",
            "payload": row.get("payload") or {},
            "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
        }
        for row in rows
    ]


def replay_dead_letter_submissions(user: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    dead_letter_ids = [str(value or "").strip() for value in (payload.get("deadLetterIds") or []) if str(value or "").strip()]
    company_ids = [str(value or "").strip() for value in (payload.get("companyIds") or []) if str(value or "").strip()]
    if dead_letter_ids:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT company_id
                    FROM ch_dead_letters
                    WHERE id = ANY(%s)
                    """,
                    (dead_letter_ids,),
                )
                rows = cursor.fetchall() or []
            connection.commit()
        company_ids.extend(str(row.get("company_id") or "").strip() for row in rows if row.get("company_id"))
    deduped_company_ids = sorted({company_id for company_id in company_ids if company_id})
    if not deduped_company_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one dead-letter company to replay.")
    result = bulk_submit_confirmation_statements(user, {"companyIds": deduped_company_ids})
    user_id = user.get("id") if isinstance(user, dict) else None
    record_audit_event(
        entity_type="ch_dead_letter",
        entity_id="replay",
        event_type="dead_letter_replay_requested",
        user_id=user_id,
        payload={"companyIds": deduped_company_ids, "deadLetterIds": dead_letter_ids},
    )
    return result
