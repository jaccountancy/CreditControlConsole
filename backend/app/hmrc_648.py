from __future__ import annotations

import json
import csv
import io
import logging
import os
import re
from urllib.parse import urlencode
from datetime import date, datetime, timedelta
from uuid import uuid4

import httpx
from fastapi import HTTPException, status

from .auth import consume_oauth_state, start_oauth_state
from .database import get_connection, utcnow
from .security import decrypt_secret, encrypt_secret

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
logger = logging.getLogger(__name__)
SA_UTR_RE = re.compile(r"^\d{10}$")
CT_UTR_RE = re.compile(r"^\d{10}$")
NINO_RE = re.compile(r"^[A-CEGHJ-PR-TW-Z]{2}\d{6}[ABCD]$", re.IGNORECASE)
CRN_RE = re.compile(r"^(?:[A-Z]{2}\d{6}|\d{1,8})$", re.IGNORECASE)
PAYE_TAX_OFFICE_NUMBER_RE = re.compile(r"^\d{1,3}$")
PAYE_TAX_OFFICE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")
AO_REFERENCE_RE = re.compile(r"^[A-Za-z0-9]{1,13}$")
POSTCODE_RE = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2}$", re.IGNORECASE)
XML_ALLOWED_CHARS_RE = re.compile(r"^[A-Za-z0-9 &'()*,\-\./%!+:;=?@\[\]\^_{}~]*$")
VRN_RE = re.compile(r"^\d{9}$")


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


def _normalise_text_field(payload: dict, key: str, limit: int, existing: dict | None = None, existing_key: str | None = None) -> str:
    existing = existing or {}
    if key in payload:
        return _text(payload.get(key), limit)
    if existing_key:
        return _text(existing.get(existing_key), limit)
    return ""


def _extract_postcode(text_value: str) -> str:
    text_value = _text(text_value, 200)
    if not text_value:
        return ""
    compact = re.sub(r"\s+", " ", text_value).strip()
    match = re.search(r"([A-Za-z]{1,2}\d[A-Za-z\d]?\s?\d[A-Za-z]{2})", compact)
    if not match:
        return ""
    return match.group(1).upper()


def _split_tax_reference(value: str) -> tuple[str, str]:
    normalised = _text(value, 40).upper().replace(" ", "")
    if "/" not in normalised:
        return "", ""
    number, reference = normalised.split("/", 1)
    return number[:3], reference[:10]


def _validate_charset(value: str, label: str, max_length: int) -> None:
    if len(value) > max_length:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} must be {max_length} characters or fewer.")
    if value and not XML_ALLOWED_CHARS_RE.match(value):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} contains unsupported characters for HMRC XML.")


def _validate_postcode(value: str, label: str = "Postcode", allow_blank: bool = False) -> None:
    if not value and allow_blank:
        return
    if not value or not POSTCODE_RE.match(value):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{label} must be a valid UK postcode.")


def _require_config(name: str, limit: int = 2000) -> str:
    value = _text(os.getenv(name), limit)
    if not value:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Missing required HMRC configuration: {name}.")
    return value


def _mtd_token_label(user_id: str, token_type: str) -> str:
    return f"hmrc-mtd:{user_id}:{token_type}"


def _service_flags_from_payload(payload: dict, existing: dict | None = None) -> dict[str, bool]:
    existing = existing or {}

    def pick(payload_key: str, existing_key: str) -> bool:
        if payload_key in payload:
            return _normalise_bool(payload.get(payload_key))
        return bool(existing.get(existing_key))

    flags = {
        "includeSa": pick("includeSa", "include_sa"),
        "includePaye": pick("includePaye", "include_paye"),
        "includeCt": pick("includeCt", "include_ct"),
        "includeVatMtd": pick("includeVatMtd", "include_vat_mtd"),
        "includeSaMtd": pick("includeSaMtd", "include_sa_mtd"),
        "includeCis": pick("includeCis", "include_cis"),
    }
    if not any(flags.values()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one service (SA, PAYE, CT, VAT MTD, SA MTD, or CIS).",
        )
    return flags


def _connector_for_flags(flags: dict[str, bool]) -> str:
    has_legacy = any([flags.get("includeSa"), flags.get("includeCt"), flags.get("includePaye"), flags.get("includeCis")])
    has_mtd = any([flags.get("includeVatMtd"), flags.get("includeSaMtd")])
    if has_legacy and has_mtd:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Legacy (SA/CT/PAYE/CIS) and MTD (VAT/SA MTD) services cannot be submitted in one request. Split into separate requests.",
        )
    return "mtd" if has_mtd else "xml"


def _normalise_known_facts(payload: dict, existing: dict | None = None) -> dict:
    existing = existing or {}
    raw_postcode = _normalise_text_field(payload, "postcode", 12, existing, "postcode")
    postal_address = _normalise_text_field(payload, "postalAddress", 1000, existing, "postal_address")
    extracted_postcode = _extract_postcode(postal_address)
    postcode = _text(raw_postcode or extracted_postcode, 12).upper()
    paye_reference = _normalise_text_field(payload, "payeReference", 80, existing, "paye_reference")
    tax_office_number = _normalise_text_field(payload, "taxOfficeNumber", 3, existing, "tax_office_number")
    tax_office_reference = _normalise_text_field(payload, "taxOfficeReference", 10, existing, "tax_office_reference")
    split_number, split_reference = _split_tax_reference(paye_reference)
    if not tax_office_number:
        tax_office_number = split_number
    if not tax_office_reference:
        tax_office_reference = split_reference
    accounts_office_reference = _normalise_text_field(
        payload,
        "accountsOfficeReference",
        13,
        existing,
        "accounts_office_reference",
    ).upper().replace(" ", "")
    if not accounts_office_reference:
        accounts_office_reference = _text(paye_reference, 13).upper().replace(" ", "")
    return {
        "saNino": _normalise_text_field(payload, "saNino", 9, existing, "sa_nino").upper().replace(" ", ""),
        "postcode": postcode,
        "taxOfficeNumber": _text(tax_office_number, 3),
        "taxOfficeReference": _text(tax_office_reference, 10).upper(),
        "accountsOfficeReference": accounts_office_reference,
    }


def _validate_service_fields(payload: dict, flags: dict[str, bool], known: dict, require_connector_config: bool = False) -> None:
    connector = _connector_for_flags(flags)
    if require_connector_config and connector == "xml":
        _require_config("HMRC_XML_SENDER_ID")
        _require_config("HMRC_XML_AUTH_VALUE")
        _require_config("HMRC_XML_IR_AGENT_REFERENCE")
        _require_config("HMRC_XML_VENDOR_ID")
        _require_config("HMRC_XML_PRODUCT_NAME")
        _require_config("HMRC_XML_ENDPOINT_URL")
    if require_connector_config and connector == "mtd":
        _require_config("HMRC_MTD_CLIENT_ID")
        _require_config("HMRC_MTD_CLIENT_SECRET")
        _require_config("HMRC_MTD_REDIRECT_URI")
        _require_config("HMRC_MTD_SUBMIT_URL")

    sa_utr = _text(payload.get("saUtr"), 40)
    ct_utr = _text(payload.get("ctUtr"), 40)
    company_number = _text(payload.get("companyNumber"), 30).upper().replace(" ", "")

    if flags.get("includeSa") or flags.get("includeSaMtd"):
        if not SA_UTR_RE.match(sa_utr):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SA UTR must be exactly 10 digits.")
        sa_nino = known.get("saNino") or ""
        if sa_nino and not NINO_RE.match(sa_nino):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SA NINO must match format AB123456D.")
        _validate_postcode(known.get("postcode") or "")
    if flags.get("includeCt"):
        if not CT_UTR_RE.match(ct_utr):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CT UTR must be exactly 10 digits.")
        if company_number and not CRN_RE.match(company_number):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Company Registration Number must be either 8 digits or 2 letters followed by 6 digits.",
            )
        _validate_postcode(known.get("postcode") or "")
    if flags.get("includePaye") or flags.get("includeCis"):
        tax_office_number = known.get("taxOfficeNumber") or ""
        tax_office_reference = known.get("taxOfficeReference") or ""
        ao_reference = known.get("accountsOfficeReference") or ""
        if not PAYE_TAX_OFFICE_NUMBER_RE.match(tax_office_number):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tax office number must be 1 to 3 digits.")
        if not PAYE_TAX_OFFICE_REFERENCE_RE.match(tax_office_reference):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tax office reference must be 1 to 10 alphanumeric characters.")
        if not AO_REFERENCE_RE.match(ao_reference):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Accounts office reference must be 1 to 13 alphanumeric characters.")
    if flags.get("includeVatMtd"):
        _validate_charset(_text(payload.get("clientName"), 250), "Client name", 250)

    your_reference = _text(payload.get("clientId") or payload.get("yourReference"), 20)
    if flags.get("includeSa") and your_reference:
        _validate_charset(your_reference, "Your reference", 20)
    if flags.get("includeCt") and your_reference:
        _validate_charset(your_reference, "Your reference", 20)
    if (flags.get("includePaye") or flags.get("includeCis")) and your_reference:
        _validate_charset(your_reference, "Your reference", 16)


def _validate_authority_code(service_labels: list[str], code: str) -> None:
    text = _text(code, 20).upper().replace(" ", "")
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Authority code is required.")
    expects = []
    if any(label in service_labels for label in ("SA", "SA MTD")):
        expects.append("SA")
    if "CT" in service_labels:
        expects.append("CT")
    if any(label in service_labels for label in ("PAYE", "CIS")):
        expects.append("PE")
    if not expects:
        return
    if not any(text.startswith(prefix) for prefix in expects):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Authority code prefix does not match selected service. Expected one of: {', '.join(expects)}.",
        )
    if not re.match(r"^[A-Z]{2}\d{8}$", text):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Authority code must match format XX12345678.")


def _service_labels(row: dict) -> list[str]:
    services = []
    if row.get("include_sa"):
        services.append("SA")
    if row.get("include_paye"):
        services.append("PAYE")
    if row.get("include_ct"):
        services.append("CT")
    if row.get("include_vat_mtd"):
        services.append("VAT MTD")
    if row.get("include_sa_mtd"):
        services.append("SA MTD")
    if row.get("include_cis"):
        services.append("CIS")
    return services


def _hmrc_submission_payload(row: dict) -> dict:
    services = _service_labels(row)
    return {
        "requestId": str(row.get("id") or ""),
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
        "saNino": row.get("sa_nino") or "",
        "postcode": row.get("postcode") or "",
        "taxOfficeNumber": row.get("tax_office_number") or "",
        "taxOfficeReference": row.get("tax_office_reference") or "",
        "accountsOfficeReference": row.get("accounts_office_reference") or "",
        "services": services,
        "submissionChannel": row.get("submission_channel") or "online",
        "notes": row.get("notes") or "",
        "requestedAt": utcnow().isoformat(),
    }


def _hmrc_xml_payload(row: dict) -> str:
    message_class = "IR-AA-PAYE"
    if row.get("include_ct"):
        message_class = "IR-AA-CT"
    elif row.get("include_sa"):
        message_class = "IR-AA-SA"
    sender_id = _require_config("HMRC_XML_SENDER_ID")
    auth_method = _text(os.getenv("HMRC_XML_AUTH_METHOD"), 20).lower() or "clear"
    if auth_method not in {"clear", "md5"}:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="HMRC_XML_AUTH_METHOD must be 'clear' or 'md5'.")
    auth_value = _require_config("HMRC_XML_AUTH_VALUE")
    ir_agent_reference = _require_config("HMRC_XML_IR_AGENT_REFERENCE")
    vendor_id = _require_config("HMRC_XML_VENDOR_ID")
    product_name = _require_config("HMRC_XML_PRODUCT_NAME")
    product_version = _text(os.getenv("HMRC_XML_PRODUCT_VERSION"), 40) or "1.0"
    gateway_test = _text(os.getenv("HMRC_XML_GATEWAY_TEST"), 5) or "1"
    period_end = _text(os.getenv("HMRC_XML_PERIOD_END"), 10) or date.today().isoformat()
    your_reference = _text(row.get("client_id"), 20)
    auth_request_id = _text(row.get("hmrc_submission_reference"), 18)

    if row.get("include_sa"):
        nino_node = f"<NINO>{row.get('sa_nino') or ''}</NINO>" if row.get("sa_nino") else ""
        postcode_node = f"<Postcode>{row.get('postcode') or ''}</Postcode>" if row.get("postcode") else ""
        your_ref_node = f"<YourReference>{your_reference}</YourReference>" if your_reference else ""
        auth_request_node = f"<AuthRequestID>{auth_request_id}</AuthRequestID>" if auth_request_id else ""
        add_node = (
            f"<SA><UTR>{row.get('sa_utr') or ''}</UTR>"
            f"{nino_node}{postcode_node}{your_ref_node}{auth_request_node}"
            f"</SA>"
        )
    elif row.get("include_ct"):
        crn_node = f"<CRN>{row.get('company_number') or ''}</CRN>" if row.get("company_number") else ""
        postcode_node = f"<Postcode>{row.get('postcode') or ''}</Postcode>" if row.get("postcode") else ""
        your_ref_node = f"<YourReference>{your_reference}</YourReference>" if your_reference else ""
        auth_request_node = f"<AuthRequestID>{auth_request_id}</AuthRequestID>" if auth_request_id else ""
        add_node = (
            f"<CT><UTR>{row.get('ct_utr') or ''}</UTR>"
            f"{crn_node}{postcode_node}{your_ref_node}{auth_request_node}"
            f"</CT>"
        )
    else:
        paye_or_cis = "CIS" if row.get("include_cis") else "PAYE"
        your_ref_node = f"<YourReference>{your_reference}</YourReference>" if your_reference else ""
        auth_request_node = f"<AuthRequestID>{auth_request_id}</AuthRequestID>" if auth_request_id else ""
        add_node = (
            f"<{paye_or_cis}><TaxOffice><Number>{row.get('tax_office_number') or ''}</Number>"
            f"<Reference>{row.get('tax_office_reference') or ''}</Reference></TaxOffice>"
            f"<AOreference>{row.get('accounts_office_reference') or ''}</AOreference>"
            f"{your_ref_node}{auth_request_node}"
            f"</{paye_or_cis}>"
        )

    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<GovTalkMessage xmlns='http://www.govtalk.gov.uk/CM/envelope'>"
        "<EnvelopeVersion>2.0</EnvelopeVersion>"
        "<Header><MessageDetails>"
        f"<Class>{message_class}</Class><Qualifier>request</Qualifier><Function>submit</Function>"
        "<CorrelationID/><Transformation>XML</Transformation>"
        f"<GatewayTest>{gateway_test}</GatewayTest><GatewayTimestamp/>"
        "</MessageDetails><SenderDetails><IDAuthentication>"
        f"<SenderID>{sender_id}</SenderID>"
        f"<Authentication><Method>{auth_method}</Method><Role>principal</Role><Value>{auth_value}</Value></Authentication>"
        "</IDAuthentication></SenderDetails></Header>"
        "<GovTalkDetails><Keys>"
        f"<Key Type='IRAgentReference'>{ir_agent_reference}</Key>"
        "</Keys><TargetDetails><Organisation>IR</Organisation></TargetDetails>"
        "<ChannelRouting><Channel>"
        f"<URI>{vendor_id}</URI><Product>{product_name}</Product><Version>{product_version}</Version>"
        "</Channel></ChannelRouting></GovTalkDetails>"
        "<Body><IRenvelope xmlns='http://www.govtalk.gov.uk/taxation/AgentAuthRequest/1'>"
        "<IRheader><Keys>"
        f"<Key Type='IRAgentReference'>{ir_agent_reference}</Key>"
        f"</Keys><PeriodEnd>{period_end}</PeriodEnd><DefaultCurrency>GBP</DefaultCurrency><Sender>Agent</Sender></IRheader>"
        f"<AgentRequest><Add>{add_node}</Add></AgentRequest>"
        "</IRenvelope></Body></GovTalkMessage>"
    )


def _submit_xml_gateway(row: dict, payload: dict) -> dict:
    submitted_at = _parse_datetime_or_none(payload.get("submittedAt")) or utcnow()
    expected_code_by = _parse_date_or_none(payload.get("expectedCodeBy")) or (submitted_at + timedelta(days=14)).date()
    provided_reference = _text(payload.get("hmrcSubmissionReference"), 120)
    submit_url = _text(os.getenv("HMRC_XML_ENDPOINT_URL"), 1000)
    submit_token = _text(os.getenv("HMRC_XML_AUTH_TOKEN"), 400)
    try:
        timeout_raw = float(os.getenv("HMRC_XML_TIMEOUT", "20") or 20)
    except ValueError:
        timeout_raw = 20.0
    timeout_seconds = max(3.0, min(timeout_raw, 120.0))
    if not submit_url:
        return {
            "submittedAt": submitted_at.isoformat(),
            "expectedCodeBy": expected_code_by.isoformat(),
            "hmrcSubmissionReference": provided_reference or f"LOCAL-{uuid4().hex[:10].upper()}",
            "notesAppend": "Submitted using local XML workflow (HMRC_XML_ENDPOINT_URL not configured).",
        }

    xml_payload = _hmrc_xml_payload(row)
    headers = {"Content-Type": "application/xml"}
    if submit_token:
        headers["Authorization"] = f"Bearer {submit_token}"

    try:
        response = httpx.post(submit_url, content=xml_payload.encode("utf-8"), headers=headers, timeout=timeout_seconds)
    except Exception as exc:
        logger.exception("HMRC 64-8 XML submission request failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Unable to submit HMRC XML request: {exc}",
        ) from exc

    if response.status_code >= 400:
        message = ""
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                message = _text(parsed.get("detail") or parsed.get("message"), 500)
        except Exception:
            message = ""
        if not message:
            message = _text(response.text, 500) or f"Gateway returned HTTP {response.status_code}."
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"HMRC XML gateway error: {message}",
        )

    response_text = _text(response.text, 4000)
    match = re.search(r"<AuthRequestID>(\d{1,18})</AuthRequestID>", response_text, re.IGNORECASE)
    parsed_ref = match.group(1) if match else ""

    ref = _text(
        parsed_ref
        or provided_reference,
        120,
    )
    remote_submitted = submitted_at
    remote_expected = expected_code_by
    remote_notes = "XML request accepted by HMRC gateway."
    return {
        "submittedAt": remote_submitted.isoformat(),
        "expectedCodeBy": remote_expected.isoformat(),
        "hmrcSubmissionReference": ref or f"HMRC-{uuid4().hex[:10].upper()}",
        "notesAppend": remote_notes,
    }


def _load_hmrc_mtd_connection(user_id: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM hmrc_mtd_connections
                WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="HMRC MTD is not connected for this user.")
    return row


def hmrc_mtd_oauth_start(user: dict, redirect_to: str | None = None) -> dict:
    client_id = _require_config("HMRC_MTD_CLIENT_ID")
    redirect_uri = _require_config("HMRC_MTD_REDIRECT_URI")
    authorize_url = _text(os.getenv("HMRC_MTD_AUTH_URL"), 2000) or "https://test-www.tax.service.gov.uk/oauth/authorize"
    scopes = _text(os.getenv("HMRC_MTD_SCOPES"), 300) or "write:agent-authorisation read:agent-authorisation"
    state_token = start_oauth_state(redirect_to=redirect_to or "/credit-control-hmrc-64-8s", user_id=user["id"], provider="hmrc-mtd")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state_token,
        }
    )
    return {"authorizeUrl": f"{authorize_url}?{query}", "state": state_token}


def hmrc_mtd_oauth_callback(code: str, state: str) -> dict:
    state_row = consume_oauth_state(state)
    if _text(state_row.get("provider"), 40) != "hmrc-mtd":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid HMRC MTD OAuth state.")
    user_id = _text(state_row.get("user_id"), 80)
    if not user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="HMRC MTD OAuth state missing user context.")
    if not _text(code, 500):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing HMRC OAuth code.")

    token_url = _text(os.getenv("HMRC_MTD_TOKEN_URL"), 2000) or "https://test-api.service.hmrc.gov.uk/oauth/token"
    client_id = _require_config("HMRC_MTD_CLIENT_ID")
    client_secret = _require_config("HMRC_MTD_CLIENT_SECRET")
    redirect_uri = _require_config("HMRC_MTD_REDIRECT_URI")
    response = httpx.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"HMRC token exchange failed: {_text(response.text, 500)}")
    payload = response.json() if response.content else {}
    access_token = _text(payload.get("access_token"), 4000)
    refresh_token = _text(payload.get("refresh_token"), 4000)
    if not access_token or not refresh_token:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="HMRC token response missing access/refresh token.")
    expires_in = int(payload.get("expires_in") or 3600)
    expires_at = utcnow() + timedelta(seconds=max(300, expires_in))
    scope = _text(payload.get("scope"), 1000)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO hmrc_mtd_connections (
                    user_id,
                    access_token,
                    refresh_token,
                    scope,
                    expires_at,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    scope = EXCLUDED.scope,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    user_id,
                    encrypt_secret(access_token, _mtd_token_label(user_id, "access")),
                    encrypt_secret(refresh_token, _mtd_token_label(user_id, "refresh")),
                    scope,
                    expires_at,
                    utcnow(),
                    utcnow(),
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    return {
        "connected": True,
        "scope": row.get("scope") or "",
        "expiresAt": row.get("expires_at").isoformat() if row.get("expires_at") else "",
        "redirectTo": _text(state_row.get("redirect_to"), 1000) or "/credit-control-hmrc-64-8s",
    }


def _hmrc_mtd_access_token(user_id: str) -> str:
    row = _load_hmrc_mtd_connection(user_id)
    access_token = decrypt_secret(row.get("access_token"), _mtd_token_label(user_id, "access"))
    if row.get("expires_at") and row["expires_at"] > (utcnow() + timedelta(seconds=60)) and access_token:
        return access_token
    refresh_token = decrypt_secret(row.get("refresh_token"), _mtd_token_label(user_id, "refresh"))
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="HMRC MTD refresh token is missing. Reconnect HMRC.")
    token_url = _text(os.getenv("HMRC_MTD_TOKEN_URL"), 2000) or "https://test-api.service.hmrc.gov.uk/oauth/token"
    client_id = _require_config("HMRC_MTD_CLIENT_ID")
    client_secret = _require_config("HMRC_MTD_CLIENT_SECRET")
    response = httpx.post(
        token_url,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"HMRC refresh failed: {_text(response.text, 500)}")
    payload = response.json() if response.content else {}
    next_access = _text(payload.get("access_token"), 4000)
    next_refresh = _text(payload.get("refresh_token"), 4000) or refresh_token
    if not next_access:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="HMRC refresh response missing access token.")
    expires_in = int(payload.get("expires_in") or 3600)
    expires_at = utcnow() + timedelta(seconds=max(300, expires_in))
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE hmrc_mtd_connections
                SET access_token = %s,
                    refresh_token = %s,
                    expires_at = %s,
                    scope = COALESCE(NULLIF(%s, ''), scope),
                    updated_at = %s
                WHERE user_id = %s
                """,
                (
                    encrypt_secret(next_access, _mtd_token_label(user_id, "access")),
                    encrypt_secret(next_refresh, _mtd_token_label(user_id, "refresh")),
                    expires_at,
                    _text(payload.get("scope"), 1000),
                    utcnow(),
                    user_id,
                ),
            )
        connection.commit()
    return next_access


def _submit_mtd_gateway(user: dict, row: dict, payload: dict) -> dict:
    submitted_at = _parse_datetime_or_none(payload.get("submittedAt")) or utcnow()
    expected_code_by = _parse_date_or_none(payload.get("expectedCodeBy")) or (submitted_at + timedelta(days=14)).date()
    provided_reference = _text(payload.get("hmrcSubmissionReference"), 120)
    submit_url = _text(os.getenv("HMRC_MTD_SUBMIT_URL"), 2000)
    if not submit_url:
        return {
            "submittedAt": submitted_at.isoformat(),
            "expectedCodeBy": expected_code_by.isoformat(),
            "hmrcSubmissionReference": provided_reference or f"LOCAL-MTD-{uuid4().hex[:8].upper()}",
            "notesAppend": "Submitted using local MTD workflow (HMRC_MTD_SUBMIT_URL not configured).",
        }
    access_token = _hmrc_mtd_access_token(user["id"])
    response = httpx.post(
        submit_url,
        json=_hmrc_submission_payload(row),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"HMRC MTD submission failed: {_text(response.text, 500)}")
    result = response.json() if response.content else {}
    if not isinstance(result, dict):
        result = {}
    ref = _text(result.get("hmrcSubmissionReference") or result.get("reference"), 120) or provided_reference
    remote_submitted = _parse_datetime_or_none(result.get("submittedAt")) or submitted_at
    remote_expected = _parse_date_or_none(result.get("expectedCodeBy")) or expected_code_by
    return {
        "submittedAt": remote_submitted.isoformat(),
        "expectedCodeBy": remote_expected.isoformat(),
        "hmrcSubmissionReference": ref or f"HMRC-MTD-{uuid4().hex[:8].upper()}",
        "notesAppend": _text(result.get("message"), 500) or "MTD authorisation request submitted.",
    }


def _serialise_request(row: dict) -> dict:
    services = _service_labels(row)
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
        "saNino": row.get("sa_nino") or "",
        "postcode": row.get("postcode") or "",
        "taxOfficeNumber": row.get("tax_office_number") or "",
        "taxOfficeReference": row.get("tax_office_reference") or "",
        "accountsOfficeReference": row.get("accounts_office_reference") or "",
        "includeSa": bool(row.get("include_sa")),
        "includePaye": bool(row.get("include_paye")),
        "includeCt": bool(row.get("include_ct")),
        "includeVatMtd": bool(row.get("include_vat_mtd")),
        "includeSaMtd": bool(row.get("include_sa_mtd")),
        "includeCis": bool(row.get("include_cis")),
        "connector": "mtd" if (bool(row.get("include_vat_mtd")) or bool(row.get("include_sa_mtd"))) else "xml",
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


def hmrc_mtd_oauth_status(user: dict) -> dict:
    configured = all(
        [
            bool(_text(os.getenv("HMRC_MTD_CLIENT_ID"), 400)),
            bool(_text(os.getenv("HMRC_MTD_CLIENT_SECRET"), 400)),
            bool(_text(os.getenv("HMRC_MTD_REDIRECT_URI"), 2000)),
        ]
    )
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT user_id, scope, expires_at, updated_at
                FROM hmrc_mtd_connections
                WHERE user_id = %s
                """,
                (user["id"],),
            )
            row = cursor.fetchone()
        connection.commit()
    return {
        "configured": configured,
        "connected": bool(row),
        "scope": row.get("scope") if row else "",
        "expiresAt": row.get("expires_at").isoformat() if row and row.get("expires_at") else "",
        "updatedAt": row.get("updated_at").isoformat() if row and row.get("updated_at") else "",
    }


def hmrc_mtd_oauth_disconnect(user: dict) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM hmrc_mtd_connections WHERE user_id = %s", (user["id"],))
        connection.commit()
    return hmrc_mtd_oauth_status(user)


def _normalise_vrn(value) -> str:
    vrn = _text(value, 30).replace(" ", "")
    if not VRN_RE.match(vrn):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="VRN must be exactly 9 digits.")
    return vrn


def _hmrc_mtd_api_base_url() -> str:
    return _text(os.getenv("HMRC_MTD_API_BASE_URL"), 2000) or "https://test-api.service.hmrc.gov.uk"


def _hmrc_mtd_api_request(
    user: dict,
    *,
    method: str,
    path_or_url: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    access_token = _hmrc_mtd_access_token(user["id"])
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        url = path_or_url
    else:
        url = f"{_hmrc_mtd_api_base_url().rstrip('/')}/{path_or_url.lstrip('/')}"
    response = httpx.request(
        method=method.upper(),
        url=url,
        params=params or None,
        json=json_body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        detail = _text(response.text, 1200)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"HMRC API request failed ({response.status_code}): {detail}")
    if not response.content:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _normalise_authorisation_status(value: str | None) -> str:
    candidate = _text(value, 80).lower()
    if candidate in {"authorised", "authorized", "accepted", "active", "approved"}:
        return "authorised"
    if candidate in {"expired"}:
        return "expired"
    if candidate in {"cancelled", "canceled"}:
        return "cancelled"
    if candidate in {"rejected", "declined", "refused"}:
        return "rejected"
    if candidate in {"pending", "awaiting", "waiting"}:
        return "pending"
    return candidate or "pending"


def _extract_authorisation_link(payload: dict) -> str:
    for key in ("authorisationUrl", "authorizationUrl", "acceptUrl", "url", "clientAuthorisationUrl", "clientAuthorisationLink"):
        value = _text(payload.get(key), 4000)
        if value.startswith("http://") or value.startswith("https://"):
            return value
    links = payload.get("_links")
    if isinstance(links, dict):
        for key in ("client", "self", "accept"):
            entry = links.get(key)
            if isinstance(entry, dict):
                href = _text(entry.get("href"), 4000)
                if href.startswith("http://") or href.startswith("https://"):
                    return href
    return ""


def _extract_invitation_id(payload: dict) -> str:
    for key in ("invitationId", "authorisationId", "authorizationId", "id", "reference"):
        value = _text(payload.get(key), 240)
        if value:
            return value
    return ""


def _extract_authorisation_status(payload: dict) -> str:
    for key in ("status", "authorisationStatus", "authorizationStatus", "state"):
        value = _text(payload.get(key), 120)
        if value:
            return _normalise_authorisation_status(value)
    return "pending"


def _extract_accepted_at(payload: dict) -> datetime | None:
    for key in ("acceptedAt", "authorisedAt", "authorizedAt", "updatedAt"):
        value = payload.get(key)
        parsed = _parse_datetime_or_none(value) if value not in (None, "") else None
        if parsed:
            return parsed
    return None


def _extract_expires_at(payload: dict) -> datetime | None:
    for key in ("expiresAt", "expiryDate", "expirationDate"):
        value = payload.get(key)
        parsed = _parse_datetime_or_none(value) if value not in (None, "") else None
        if parsed:
            return parsed
    return None


def _serialise_vat_authorisation(row: dict) -> dict:
    return {
        "id": str(row.get("id") or ""),
        "gatewayClientId": row.get("gateway_client_id") or "",
        "vrn": row.get("vrn") or "",
        "agentReferenceNumber": row.get("agent_reference_number") or "",
        "service": row.get("service") or "mtd-vat",
        "invitationId": row.get("invitation_id") or "",
        "authorisationUrl": row.get("authorisation_url") or "",
        "status": row.get("status") or "pending",
        "statusDetail": row.get("status_detail") or "",
        "requestedAt": row.get("requested_at").isoformat() if row.get("requested_at") else "",
        "acceptedAt": row.get("accepted_at").isoformat() if row.get("accepted_at") else "",
        "expiresAt": row.get("expires_at").isoformat() if row.get("expires_at") else "",
        "lastCheckedAt": row.get("last_checked_at").isoformat() if row.get("last_checked_at") else "",
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else "",
    }


def _load_vat_authorisation(user_id: str, gateway_client_id: str, vrn: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM hmrc_mtd_vat_authorisations
                WHERE user_id = %s
                  AND gateway_client_id = %s
                  AND vrn = %s
                LIMIT 1
                """,
                (user_id, gateway_client_id, vrn),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No VAT authorisation record found for this gateway client and VRN.")
    return row


def _upsert_vat_authorisation(
    user_id: str,
    gateway_client_id: str,
    vrn: str,
    arn: str,
    invitation_id: str,
    authorisation_url: str,
    status_value: str,
    status_detail: str,
    requested_at: datetime | None,
    accepted_at: datetime | None,
    expires_at: datetime | None,
    raw_request: dict,
    raw_status: dict,
) -> dict:
    now = utcnow()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO hmrc_mtd_vat_authorisations (
                    user_id,
                    gateway_client_id,
                    vrn,
                    agent_reference_number,
                    service,
                    invitation_id,
                    authorisation_url,
                    status,
                    status_detail,
                    requested_at,
                    accepted_at,
                    expires_at,
                    last_checked_at,
                    raw_request,
                    raw_status,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s, %s, %s, %s, 'mtd-vat', %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s
                )
                ON CONFLICT (user_id, gateway_client_id, vrn)
                DO UPDATE SET
                    agent_reference_number = EXCLUDED.agent_reference_number,
                    invitation_id = COALESCE(NULLIF(EXCLUDED.invitation_id, ''), hmrc_mtd_vat_authorisations.invitation_id),
                    authorisation_url = COALESCE(NULLIF(EXCLUDED.authorisation_url, ''), hmrc_mtd_vat_authorisations.authorisation_url),
                    status = EXCLUDED.status,
                    status_detail = EXCLUDED.status_detail,
                    requested_at = COALESCE(EXCLUDED.requested_at, hmrc_mtd_vat_authorisations.requested_at),
                    accepted_at = COALESCE(EXCLUDED.accepted_at, hmrc_mtd_vat_authorisations.accepted_at),
                    expires_at = COALESCE(EXCLUDED.expires_at, hmrc_mtd_vat_authorisations.expires_at),
                    last_checked_at = EXCLUDED.last_checked_at,
                    raw_request = COALESCE(EXCLUDED.raw_request, hmrc_mtd_vat_authorisations.raw_request),
                    raw_status = COALESCE(EXCLUDED.raw_status, hmrc_mtd_vat_authorisations.raw_status),
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    user_id,
                    gateway_client_id,
                    vrn,
                    arn,
                    invitation_id,
                    authorisation_url,
                    status_value,
                    status_detail,
                    requested_at,
                    accepted_at,
                    expires_at,
                    now,
                    json.dumps(raw_request or {}),
                    json.dumps(raw_status or {}),
                    now,
                    now,
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def hmrc_mtd_start_vat_authorisation(user: dict, payload: dict) -> dict:
    gateway_client_id = _text(payload.get("gatewayClientId"), 120)
    if not gateway_client_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Gateway client ID is required.")
    vrn = _normalise_vrn(payload.get("vrn"))
    arn = _text(payload.get("agentReferenceNumber"), 120) or _text(os.getenv("HMRC_AGENT_REFERENCE_NUMBER"), 120)
    if not arn:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Agent Reference Number (ARN) is required.")
    invitation_url = _text(os.getenv("HMRC_MTD_AGENT_AUTHORISATION_URL"), 2000)
    if invitation_url:
        invitation_url = invitation_url.replace("{arn}", arn).replace("{vrn}", vrn)
    request_payload = {
        "service": "MTD-VAT",
        "gatewayClientId": gateway_client_id,
        "vrn": vrn,
        "agentReferenceNumber": arn,
    }
    if invitation_url:
        remote = _hmrc_mtd_api_request(user, method="POST", path_or_url=invitation_url, json_body=request_payload)
        auth_url = _extract_authorisation_link(remote)
        invitation_id = _extract_invitation_id(remote) or uuid4().hex
        status_value = _extract_authorisation_status(remote)
        status_detail = _text(remote.get("message"), 800)
        expires_at = _extract_expires_at(remote)
        record = _upsert_vat_authorisation(
            user_id=user["id"],
            gateway_client_id=gateway_client_id,
            vrn=vrn,
            arn=arn,
            invitation_id=invitation_id,
            authorisation_url=auth_url,
            status_value=status_value,
            status_detail=status_detail,
            requested_at=utcnow(),
            accepted_at=_extract_accepted_at(remote),
            expires_at=expires_at,
            raw_request=request_payload,
            raw_status=remote,
        )
        return {"authorisation": _serialise_vat_authorisation(record), "handshake": {"authorisationUrl": auth_url}}
    fallback_url = f"https://www.tax.service.gov.uk/vat-through-software/sign-up/client/{gateway_client_id}"
    record = _upsert_vat_authorisation(
        user_id=user["id"],
        gateway_client_id=gateway_client_id,
        vrn=vrn,
        arn=arn,
        invitation_id=f"LOCAL-{uuid4().hex[:12].upper()}",
        authorisation_url=fallback_url,
        status_value="pending",
        status_detail="HMRC_MTD_AGENT_AUTHORISATION_URL is not configured; fallback authorisation link generated locally.",
        requested_at=utcnow(),
        accepted_at=None,
        expires_at=utcnow() + timedelta(days=30),
        raw_request=request_payload,
        raw_status={},
    )
    return {"authorisation": _serialise_vat_authorisation(record), "handshake": {"authorisationUrl": fallback_url}}


def hmrc_mtd_check_vat_authorisation(user: dict, payload: dict) -> dict:
    gateway_client_id = _text(payload.get("gatewayClientId"), 120)
    vrn = _normalise_vrn(payload.get("vrn"))
    row = _load_vat_authorisation(user["id"], gateway_client_id, vrn)
    invitation_id = _text(payload.get("invitationId"), 240) or _text(row.get("invitation_id"), 240)
    status_url = _text(os.getenv("HMRC_MTD_AGENT_AUTHORISATION_STATUS_URL"), 2000)
    remote = {}
    status_value = _normalise_authorisation_status(_text(row.get("status"), 80))
    status_detail = _text(row.get("status_detail"), 800)
    accepted_at = row.get("accepted_at")
    expires_at = row.get("expires_at")
    if status_url and invitation_id:
        status_url = status_url.replace("{arn}", _text(row.get("agent_reference_number"), 120)).replace("{vrn}", vrn).replace("{invitationId}", invitation_id)
        remote = _hmrc_mtd_api_request(user, method="GET", path_or_url=status_url)
        status_value = _extract_authorisation_status(remote)
        status_detail = _text(remote.get("message") or remote.get("reason"), 800)
        accepted_at = _extract_accepted_at(remote) or accepted_at
        expires_at = _extract_expires_at(remote) or expires_at
    elif status_value in {"pending", "awaiting"}:
        status_detail = status_detail or "Status endpoint not configured; using last known local status."
    record = _upsert_vat_authorisation(
        user_id=user["id"],
        gateway_client_id=gateway_client_id,
        vrn=vrn,
        arn=_text(row.get("agent_reference_number"), 120),
        invitation_id=invitation_id,
        authorisation_url=_text(row.get("authorisation_url"), 4000),
        status_value=status_value,
        status_detail=status_detail,
        requested_at=row.get("requested_at"),
        accepted_at=accepted_at,
        expires_at=expires_at,
        raw_request=row.get("raw_request") if isinstance(row.get("raw_request"), dict) else {},
        raw_status=remote or (row.get("raw_status") if isinstance(row.get("raw_status"), dict) else {}),
    )
    return {"authorisation": _serialise_vat_authorisation(record)}


def _require_vat_authorised(user: dict, gateway_client_id: str, vrn: str) -> dict:
    row = _load_vat_authorisation(user["id"], gateway_client_id, vrn)
    status_value = _normalise_authorisation_status(_text(row.get("status"), 80))
    if status_value != "authorised":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="VAT MTD authorisation is not active for this client.")
    return row


def hmrc_vat_obligations(user: dict, payload: dict) -> dict:
    gateway_client_id = _text(payload.get("gatewayClientId"), 120)
    vrn = _normalise_vrn(payload.get("vrn"))
    _require_vat_authorised(user, gateway_client_id, vrn)
    params = {
        "from": _text(payload.get("from"), 40),
        "to": _text(payload.get("to"), 40),
        "status": _text(payload.get("status"), 20) or "O",
    }
    cleaned = {key: value for key, value in params.items() if value}
    remote = _hmrc_mtd_api_request(
        user,
        method="GET",
        path_or_url=f"/organisations/vat/{vrn}/obligations",
        params=cleaned,
    )
    return {"gatewayClientId": gateway_client_id, "vrn": vrn, "obligations": remote.get("obligations") if isinstance(remote.get("obligations"), list) else []}


def hmrc_vat_returns(user: dict, payload: dict) -> dict:
    gateway_client_id = _text(payload.get("gatewayClientId"), 120)
    vrn = _normalise_vrn(payload.get("vrn"))
    period_key = _text(payload.get("periodKey"), 40)
    if not period_key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="periodKey is required.")
    _require_vat_authorised(user, gateway_client_id, vrn)
    remote = _hmrc_mtd_api_request(
        user,
        method="GET",
        path_or_url=f"/organisations/vat/{vrn}/returns/{period_key}",
    )
    return {"gatewayClientId": gateway_client_id, "vrn": vrn, "periodKey": period_key, "return": remote}


def hmrc_vat_liabilities(user: dict, payload: dict) -> dict:
    gateway_client_id = _text(payload.get("gatewayClientId"), 120)
    vrn = _normalise_vrn(payload.get("vrn"))
    _require_vat_authorised(user, gateway_client_id, vrn)
    params = {
        "from": _text(payload.get("from"), 40),
        "to": _text(payload.get("to"), 40),
    }
    cleaned = {key: value for key, value in params.items() if value}
    remote = _hmrc_mtd_api_request(
        user,
        method="GET",
        path_or_url=f"/organisations/vat/{vrn}/liabilities",
        params=cleaned,
    )
    return {"gatewayClientId": gateway_client_id, "vrn": vrn, "liabilities": remote.get("liabilities") if isinstance(remote.get("liabilities"), list) else []}


def hmrc_vat_payments(user: dict, payload: dict) -> dict:
    gateway_client_id = _text(payload.get("gatewayClientId"), 120)
    vrn = _normalise_vrn(payload.get("vrn"))
    _require_vat_authorised(user, gateway_client_id, vrn)
    params = {
        "from": _text(payload.get("from"), 40),
        "to": _text(payload.get("to"), 40),
    }
    cleaned = {key: value for key, value in params.items() if value}
    remote = _hmrc_mtd_api_request(
        user,
        method="GET",
        path_or_url=f"/organisations/vat/{vrn}/payments",
        params=cleaned,
    )
    return {"gatewayClientId": gateway_client_id, "vrn": vrn, "payments": remote.get("payments") if isinstance(remote.get("payments"), list) else []}


def hmrc_vat_submit_return(user: dict, payload: dict) -> dict:
    gateway_client_id = _text(payload.get("gatewayClientId"), 120)
    vrn = _normalise_vrn(payload.get("vrn"))
    vat_return = payload.get("return")
    if not isinstance(vat_return, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="return payload is required.")
    _require_vat_authorised(user, gateway_client_id, vrn)
    remote = _hmrc_mtd_api_request(
        user,
        method="POST",
        path_or_url=f"/organisations/vat/{vrn}/returns",
        json_body=vat_return,
    )
    return {"gatewayClientId": gateway_client_id, "vrn": vrn, "submission": remote}


def _serialise_vat_gateway_record(row: dict) -> dict:
    status_value = _text(row.get("status"), 40).lower() or "draft"
    return {
        "gatewayClientId": row.get("client_id") or "",
        "clientName": row.get("client_name") or "",
        "clientManager": row.get("client_manager") or "",
        "clientContactName": row.get("client_contact_name") or "",
        "clientContactEmail": row.get("client_contact_email") or "",
        "clientContactPhone": row.get("client_contact_phone") or "",
        "postalAddress": row.get("postal_address") or "",
        "postcode": row.get("postcode") or "",
        "status": status_value,
        "hmrcSubmissionReference": row.get("hmrc_submission_reference") or "",
        "expectedCodeBy": row.get("expected_code_by").isoformat() if row.get("expected_code_by") else "",
        "submittedAt": row.get("submitted_at").isoformat() if row.get("submitted_at") else "",
        "authorityCodeReceivedAt": row.get("authority_code_received_at").isoformat() if row.get("authority_code_received_at") else "",
        "authorityActivatedAt": row.get("authority_activated_at").isoformat() if row.get("authority_activated_at") else "",
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else "",
        "vatMtdLinked": bool(row.get("include_vat_mtd")),
        "vatMtdAuthorised": status_value == "authorised",
    }


def hmrc_vat_gateway_clients(user: dict, search: str = "", limit: int = 50) -> list[dict]:
    search_value = _text(search, 200).lower()
    limit_value = max(1, min(int(limit or 50), 200))
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT ON (client_id)
                    client_id,
                    client_name,
                    client_manager,
                    client_contact_name,
                    client_contact_email,
                    client_contact_phone,
                    postal_address,
                    postcode,
                    status,
                    hmrc_submission_reference,
                    expected_code_by,
                    submitted_at,
                    authority_code_received_at,
                    authority_activated_at,
                    include_vat_mtd,
                    updated_at
                FROM hmrc_64_8_requests
                WHERE created_by_user_id = %s
                  AND include_vat_mtd = TRUE
                  AND client_id <> ''
                  AND (
                    %s = ''
                    OR LOWER(client_id) LIKE %s
                    OR LOWER(client_name) LIKE %s
                  )
                ORDER BY client_id, updated_at DESC
                LIMIT %s
                """,
                (
                    user["id"],
                    search_value,
                    f"%{search_value}%",
                    f"%{search_value}%",
                    limit_value,
                ),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return [_serialise_vat_gateway_record(row) for row in rows]


def hmrc_vat_gateway_client_detail(user: dict, gateway_client_id: str) -> dict:
    client_id = _text(gateway_client_id, 120)
    if not client_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Gateway client ID is required.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    client_id,
                    client_name,
                    client_manager,
                    client_contact_name,
                    client_contact_email,
                    client_contact_phone,
                    postal_address,
                    postcode,
                    status,
                    hmrc_submission_reference,
                    expected_code_by,
                    submitted_at,
                    authority_code_received_at,
                    authority_activated_at,
                    include_vat_mtd,
                    updated_at
                FROM hmrc_64_8_requests
                WHERE created_by_user_id = %s
                  AND include_vat_mtd = TRUE
                  AND client_id = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user["id"], client_id),
            )
            row = cursor.fetchone()
        connection.commit()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No VAT gateway record found for this client ID.")
    return _serialise_vat_gateway_record(row)


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
    return {"requests": requests, "summary": summary, "mtdOAuth": hmrc_mtd_oauth_status(user)}


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
    service_flags = _service_flags_from_payload(payload)
    known = _normalise_known_facts(payload)
    _validate_service_fields(payload, service_flags, known)
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
                    sa_nino,
                    postcode,
                    tax_office_number,
                    tax_office_reference,
                    accounts_office_reference,
                    include_sa,
                    include_paye,
                    include_ct,
                    include_vat_mtd,
                    include_sa_mtd,
                    include_cis,
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
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s
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
                    known["saNino"],
                    known["postcode"],
                    known["taxOfficeNumber"],
                    known["taxOfficeReference"],
                    known["accountsOfficeReference"],
                    service_flags["includeSa"],
                    service_flags["includePaye"],
                    service_flags["includeCt"],
                    service_flags["includeVatMtd"],
                    service_flags["includeSaMtd"],
                    service_flags["includeCis"],
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
    service_flags = _service_flags_from_payload(payload, existing=existing)
    known = _normalise_known_facts(payload, existing=existing)
    validate_payload = dict(payload)
    if "saUtr" not in validate_payload:
        validate_payload["saUtr"] = existing.get("sa_utr")
    if "ctUtr" not in validate_payload:
        validate_payload["ctUtr"] = existing.get("ct_utr")
    if "companyNumber" not in validate_payload:
        validate_payload["companyNumber"] = existing.get("company_number")
    if "payeReference" not in validate_payload:
        validate_payload["payeReference"] = existing.get("paye_reference")
    if "clientName" not in validate_payload:
        validate_payload["clientName"] = existing.get("client_name")
    if "clientId" not in validate_payload:
        validate_payload["clientId"] = existing.get("client_id")
    _validate_service_fields(validate_payload, service_flags, known)
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
                    sa_nino = %s,
                    postcode = %s,
                    tax_office_number = %s,
                    tax_office_reference = %s,
                    accounts_office_reference = %s,
                    include_sa = %s,
                    include_paye = %s,
                    include_ct = %s,
                    include_vat_mtd = %s,
                    include_sa_mtd = %s,
                    include_cis = %s,
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
                    known["saNino"],
                    known["postcode"],
                    known["taxOfficeNumber"],
                    known["taxOfficeReference"],
                    known["accountsOfficeReference"],
                    service_flags["includeSa"],
                    service_flags["includePaye"],
                    service_flags["includeCt"],
                    service_flags["includeVatMtd"],
                    service_flags["includeSaMtd"],
                    service_flags["includeCis"],
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
    existing = _get_user_request(user["id"], request_id)
    flags = {
        "includeSa": bool(existing.get("include_sa")),
        "includePaye": bool(existing.get("include_paye")),
        "includeCt": bool(existing.get("include_ct")),
        "includeVatMtd": bool(existing.get("include_vat_mtd")),
        "includeSaMtd": bool(existing.get("include_sa_mtd")),
        "includeCis": bool(existing.get("include_cis")),
    }
    validate_payload = {
        "saUtr": existing.get("sa_utr"),
        "ctUtr": existing.get("ct_utr"),
        "companyNumber": existing.get("company_number"),
        "payeReference": existing.get("paye_reference"),
        "clientName": existing.get("client_name"),
        "clientId": existing.get("client_id"),
    }
    known = _normalise_known_facts({}, existing=existing)
    _validate_service_fields(validate_payload, flags, known, require_connector_config=True)
    gateway_result = _submit_mtd_gateway(user, existing, payload) if _connector_for_flags(flags) == "mtd" else _submit_xml_gateway(existing, payload)
    existing_notes = _text(existing.get("notes"), 5000)
    payload_notes = _text(payload.get("notes"), 5000)
    gateway_note = _text(gateway_result.get("notesAppend"), 500)
    note_parts = [part for part in [payload_notes or existing_notes, gateway_note] if part]
    merged_notes = " | ".join(note_parts)[:5000] if note_parts else ""
    return update_hmrc_64_8_request(
        user,
        request_id,
        {
            "status": "awaiting_code",
            "submittedAt": gateway_result.get("submittedAt"),
            "expectedCodeBy": gateway_result.get("expectedCodeBy"),
            "hmrcSubmissionReference": gateway_result.get("hmrcSubmissionReference"),
            "submissionChannel": payload.get("submissionChannel") or "online",
            "notes": merged_notes,
        },
    )


def capture_hmrc_64_8_code(user: dict, request_id: str, payload: dict) -> dict:
    existing = _get_user_request(user["id"], request_id)
    code = _text(payload.get("authorityCode"), 60).replace(" ", "").upper()
    if not code:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Authority code is required.")
    _validate_authority_code(_service_labels(existing), code)
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
