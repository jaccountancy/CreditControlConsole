import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

import httpx
from fastapi import HTTPException, status

from .config import get_settings
from .database import get_connection, utcnow
from .security import decrypt_secret, encrypt_secret
from .services import get_xero_connection_for_user
from .xero import create_sales_invoice

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

CLIENT_IMPORT_HEADER_ALIASES = {
    "client_name": {"client name", "client", "customer", "customer name"},
    "client_id": {"client id", "client reference", "client ref", "reference", "ref", "bm id", "bm client id"},
    "company_name": {"company name", "company", "registered name", "limited company", "ltd name"},
    "company_number": {"company number", "company no", "company no.", "crn", "registration number", "companies house number", "ch number"},
    "auth_code": {"authentication code", "auth code", "auth", "ch auth code", "companies house authentication code"},
    "contact_email": {"contact email", "email", "primary email", "email address"},
    "contact_phone": {"contact phone", "phone", "telephone", "phone number"},
    "assigned_staff": {"assigned staff", "assigned staff member", "staff", "owner", "manager", "account manager"},
    "notes": {"notes", "note", "internal notes", "comment"},
    "company_type": {"company type", "type", "legal type", "entity type"},
    "period_end": {"confirmation statement period end", "statement period end", "made up to", "period end", "confirmation period end"},
    "period_start": {"confirmation statement period start", "statement period start", "period start"},
    "due_date": {"due date", "next due date", "confirmation due date", "next confirmation due"},
    "manager_reference": {"client manager", "manager reference", "relationship manager", "manager ref", "portfolio manager"},
}

COMPANY_NUMBER_RE = re.compile(r"^[A-Z0-9]{1,2}\d{6,}$|^\d{8}$|^[A-Z]{2}\d{6}$")


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


def _normalise_header(header: str) -> str:
    return re.sub(r"\s+", " ", str(header or "").strip().lower())


def _resolve_header_map(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lowered = [_normalise_header(header) for header in headers]
    for canonical, aliases in CLIENT_IMPORT_HEADER_ALIASES.items():
        for index, header in enumerate(lowered):
            if not header:
                continue
            if header == canonical.replace("_", " ") or header in aliases:
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
        if response.is_error:
            return current_map
        payload = response.json()
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


def _looks_private_limited(row_payload: dict) -> bool:
    company_type = str(row_payload.get("company_type") or "").strip().lower()
    company_name = str(row_payload.get("company_name") or row_payload.get("client_name") or "").strip().lower()
    combined = f"{company_type} {company_name}".strip()
    exclude_terms = ("sole trader", "self employed", "self-employed", "individual", "partnership", "llp")
    if any(term in combined for term in exclude_terms):
        return False
    if "private limited" in combined:
        return True
    if re.search(r"\bltd\b", company_name) or "limited" in company_name:
        return True
    if "limited company" in company_type or "ltd company" in company_type:
        return True
    return False


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
                "SELECT * FROM ch_companies WHERE company_number = ANY(%s)",
                (numbers,),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return {row["company_number"]: row for row in rows}


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

    parsed_rows: list[dict] = []
    errors: list[dict] = []
    seen_numbers: set[str] = set()
    duplicate_numbers: set[str] = set()

    for index, raw_row in enumerate(rows, start=2):
        row_payload: dict[str, str] = {}
        for canonical, column_index in column_map.items():
            value = raw_row[column_index] if column_index < len(raw_row) else ""
            row_payload[canonical] = _coerce_text(value, 2000 if canonical == "notes" else 250)
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

    valid_numbers = [row["data"]["company_number"] for row in parsed_rows if row["data"]["company_number"] and not row["errors"]]
    existing = _existing_companies_by_number(valid_numbers)

    create_count = 0
    update_count = 0
    skip_count = 0
    error_count = 0
    auth_codes_in_file = 0
    selected_count = 0
    excluded_non_ltd_count = 0

    for row in parsed_rows:
        data = row["data"]
        company_number = data["company_number"]
        if company_number and company_number in duplicate_numbers and not row["errors"]:
            row["errors"].append("Duplicate company number within this file.")
        if data.get("auth_code"):
            auth_codes_in_file += 1
        row["included"] = _looks_private_limited(data)
        if row["errors"]:
            error_count += 1
            row["action"] = "error"
            continue
        if not row["included"]:
            excluded_non_ltd_count += 1
            row["action"] = "skip"
            continue
        if company_number in existing:
            update_count += 1
            row["action"] = "update"
            row["existingCompany"] = {
                "id": str(existing[company_number]["id"]),
                "companyName": existing[company_number].get("company_name") or "",
            }
        else:
            create_count += 1
            row["action"] = "create"
        selected_count += 1

    if not parsed_rows:
        skip_count = 0
    visible_rows = [row for row in parsed_rows if row.get("included") or row.get("errors")]

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
            assigned_staff_name,
            notes,
            next_made_up_to_date,
            next_due_date
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULLIF(%s, '')::date, NULLIF(%s, '')::date)
        ON CONFLICT (company_number) DO UPDATE
        SET company_name = COALESCE(NULLIF(EXCLUDED.company_name, ''), ch_companies.company_name),
            client_id = COALESCE(NULLIF(EXCLUDED.client_id, ''), ch_companies.client_id),
            client_name = COALESCE(NULLIF(EXCLUDED.client_name, ''), ch_companies.client_name),
            contact_email = COALESCE(NULLIF(EXCLUDED.contact_email, ''), ch_companies.contact_email),
            contact_phone = COALESCE(NULLIF(EXCLUDED.contact_phone, ''), ch_companies.contact_phone),
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


def _serialise_company_row(row: dict, *, include_auth: bool = True) -> dict:
    today = date.today()
    next_due = row.get("next_due_date")
    if isinstance(next_due, date):
        due_in_days = (next_due - today).days
    else:
        due_in_days = None
    latest_submission_status = row.get("latest_submission_status") or ""
    latest_submission_invoice_id = row.get("latest_submission_xero_invoice_id") or ""
    internal_status = row.get("internal_status") or "active"
    blocked_internal = internal_status in {"paused", "do_not_file", "inactive"}
    has_due_date = isinstance(next_due, date)
    has_auth = bool(row.get("auth_code_on_file"))
    eligible_for_submission = bool(
        has_due_date
        and not blocked_internal
        and has_auth
        and (due_in_days is None or due_in_days <= 60)
    )
    eligible_for_invoicing = bool(
        latest_submission_status in {"submitted", "accepted"}
        and not latest_submission_invoice_id
    )
    return {
        "id": str(row.get("id")) if row.get("id") else None,
        "companyNumber": row.get("company_number") or "",
        "companyName": row.get("company_name") or "",
        "clientId": row.get("client_id") or "",
        "clientName": row.get("client_name") or "",
        "contactEmail": row.get("contact_email") or "",
        "contactPhone": row.get("contact_phone") or "",
        "assignedStaffName": row.get("assigned_staff_name") or "",
        "registeredOffice": row.get("registered_office") or "",
        "companyStatus": row.get("company_status") or "",
        "incorporationDate": _date_or_none(row.get("incorporation_date")),
        "sicCodes": row.get("sic_codes") or [],
        "officers": row.get("officers") or [],
        "pscs": row.get("pscs") or [],
        "shareCapital": row.get("share_capital") or {},
        "nextMadeUpToDate": _date_or_none(row.get("next_made_up_to_date")),
        "nextDueDate": _date_or_none(row.get("next_due_date")),
        "lastFiledDate": _date_or_none(row.get("last_filed_date")),
        "filingHistory": row.get("filing_history") or [],
        "internalStatus": row.get("internal_status") or "active",
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
        "dueInDays": due_in_days,
        "eligibleForSubmission": eligible_for_submission,
        "eligibleForInvoicing": eligible_for_invoicing,
    }


def list_companies(filters: dict | None = None) -> list[dict]:
    filters = filters or {}
    where_clauses: list[str] = []
    params: list = []

    search = (filters.get("search") or "").strip().lower()
    if search:
        where_clauses.append(
            "(LOWER(c.company_name) LIKE %s OR LOWER(c.client_name) LIKE %s OR LOWER(c.company_number) LIKE %s)"
        )
        like = f"%{search}%"
        params.extend([like, like, like])

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
               latest.xero_invoice_id AS latest_submission_xero_invoice_id
        FROM ch_companies c
        LEFT JOIN ch_auth_codes a ON a.company_id = c.id
        LEFT JOIN LATERAL (
            SELECT s.id, s.status, s.submitted_at, s.submission_reference, s.xero_invoice_id
            FROM ch_submissions s
            WHERE s.company_id = c.id
            ORDER BY s.submitted_at DESC
            LIMIT 1
        ) latest ON TRUE
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

    return [_serialise_company_row(row) for row in rows]


def _chunk_company_ids(company_ids: list[str]) -> list[str]:
    normalised = [str(company_id or "").strip() for company_id in company_ids]
    return [company_id for company_id in normalised if company_id]


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


def bulk_submit_confirmation_statements(user: dict, payload: dict | None = None) -> dict:
    payload = payload or {}
    company_ids = _chunk_company_ids(payload.get("companyIds") or [])
    if not company_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one company.")
    if len(company_ids) > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Maximum 500 companies per bulk submission.")

    user_id = user.get("id") if isinstance(user, dict) else None
    companies = _resolve_submission_candidates(company_ids)
    found_ids = {str(row.get("id")) for row in companies if row.get("id")}
    missing_ids = [company_id for company_id in company_ids if company_id not in found_ids]

    submitted: list[dict] = []
    skipped: list[dict] = []
    now = utcnow()
    today = date.today()

    for row in companies:
        company_id = str(row.get("id") or "")
        company_number = row.get("company_number") or ""
        internal_status = row.get("internal_status") or "active"
        next_due = row.get("next_due_date")
        has_auth = bool(row.get("auth_code_on_file"))
        due_in_days = (next_due - today).days if isinstance(next_due, date) else None
        if internal_status in {"paused", "do_not_file", "inactive"}:
            skipped.append({
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": row.get("company_name") or "",
                "reason": f"Internal status is '{internal_status}'.",
            })
            continue
        if not has_auth:
            skipped.append({
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": row.get("company_name") or "",
                "reason": "Missing Companies House authentication code.",
            })
            continue
        if due_in_days is not None and due_in_days > 60:
            skipped.append({
                "companyId": company_id,
                "companyNumber": company_number,
                "companyName": row.get("company_name") or "",
                "reason": f"Due date is outside workflow window ({due_in_days} days).",
            })
            continue

        submission_reference = f"CS-{company_number or company_id[:8].upper()}-{now.strftime('%Y%m%d%H%M%S')}"
        transaction_id = f"txn-{uuid4().hex[:20]}"
        fee_amount = Decimal("0.00")
        status_value = "submitted"
        response_payload = {
            "queuedAt": now.isoformat(),
            "source": "bulk_workflow",
            "mode": "workflow",
            "companyNumber": company_number,
        }

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ch_submissions (
                        company_id,
                        submission_reference,
                        transaction_id,
                        fee_amount,
                        status,
                        response_payload,
                        submitted_by_user_id,
                        submitted_at,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        company_id,
                        submission_reference,
                        transaction_id,
                        fee_amount,
                        status_value,
                        json.dumps(response_payload),
                        user_id,
                        now,
                        now,
                        now,
                    ),
                )
                submission_id = str(cursor.fetchone()["id"])
                cursor.execute(
                    """
                    INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                    VALUES ('ch_submission', %s, 'bulk_submission_queued', %s::jsonb, %s)
                    """,
                    (
                        submission_id,
                        json.dumps({
                            "companyId": company_id,
                            "companyNumber": company_number,
                            "workflow": "confirmation_statement_bulk",
                        }),
                        user_id,
                    ),
                )
            connection.commit()

        submitted.append({
            "submissionId": submission_id,
            "companyId": company_id,
            "companyName": row.get("company_name") or "",
            "companyNumber": company_number,
            "status": status_value,
            "submittedAt": now.isoformat(),
            "submissionReference": submission_reference,
            "transactionId": transaction_id,
        })

    for missing_id in missing_ids:
        skipped.append({
            "companyId": missing_id,
            "companyNumber": "",
            "companyName": "",
            "reason": "Company not found.",
        })

    return {
        "submittedCount": len(submitted),
        "skippedCount": len(skipped),
        "submitted": submitted,
        "skipped": skipped,
    }


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
    configured_unit_amount = Decimal(str(settings_row.get("xero_invoice_unit_amount") or 0)).quantize(Decimal("0.01"))
    if configured_unit_amount <= Decimal("0.00"):
        configured_unit_amount = Decimal("13.00")

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
        company_name = row.get("company_name") or row.get("client_name") or "Client"
        company_number = row.get("company_number") or ""
        existing_invoice_id = row.get("xero_invoice_id") or ""

        if not submission_id:
            skipped.append({"companyId": company_id, "companyName": company_name, "companyNumber": company_number, "reason": "No submission found."})
            continue
        if status_value not in {"submitted", "accepted"}:
            skipped.append({"companyId": company_id, "companyName": company_name, "companyNumber": company_number, "reason": f"Latest submission status is '{status_value}'."})
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

        created.append({
            "companyId": company_id,
            "companyName": company_name,
            "companyNumber": company_number,
            "submissionId": submission_id,
            "xeroInvoiceId": xero_invoice_id,
            "xeroInvoiceNumber": xero_invoice_number,
            "amount": float(line_amount),
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
    updates: dict[str, object] = {}

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
    if "clientName" in payload:
        updates["client_name"] = _coerce_text(payload.get("clientName"), 250)
    if "clientId" in payload:
        updates["client_id"] = _coerce_text(payload.get("clientId"), 80)

    if not updates:
        return get_company_detail(company_id)

    set_clauses = ", ".join(f"{column} = %s" for column in updates)
    params = list(updates.values()) + [company_id]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE ch_companies SET {set_clauses}, updated_at = NOW() WHERE id = %s RETURNING id",
                params,
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found.")
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES ('ch_company', %s, 'company_updated', %s::jsonb, %s)
                """,
                (company_id, json.dumps({"fields": list(updates.keys())}), user_id),
            )
        connection.commit()

    return get_company_detail(company_id)


def dashboard_summary() -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_companies,
                    SUM(CASE WHEN c.next_due_date IS NOT NULL AND c.next_due_date BETWEEN CURRENT_DATE AND (CURRENT_DATE + INTERVAL '30 days') THEN 1 ELSE 0 END) AS due_soon,
                    SUM(CASE WHEN c.next_due_date IS NOT NULL AND c.next_due_date < CURRENT_DATE THEN 1 ELSE 0 END) AS overdue,
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

    return {
        "tiles": {
            "totalCompanies": int(tile_row.get("total_companies") or 0),
            "dueSoon": int(tile_row.get("due_soon") or 0),
            "overdue": int(tile_row.get("overdue") or 0),
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
