import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
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
    column_map = _resolve_header_map(headers)
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
        company_number = normalise_company_number(row_payload.get("company_number"))
        row_payload["company_number"] = company_number

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
        })

    valid_numbers = [row["data"]["company_number"] for row in parsed_rows if row["data"]["company_number"] and not row["errors"]]
    existing = _existing_companies_by_number(valid_numbers)

    create_count = 0
    update_count = 0
    skip_count = 0
    error_count = 0
    auth_codes_in_file = 0

    for row in parsed_rows:
        data = row["data"]
        company_number = data["company_number"]
        if company_number and company_number in duplicate_numbers and not row["errors"]:
            row["errors"].append("Duplicate company number within this file.")
        if data.get("auth_code"):
            auth_codes_in_file += 1
        if row["errors"]:
            error_count += 1
            row["action"] = "error"
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

    if not parsed_rows:
        skip_count = 0

    return {
        "filename": filename,
        "totalRows": len(parsed_rows),
        "createCount": create_count,
        "updateCount": update_count,
        "skipCount": skip_count,
        "errorCount": error_count,
        "authCodesInFile": auth_codes_in_file,
        "rows": parsed_rows,
        "headers": headers,
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
            notes
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (company_number) DO UPDATE
        SET company_name = COALESCE(NULLIF(EXCLUDED.company_name, ''), ch_companies.company_name),
            client_id = COALESCE(NULLIF(EXCLUDED.client_id, ''), ch_companies.client_id),
            client_name = COALESCE(NULLIF(EXCLUDED.client_name, ''), ch_companies.client_name),
            contact_email = COALESCE(NULLIF(EXCLUDED.contact_email, ''), ch_companies.contact_email),
            contact_phone = COALESCE(NULLIF(EXCLUDED.contact_phone, ''), ch_companies.contact_phone),
            assigned_staff_name = COALESCE(NULLIF(EXCLUDED.assigned_staff_name, ''), ch_companies.assigned_staff_name),
            notes = COALESCE(NULLIF(EXCLUDED.notes, ''), ch_companies.notes),
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
               a.uploaded_at AS auth_code_uploaded_at
        FROM ch_companies c
        LEFT JOIN ch_auth_codes a ON a.company_id = c.id
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
