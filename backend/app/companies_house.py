from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow
from .security import decrypt_secret, encrypt_secret

CH_API_KEY_LABEL = "ch:api_key"
CH_PRESENTER_AUTH_LABEL = "ch:presenter_auth"
CH_COMPANY_AUTH_LABEL = "ch:company_auth"

VALID_ENVIRONMENTS = {"sandbox", "production"}
MASK_VISIBLE_CHARS = 4


def _mask(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= MASK_VISIBLE_CHARS:
        return "•" * len(text)
    return "•" * (len(text) - MASK_VISIBLE_CHARS) + text[-MASK_VISIBLE_CHARS:]


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
    return {
        "environment": environment,
        "apiBaseUrl": api_base,
        "apiKeyHint": row.get("api_key_hint") or "",
        "apiKeyConfigured": bool(row.get("api_key_encrypted")),
        "presenterId": row.get("presenter_id") or "",
        "presenterAuthHint": row.get("presenter_auth_hint") or "",
        "presenterAuthConfigured": bool(row.get("presenter_auth_encrypted")),
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

    new_api_key = payload.get("apiKey")
    if new_api_key is None:
        api_key_encrypted = existing.get("api_key_encrypted") or ""
        api_key_hint = existing.get("api_key_hint") or ""
    else:
        api_key_value = str(new_api_key).strip()
        if api_key_value:
            api_key_encrypted = encrypt_secret(api_key_value, CH_API_KEY_LABEL)
            api_key_hint = _mask(api_key_value)
        else:
            api_key_encrypted = ""
            api_key_hint = ""

    new_presenter_auth = payload.get("presenterAuth")
    if new_presenter_auth is None:
        presenter_auth_encrypted = existing.get("presenter_auth_encrypted") or ""
        presenter_auth_hint = existing.get("presenter_auth_hint") or ""
    else:
        presenter_auth_value = str(new_presenter_auth).strip()
        if presenter_auth_value:
            presenter_auth_encrypted = encrypt_secret(presenter_auth_value, CH_PRESENTER_AUTH_LABEL)
            presenter_auth_hint = _mask(presenter_auth_value)
        else:
            presenter_auth_encrypted = ""
            presenter_auth_hint = ""

    presenter_id = str(payload.get("presenterId") or "").strip()
    credit_account_number = str(payload.get("creditAccountNumber") or "").strip()
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
            "apiKeyChanged": new_api_key is not None,
            "presenterAuthChanged": new_presenter_auth is not None,
        },
    )

    return _serialise(updated)


def decrypt_api_key() -> str:
    row = _load_settings_row()
    if row is None or not row.get("api_key_encrypted"):
        settings = get_settings()
        return (settings.companies_house_api_key or "").strip()
    return decrypt_secret(row["api_key_encrypted"], CH_API_KEY_LABEL)


def decrypt_presenter_auth() -> str:
    row = _load_settings_row()
    if row is None or not row.get("presenter_auth_encrypted"):
        settings = get_settings()
        return (settings.companies_house_presenter_auth or "").strip()
    return decrypt_secret(row["presenter_auth_encrypted"], CH_PRESENTER_AUTH_LABEL)
