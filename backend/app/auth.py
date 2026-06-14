from __future__ import annotations

import os
import re
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import HTTPException, Request, status

from .config import get_settings
from .database import get_connection, utcnow
from .security import create_session, hash_token, random_token


COOKIE_NAME = "credit_control_session"
REQUIRED_XERO_IDENTITY_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
)
DEFAULT_XERO_CONNECTION_SCOPES = (
    "accounting.invoices",
    "accounting.payments",
    "accounting.banktransactions",
    "accounting.manualjournals",
    "accounting.journals.read",
    "accounting.contacts",
    "accounting.settings",
    "accounting.attachments",
    "accounting.reports.banksummary.read",
    "accounting.reports.balancesheet.read",
    "accounting.reports.profitandloss.read",
    "accounting.reports.trialbalance.read",
    "assets.read",
)
XERO_PAYROLL_SCOPES = (
    "payroll.employees.read",
    "payroll.payruns.read",
)
KNOWN_XERO_SCOPES = {
    "openid",
    "profile",
    "email",
    "offline_access",
    "accounting.transactions",
    "accounting.transactions.read",
    "accounting.invoices",
    "accounting.invoices.read",
    "accounting.payments",
    "accounting.payments.read",
    "accounting.banktransactions",
    "accounting.banktransactions.read",
    "accounting.manualjournals",
    "accounting.manualjournals.read",
    "accounting.journals",
    "accounting.journals.read",
    "accounting.contacts",
    "accounting.contacts.read",
    "accounting.settings",
    "accounting.settings.read",
    "accounting.attachments",
    "accounting.attachments.read",
    "accounting.reports.read",
    "accounting.reports.aged.read",
    "accounting.reports.balancesheet.read",
    "accounting.reports.banksummary.read",
    "accounting.reports.budgetsummary.read",
    "accounting.reports.executivesummary.read",
    "accounting.reports.profitandloss.read",
    "accounting.reports.taxreports.read",
    "accounting.reports.trialbalance.read",
    "accounting.reports.tenninetynine.read",
    "accounting.budgets.read",
    "payroll.employees",
    "payroll.employees.read",
    "payroll.payruns",
    "payroll.payruns.read",
    "payroll.payslip",
    "payroll.payslip.read",
    "payroll.timesheets",
    "payroll.timesheets.read",
    "payroll.settings",
    "payroll.settings.read",
    "files",
    "files.read",
    "assets",
    "assets.read",
    "projects",
    "projects.read",
}
LEGACY_XERO_SCOPE_REPLACEMENTS = {
    "accounting.transactions.read": ("accounting.invoices.read", "accounting.payments.read", "accounting.banktransactions.read", "accounting.manualjournals.read", "accounting.journals.read"),
    "accounting.transactions": ("accounting.invoices", "accounting.payments", "accounting.banktransactions", "accounting.manualjournals", "accounting.journals.read"),
    "accounting.reports.read": (
        "accounting.reports.aged.read",
        "accounting.reports.balancesheet.read",
        "accounting.reports.profitandloss.read",
        "accounting.reports.trialbalance.read",
    ),
}
XERO_SCOPE_NORMALISATIONS = {
    "accounting.transaction": "accounting.transactions",
    "accounting.transaction.read": "accounting.transactions.read",
    "accounting.report.read": "accounting.reports.read",
    "payroll.employee": "payroll.employees",
    "payroll.employee.read": "payroll.employees.read",
    "payroll.payrun": "payroll.payruns",
    "payroll.payrun.read": "payroll.payruns.read",
}


def _configured_xero_scopes(configured_scopes: str) -> list[str]:
    scope_tokens: list[str] = []
    for raw_token in re.split(r"[\s,]+", str(configured_scopes or "").strip()):
        token = raw_token.strip().strip('"').strip("'").lower()
        if not token:
            continue
        if token.startswith("scope="):
            token = token.removeprefix("scope=").strip()
        token = XERO_SCOPE_NORMALISATIONS.get(token, token)
        if token and token not in scope_tokens:
            scope_tokens.append(token)
    return scope_tokens


def configured_xero_scopes_include_payroll(configured_scopes: str) -> bool:
    return any(scope.startswith("payroll.") for scope in _configured_xero_scopes(configured_scopes))


def xero_scope_string(configured_scopes: str, include_payroll_scopes: bool = False) -> str:
    scopes = []
    configured_scope_tokens = _configured_xero_scopes(configured_scopes)
    for configured_scope in configured_scope_tokens:
        for scope in LEGACY_XERO_SCOPE_REPLACEMENTS.get(configured_scope, (configured_scope,)):
            if scope not in KNOWN_XERO_SCOPES:
                continue
            if scope and scope not in scopes:
                scopes.append(scope)

    if not include_payroll_scopes:
        scopes = [scope for scope in scopes if not scope.startswith("payroll.")]
    elif not any(scope.startswith("payroll.") for scope in scopes):
        for payroll_scope in XERO_PAYROLL_SCOPES:
            if payroll_scope not in scopes:
                scopes.append(payroll_scope)

    # Preserve existing behaviour for empty or malformed configuration while
    # avoiding forced scope inflation when a valid explicit scope list exists.
    if not scopes:
        for default_scope in DEFAULT_XERO_CONNECTION_SCOPES:
            if default_scope not in scopes:
                scopes.append(default_scope)

    for required_scope in REQUIRED_XERO_IDENTITY_SCOPES:
        if required_scope not in scopes:
            scopes.append(required_scope)
    return " ".join(scopes)


def xero_scope_string_all_available() -> str:
    scopes = []
    for scope in sorted(KNOWN_XERO_SCOPES):
        if scope in REQUIRED_XERO_IDENTITY_SCOPES:
            continue
        if scope not in scopes:
            scopes.append(scope)
    for required_scope in REQUIRED_XERO_IDENTITY_SCOPES:
        if required_scope not in scopes:
            scopes.append(required_scope)
    return " ".join(scopes)


def allowed_panel_origins() -> set[str]:
    base_url = os.getenv("BASE_URL", "https://creditcontrolconsole-production.up.railway.app")
    panel_allowed_origins = os.getenv(
        "PANEL_ALLOWED_ORIGINS",
        "https://www.team.jaccountancy.co.uk,https://team.jaccountancy.co.uk,https://my.jaccountancy.co.uk",
    )
    origins = {base_url.rstrip("/")}
    origins.update(
        origin.strip().rstrip("/")
        for origin in panel_allowed_origins.split(",")
        if origin.strip()
    )
    return origins


def oauth_state_ttl_seconds(provider: str) -> int:
    settings = get_settings()
    if provider == "ignition":
        return max(settings.ignition_state_ttl_seconds, settings.xero_state_ttl_seconds)
    return settings.xero_state_ttl_seconds


def current_user_from_request(request: Request) -> dict | None:
    token = session_token_from_request(request)
    if not token:
        return None

    return user_for_session_token(token)


def require_user(request: Request) -> dict:
    user = current_user_from_request(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


def require_panel_user(request: Request) -> dict:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in allowed_panel_origins():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This panel origin is not allowed.")

    try:
        user = current_user_from_request(request)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Unable to validate the panel session right now.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
            },
        ) from exc
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Sign in with Xero again before syncing.",
        )
    return user


def panel_user_role(user: dict | None) -> str:
    role = str((user or {}).get("role") or "").strip().lower()
    return role or "admin"


def require_panel_roles(user: dict, allowed_roles: set[str], action: str) -> dict:
    role = panel_user_role(user)
    if role in allowed_roles:
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"Role '{role}' is not allowed to {action}. "
            f"Allowed roles: {', '.join(sorted(allowed_roles))}."
        ),
    )


def require_panel_write_user(user: dict, action: str = "perform this action") -> dict:
    return require_panel_roles(user, {"admin", "finance_admin", "client_manager"}, action)


def require_api_user(request: Request) -> dict:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    token = authorization.removeprefix("Bearer ").strip()
    if token == get_settings().widget_token:
        return {"id": None, "email": "widget@system", "full_name": "Widget Token", "widget_only": True}

    user = user_for_session_token(token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return user


def session_token_from_request(request: Request) -> str:
    cookie_token = request.cookies.get(COOKIE_NAME)
    if cookie_token:
        return cookie_token

    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ").strip()
    return ""


def user_for_session_token(token: str) -> dict | None:
    settings = get_settings()
    now = utcnow()
    expires_at = now + timedelta(days=settings.session_ttl_days)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT users.*, sessions.id AS session_id
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = %s
                  AND sessions.expires_at > NOW()
                """,
                (hash_token(token),),
            )
            row = cursor.fetchone()
            if row is None:
                return None

            cursor.execute(
                """
                UPDATE sessions
                SET last_seen_at = %s,
                    expires_at = %s
                WHERE id = %s
                """,
                (now, expires_at, row["session_id"]),
            )
        connection.commit()

    return row


def set_session_cookie(response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="none" if settings.base_url.startswith("https://") else "lax",
        secure=settings.base_url.startswith("https://"),
        max_age=settings.session_ttl_days * 24 * 60 * 60,
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME)


def start_oauth_state(
    redirect_to: str | None = None,
    device_code: str | None = None,
    user_id: str | None = None,
    provider: str = "xero",
    code_verifier: str | None = None,
) -> str:
    state_token = random_token()
    expires_at = utcnow() + timedelta(seconds=oauth_state_ttl_seconds(provider))

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO oauth_states (state_token, redirect_to, device_code, user_id, provider, code_verifier, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (state_token, redirect_to, device_code, user_id, provider, code_verifier, expires_at),
            )
        connection.commit()

    return state_token


def consume_oauth_state(state_token: str) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM oauth_states
                WHERE state_token = %s
                  AND expires_at > NOW()
                """,
                (state_token,),
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired state.")
            cursor.execute("DELETE FROM oauth_states WHERE id = %s", (row["id"],))
        connection.commit()

    return row


def create_device_login() -> dict:
    settings = get_settings()
    device_code = random_token(24)
    verification_code = random_token(8).replace("-", "")[:8].upper()
    expires_at = utcnow() + timedelta(minutes=settings.device_code_ttl_minutes)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO device_logins (device_code, verification_code, expires_at)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (device_code, verification_code, expires_at),
            )
            row = cursor.fetchone()
        connection.commit()

    return row


def complete_device_login(device_code: str, user_id: str) -> str:
    session_token = create_session(user_id, "macOS app")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE device_logins
                SET status = 'approved',
                    user_id = %s,
                    completed_at = %s
                WHERE device_code = %s
                  AND expires_at > NOW()
                RETURNING id
                """,
                (user_id, utcnow(), device_code),
            )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Device login expired.")
            cursor.execute(
                """
                UPDATE device_logins
                SET session_id = (SELECT id FROM sessions WHERE token_hash = %s),
                    session_token = %s
                WHERE id = %s
                """,
                (hash_token(session_token), session_token, row["id"]),
            )
        connection.commit()

    return session_token


def approve_device_code(verification_code: str, user_id: str) -> str:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM device_logins
                WHERE verification_code = %s
                  AND status = 'pending'
                  AND expires_at > NOW()
                """,
                (verification_code.upper(),),
            )
            row = cursor.fetchone()
        connection.commit()

    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code.")

    return complete_device_login(row["device_code"], user_id)


def xero_authorize_url(
    state_token: str,
    prompt_consent: bool = False,
    include_payroll_scopes: bool = False,
    include_all_scopes: bool = False,
) -> str:
    settings = get_settings()
    scope_value = (
        xero_scope_string_all_available()
        if include_all_scopes
        else xero_scope_string(settings.xero_scopes, include_payroll_scopes=include_payroll_scopes)
    )
    params = {
        "response_type": "code",
        "client_id": settings.xero_client_id,
        "redirect_uri": settings.xero_redirect_uri,
        "scope": scope_value,
        "state": state_token,
    }
    if prompt_consent:
        # Reconnect flows should always re-open consent so newly requested scopes are granted.
        params["prompt"] = "consent"
    query = urlencode(params)
    return f"https://login.xero.com/identity/connect/authorize?{query}"
