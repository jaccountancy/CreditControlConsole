import json
import csv
import io
from datetime import date, datetime, timedelta

from fastapi import HTTPException, status

from .database import get_connection, utcnow

VALID_STATUSES = {
    "draft",
    "prepared",
    "submitted",
    "awaiting_code",
    "code_received",
    "authorised",
    "rejected",
    "cancelled",
}
VALID_CHANNELS = {"online", "paper"}


def _text(value, limit: int = 3000) -> str:
    return str(value or "").strip()[:limit]


def _normalise_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _normalise_status(value: str | None, default: str = "draft") -> str:
    candidate = _text(value, 40).lower() or default
    if candidate not in VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid HMRC 64-8 status. Allowed: {', '.join(sorted(VALID_STATUSES))}.",
        )
    return candidate


def _normalise_channel(value: str | None, default: str = "online") -> str:
    candidate = _text(value, 20).lower() or default
    if candidate not in VALID_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid submission channel. Allowed: {', '.join(sorted(VALID_CHANNELS))}.",
        )
    return candidate


def _parse_date_or_none(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value, 40)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Expected a valid ISO date.") from exc


def _parse_datetime_or_none(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = _text(value, 80)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Expected a valid ISO datetime.") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=utcnow().tzinfo)
    return parsed


def _serialise_request(row: dict) -> dict:
    services = []
    if row.get("include_sa"):
        services.append("SA")
    if row.get("include_paye"):
        services.append("PAYE")
    if row.get("include_ct"):
        services.append("CT")
    return {
        "id": str(row.get("id") or ""),
        "clientId": row.get("client_id") or "",
        "clientName": row.get("client_name") or "",
        "clientManager": row.get("client_manager") or "",
        "clientContactName": row.get("client_contact_name") or "",
        "clientContactEmail": row.get("client_contact_email") or "",
        "clientContactPhone": row.get("client_contact_phone") or "",
        "postalAddress": row.get("postal_address") or "",
        "saUtr": row.get("sa_utr") or "",
        "ctUtr": row.get("ct_utr") or "",
        "payeReference": row.get("paye_reference") or "",
        "companyNumber": row.get("company_number") or "",
        "includeSa": bool(row.get("include_sa")),
        "includePaye": bool(row.get("include_paye")),
        "includeCt": bool(row.get("include_ct")),
        "services": services,
        "status": row.get("status") or "draft",
        "submissionChannel": row.get("submission_channel") or "online",
        "hmrcSubmissionReference": row.get("hmrc_submission_reference") or "",
        "submittedAt": row.get("submitted_at").isoformat() if row.get("submitted_at") else "",
        "expectedCodeBy": row.get("expected_code_by").isoformat() if row.get("expected_code_by") else "",
        "reminderCount": int(row.get("reminder_count") or 0),
        "lastReminderAt": row.get("last_reminder_at").isoformat() if row.get("last_reminder_at") else "",
        "authorityCode": row.get("authority_code") or "",
        "authorityCodeReceivedAt": row.get("authority_code_received_at").isoformat() if row.get("authority_code_received_at") else "",
        "authorityActivatedAt": row.get("authority_activated_at").isoformat() if row.get("authority_activated_at") else "",
        "notes": row.get("notes") or "",
        "evidenceLinks": row.get("evidence_links") if isinstance(row.get("evidence_links"), list) else [],
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else "",
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else "",
    }


def _record_audit_event(entity_type: str, entity_id: str, event_type: str, payload: dict, user_id: str | None) -> None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (entity_type, entity_id, event_type, json.dumps(payload, default=str), user_id),
            )
        connection.commit()


def hmrc_64_8_payload(user: dict) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM hmrc_64_8_requests
                WHERE created_by_user_id = %s
                ORDER BY created_at DESC
                LIMIT 1000
                """,
                (user["id"],),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    requests = [_serialise_request(row) for row in rows]
    now = date.today()
    summary = {
        "total": len(requests),
        "draft": 0,
        "awaitingCode": 0,
        "authorised": 0,
        "overdueCode": 0,
    }
    for row in requests:
        status_value = str(row.get("status") or "draft").lower()
        if status_value == "draft":
            summary["draft"] += 1
        if status_value in {"submitted", "awaiting_code", "code_received"}:
            summary["awaitingCode"] += 1
        if status_value == "authorised":
            summary["authorised"] += 1
        expected = row.get("expectedCodeBy")
        if expected and status_value not in {"authorised", "cancelled", "rejected"}:
            try:
                expected_date = date.fromisoformat(expected)
            except ValueError:
                continue
            if expected_date < now:
                summary["overdueCode"] += 1
    return {"requests": requests, "summary": summary}


def hmrc_64_8_export_csv(user: dict) -> str:
    payload = hmrc_64_8_payload(user)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "request_id",
            "client_id",
            "client_name",
            "client_manager",
            "services",
            "status",
            "submission_channel",
            "hmrc_submission_reference",
            "submitted_at",
            "expected_code_by",
            "reminder_count",
            "last_reminder_at",
            "authority_code_received_at",
            "authority_activated_at",
            "created_at",
            "updated_at",
        ]
    )
    for row in payload.get("requests") or []:
        writer.writerow(
            [
                row.get("id") or "",
                row.get("clientId") or "",
                row.get("clientName") or "",
                row.get("clientManager") or "",
                ",".join(row.get("services") or []),
                row.get("status") or "",
                row.get("submissionChannel") or "",
                row.get("hmrcSubmissionReference") or "",
                row.get("submittedAt") or "",
                row.get("expectedCodeBy") or "",
                row.get("reminderCount") or 0,
                row.get("lastReminderAt") or "",
                row.get("authorityCodeReceivedAt") or "",
                row.get("authorityActivatedAt") or "",
                row.get("createdAt") or "",
                row.get("updatedAt") or "",
            ]
        )
    return output.getvalue()


def hmrc_64_8_history(user: dict, limit: int = 500) -> dict:
    limit_value = max(1, min(int(limit or 500), 2000))
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    audit_events.id,
                    audit_events.entity_id,
                    audit_events.event_type,
                    audit_events.payload,
                    audit_events.created_at,
                    hmrc_64_8_requests.client_name,
                    hmrc_64_8_requests.client_id,
                    hmrc_64_8_requests.status
                FROM audit_events
                JOIN hmrc_64_8_requests
                  ON hmrc_64_8_requests.id::text = audit_events.entity_id
                WHERE audit_events.entity_type = 'hmrc_64_8_request'
                  AND hmrc_64_8_requests.created_by_user_id = %s
                ORDER BY audit_events.created_at DESC
                LIMIT %s
                """,
                (user["id"], limit_value),
            )
            audit_rows = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT
                    client_id,
                    client_name,
                    COUNT(*) AS requests_sent,
                    MAX(submitted_at) AS latest_submitted_at
                FROM hmrc_64_8_requests
                WHERE created_by_user_id = %s
                  AND submitted_at IS NOT NULL
                GROUP BY client_id, client_name
                ORDER BY MAX(submitted_at) DESC
                """,
                (user["id"],),
            )
            sent_rows = cursor.fetchall() or []
        connection.commit()
    history = [
        {
            "id": str(row.get("id") or ""),
            "requestId": str(row.get("entity_id") or ""),
            "eventType": row.get("event_type") or "",
            "payload": row.get("payload") if isinstance(row.get("payload"), dict) else {},
            "createdAt": row.get("created_at").isoformat() if row.get("created_at") else "",
            "clientId": row.get("client_id") or "",
            "clientName": row.get("client_name") or "",
            "requestStatus": row.get("status") or "",
        }
        for row in audit_rows
    ]
    sent_clients = [
        {
            "clientId": row.get("client_id") or "",
            "clientName": row.get("client_name") or "",
            "requestsSent": int(row.get("requests_sent") or 0),
            "latestSubmittedAt": row.get("latest_submitted_at").isoformat() if row.get("latest_submitted_at") else "",
        }
        for row in sent_rows
    ]
    return {"history": history, "sentClients": sent_clients}


def create_hmrc_64_8_request(user: dict, payload: dict) -> dict:
    client_name = _text(payload.get("clientName"), 250)
    if not client_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Client name is required.")
    include_sa = _normalise_bool(payload.get("includeSa"))
    include_paye = _normalise_bool(payload.get("includePaye"))
    include_ct = _normalise_bool(payload.get("includeCt"))
    if not any((include_sa, include_paye, include_ct)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one service (SA, PAYE, or CT).")
    now = utcnow()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO hmrc_64_8_requests (
                    created_by_user_id,
                    client_id,
                    client_name,
                    client_manager,
                    client_contact_name,
                    client_contact_email,
                    client_contact_phone,
                    postal_address,
                    sa_utr,
                    ct_utr,
                    paye_reference,
                    company_number,
                    include_sa,
                    include_paye,
                    include_ct,
                    status,
                    submission_channel,
                    hmrc_submission_reference,
                    expected_code_by,
                    reminder_count,
                    last_reminder_at,
                    notes,
                    evidence_links,
                    created_at,
                    updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s
                )
                RETURNING *
                """,
                (
                    user["id"],
                    _text(payload.get("clientId"), 120),
                    client_name,
                    _text(payload.get("clientManager"), 160),
                    _text(payload.get("clientContactName"), 160),
                    _text(payload.get("clientContactEmail"), 180),
                    _text(payload.get("clientContactPhone"), 80),
                    _text(payload.get("postalAddress"), 1000),
                    _text(payload.get("saUtr"), 40),
                    _text(payload.get("ctUtr"), 40),
                    _text(payload.get("payeReference"), 80),
                    _text(payload.get("companyNumber"), 30),
                    include_sa,
                    include_paye,
                    include_ct,
                    _normalise_status(payload.get("status"), default="draft"),
                    _normalise_channel(payload.get("submissionChannel"), default="online"),
                    _text(payload.get("hmrcSubmissionReference"), 120),
                    _parse_date_or_none(payload.get("expectedCodeBy")),
                    int(payload.get("reminderCount") or 0),
                    _parse_datetime_or_none(payload.get("lastReminderAt")),
                    _text(payload.get("notes"), 5000),
                    json.dumps(payload.get("evidenceLinks") if isinstance(payload.get("evidenceLinks"), list) else []),
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    result = _serialise_request(row)
    _record_audit_event("hmrc_64_8_request", result["id"], "hmrc_64_8.created", {"status": result["status"]}, user["id"])
    return result


def _get_user_request(user_id: str, request_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM hmrc_64_8_requests
                WHERE id = %s
                  AND created_by_user_id = %s
                """,
                (request_id, user_id),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HMRC 64-8 request not found.")
    return row


def update_hmrc_64_8_request(user: dict, request_id: str, payload: dict) -> dict:
    existing = _get_user_request(user["id"], request_id)
    status_value = _normalise_status(payload.get("status"), default=str(existing.get("status") or "draft"))
    include_sa = _normalise_bool(payload.get("includeSa")) if "includeSa" in payload else bool(existing.get("include_sa"))
    include_paye = _normalise_bool(payload.get("includePaye")) if "includePaye" in payload else bool(existing.get("include_paye"))
    include_ct = _normalise_bool(payload.get("includeCt")) if "includeCt" in payload else bool(existing.get("include_ct"))
    if not any((include_sa, include_paye, include_ct)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one service (SA, PAYE, or CT).")
    now = utcnow()
    authority_code = _text(payload.get("authorityCode"), 60) if "authorityCode" in payload else _text(existing.get("authority_code"), 60)
    authority_code_received_at = (
        _parse_datetime_or_none(payload.get("authorityCodeReceivedAt"))
        if "authorityCodeReceivedAt" in payload
        else existing.get("authority_code_received_at")
    )
    if authority_code and not authority_code_received_at:
        authority_code_received_at = now
    authority_activated_at = (
        _parse_datetime_or_none(payload.get("authorityActivatedAt"))
        if "authorityActivatedAt" in payload
        else existing.get("authority_activated_at")
    )
    if status_value == "authorised" and not authority_activated_at:
        authority_activated_at = now
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE hmrc_64_8_requests
                SET client_id = %s,
                    client_name = %s,
                    client_manager = %s,
                    client_contact_name = %s,
                    client_contact_email = %s,
                    client_contact_phone = %s,
                    postal_address = %s,
                    sa_utr = %s,
                    ct_utr = %s,
                    paye_reference = %s,
                    company_number = %s,
                    include_sa = %s,
                    include_paye = %s,
                    include_ct = %s,
                    status = %s,
                    submission_channel = %s,
                    hmrc_submission_reference = %s,
                    submitted_at = %s,
                    expected_code_by = %s,
                    reminder_count = %s,
                    last_reminder_at = %s,
                    authority_code = %s,
                    authority_code_received_at = %s,
                    authority_activated_at = %s,
                    notes = %s,
                    evidence_links = %s::jsonb,
                    updated_at = %s
                WHERE id = %s
                  AND created_by_user_id = %s
                RETURNING *
                """,
                (
                    _text(payload.get("clientId"), 120) if "clientId" in payload else existing.get("client_id"),
                    _text(payload.get("clientName"), 250) if "clientName" in payload else existing.get("client_name"),
                    _text(payload.get("clientManager"), 160)
                    if "clientManager" in payload
                    else existing.get("client_manager"),
                    _text(payload.get("clientContactName"), 160)
                    if "clientContactName" in payload
                    else existing.get("client_contact_name"),
                    _text(payload.get("clientContactEmail"), 180)
                    if "clientContactEmail" in payload
                    else existing.get("client_contact_email"),
                    _text(payload.get("clientContactPhone"), 80)
                    if "clientContactPhone" in payload
                    else existing.get("client_contact_phone"),
                    _text(payload.get("postalAddress"), 1000) if "postalAddress" in payload else existing.get("postal_address"),
                    _text(payload.get("saUtr"), 40) if "saUtr" in payload else existing.get("sa_utr"),
                    _text(payload.get("ctUtr"), 40) if "ctUtr" in payload else existing.get("ct_utr"),
                    _text(payload.get("payeReference"), 80)
                    if "payeReference" in payload
                    else existing.get("paye_reference"),
                    _text(payload.get("companyNumber"), 30)
                    if "companyNumber" in payload
                    else existing.get("company_number"),
                    include_sa,
                    include_paye,
                    include_ct,
                    status_value,
                    _normalise_channel(payload.get("submissionChannel"), default=str(existing.get("submission_channel") or "online")),
                    _text(payload.get("hmrcSubmissionReference"), 120)
                    if "hmrcSubmissionReference" in payload
                    else existing.get("hmrc_submission_reference"),
                    _parse_datetime_or_none(payload.get("submittedAt")) if "submittedAt" in payload else existing.get("submitted_at"),
                    _parse_date_or_none(payload.get("expectedCodeBy")) if "expectedCodeBy" in payload else existing.get("expected_code_by"),
                    int(payload.get("reminderCount") or 0) if "reminderCount" in payload else int(existing.get("reminder_count") or 0),
                    _parse_datetime_or_none(payload.get("lastReminderAt"))
                    if "lastReminderAt" in payload
                    else existing.get("last_reminder_at"),
                    authority_code,
                    authority_code_received_at,
                    authority_activated_at,
                    _text(payload.get("notes"), 5000) if "notes" in payload else existing.get("notes"),
                    json.dumps(payload.get("evidenceLinks"))
                    if isinstance(payload.get("evidenceLinks"), list)
                    else json.dumps(existing.get("evidence_links") if isinstance(existing.get("evidence_links"), list) else []),
                    now,
                    request_id,
                    user["id"],
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HMRC 64-8 request not found.")
    result = _serialise_request(row)
    _record_audit_event("hmrc_64_8_request", result["id"], "hmrc_64_8.updated", {"status": result["status"]}, user["id"])
    return result


def submit_hmrc_64_8_request(user: dict, request_id: str, payload: dict) -> dict:
    now = utcnow()
    expected_code_by = _parse_date_or_none(payload.get("expectedCodeBy")) if "expectedCodeBy" in payload else (now + timedelta(days=14)).date()
    return update_hmrc_64_8_request(
        user,
        request_id,
        {
            "status": "awaiting_code",
            "submittedAt": payload.get("submittedAt") or now.isoformat(),
            "expectedCodeBy": expected_code_by.isoformat() if expected_code_by else "",
            "hmrcSubmissionReference": payload.get("hmrcSubmissionReference") or "",
            "submissionChannel": payload.get("submissionChannel") or "online",
            "notes": payload.get("notes") or "",
        },
    )


def capture_hmrc_64_8_code(user: dict, request_id: str, payload: dict) -> dict:
    code = _text(payload.get("authorityCode"), 60).replace(" ", "").upper()
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Authority code is required.")
    activate = _normalise_bool(payload.get("activateNow"))
    next_status = "authorised" if activate else "code_received"
    return update_hmrc_64_8_request(
        user,
        request_id,
        {
            "authorityCode": code,
            "authorityCodeReceivedAt": payload.get("authorityCodeReceivedAt") or utcnow().isoformat(),
            "authorityActivatedAt": utcnow().isoformat() if activate else payload.get("authorityActivatedAt"),
            "status": next_status,
            "notes": payload.get("notes") or "",
        },
    )


def send_hmrc_64_8_reminder(user: dict, request_id: str, payload: dict) -> dict:
    existing = _get_user_request(user["id"], request_id)
    now = utcnow()
    next_count = int(existing.get("reminder_count") or 0) + 1
    notes = _text(payload.get("notes"), 5000) or _text(existing.get("notes"), 5000)
    reminder_method = _text(payload.get("method"), 40).lower() or "phone"
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE hmrc_64_8_requests
                SET reminder_count = %s,
                    last_reminder_at = %s,
                    notes = %s,
                    updated_at = %s
                WHERE id = %s
                  AND created_by_user_id = %s
                RETURNING *
                """,
                (next_count, now, notes, now, request_id, user["id"]),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HMRC 64-8 request not found.")
    result = _serialise_request(row)
    _record_audit_event(
        "hmrc_64_8_request",
        result["id"],
        "hmrc_64_8.reminder_logged",
        {"method": reminder_method, "reminderCount": next_count},
        user["id"],
    )
    return result
