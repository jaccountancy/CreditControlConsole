from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from .auth import (
    COOKIE_NAME,
    allowed_panel_origins,
    approve_device_code,
    clear_session_cookie,
    configured_xero_scopes_include_payroll,
    consume_oauth_state,
    create_device_login,
    current_user_from_request,
    require_api_user,
    require_panel_user,
    require_panel_write_user,
    require_user,
    set_session_cookie,
    start_oauth_state,
    xero_authorize_url,
)
from .companies_house import (
    commit_auth_code_register_import,
    bulk_raise_submission_invoices,
    bulk_submit_confirmation_statements,
    create_bulk_submission_job,
    get_bulk_submission_job,
    run_bulk_submission_job,
    complete_company_secretarial_filing,
    commit_clients_import,
    create_company_secretarial_filing,
    add_auth_register_client_note,
    dashboard_summary as companies_house_dashboard_summary,
    delete_company,
    export_companies_house_support_report,
    export_submission_attempts_csv,
    get_companies_house_settings,
    get_company_detail,
    get_auth_register_client_page,
    get_submission_raw_response,
    list_dead_letters,
    list_company_secretarial_filings,
    list_companies,
    list_auth_code_register,
    list_imports as list_companies_house_imports,
    list_submission_attempts,
    patch_company_secretarial_filing,
    parse_clients_import,
    preview_auth_code_register_csv,
    populate_auth_codes_from_register,
    populate_xero_lock_date_company_numbers,
    replay_dead_letter_submissions,
    run_companies_house_submission_reconciliation,
    save_companies_house_settings,
    submit_company_secretarial_filing,
    sync_xero_lock_date_company_records,
    save_auth_register_client_page,
    submission_reconciliation_report,
    start_companies_house_auto_sync_worker,
    sync_companies_house_companies,
    test_companies_house_connection,
    upload_auth_code_register_csv,
    update_auth_code_register_row,
    update_company,
    validate_company_secretarial_filing,
)
from .config import get_settings
from .database import ensure_schema, get_connection
from .security import create_session, hash_token
from .services import (
    add_customer_note,
    add_note,
    add_promise,
    active_ignition_sync_run_for_user,
    active_me_report_sync_run_for_user,
    active_sync_run_for_user,
    active_xero_rate_limit_for_user,
    allocate_customer_credit,
    add_bank_statement_client,
    create_bad_debt_write_offs,
    create_bank_statement_account,
    code_breaker_apply_xero_transaction_action,
    code_breaker_workspace_snapshot,
    create_late_payment_charges,
    company_calendar_payload,
    create_payment_plan,
    apply_customer_vat_transaction_edits,
    customer_vat_return_transactions,
    customer_vat_return_unreconciled_transactions,
    vat_no_vat_suggestions,
    customer_detail,
    customer_xero_transactions,
    dashboard_payload,
    disconnect_xero,
    delete_bank_statement_client,
    delete_bank_statement_upload,
    delete_me_report_client,
    delete_me_report_submission,
    delete_customer_vat_unreconciled_transaction,
    extract_me_report_ct_comps_loss,
    factory_reset_console,
    fix_xero_lock_date_mismatch,
    get_xero_connection_for_user,
    get_operation_run,
    get_ignition_sync_run,
    create_ignition_renewal_run,
    delete_ignition_renewal_run,
    finalise_ignition_renewals,
    unlock_ignition_renewals,
    populate_ignition_renewal_candidate_client_ids,
    ignition_renewals_audit_history,
    ignition_renewals_email_preview,
    ignition_renewals_report_pdf,
    mark_ignition_renewal_proposals_ineligible,
    restore_ignition_renewal_proposals_to_eligible,
    insights_payload,
    ignition_payload,
    ignition_renewals_payload,
    micro_analyzer_clients_payload,
    generate_risk_assessments_payload,
    build_risk_assessments_zip_payload,
    preview_risk_assessments_xero_payload,
    send_risk_assessments_to_xero_payload,
    invoice_detail,
    install_sync_signal_handlers,
    add_jashflow_charge,
    add_jashflow_payment,
    create_jashflow_loan,
    delete_jashflow_loan,
    jashflow_payload,
    jashflow_interest_preview,
    post_jashflow_interest_invoice,
    save_jashflow_settings,
    update_jashflow_loan,
    list_customers,
    clear_developer_logs,
    list_developer_logs,
    runtime_diagnostics_payload,
    connect_me_report_client_to_current_xero,
    create_me_report_client,
    generate_me_report,
    get_me_report_sync_run,
    normalise_sync_options,
    panel_payload,
    pending_xero_actions_payload,
    pi_clearing_payload,
    pi_clearing_dry_run_pdf,
    practice_pack_payload,
    list_retained_practice_pack_runs,
    retained_practice_pack_download,
    process_pending_xero_actions,
    run_pi_clearing_workflow,
    override_bank_statement_transaction,
    payroll_headcount_payload,
    call_stats_dashboard_payload,
    call_stats_import_preview,
    call_stats_import_commit,
    call_stats_resync,
    call_stats_extension_directory_payload,
    call_stats_save_extension,
    call_stats_unmatched_numbers,
    call_stats_apply_number_action,
    call_stats_client_logs_payload,
    call_stats_generate_ai_report,
    call_stats_ai_reports_history,
    call_stats_suggest_filter_presets,
    bank_statement_upload_source_file,
    get_sync_run,
    me_report_payload,
    me_report_report_html,
    me_report_report_pdf,
    record_sync_start_failure,
    request_me_report_sync_run,
    request_sync_run,
    request_ignition_sync_run,
    queue_pending_xero_action,
    run_ignition_sync_job,
    run_me_report_sync_job,
    request_operation_run,
    run_invoice_operation_job,
    run_sync,
    run_sync_job,
    save_pi_clearing_account_setup,
    save_posting_settings,
    save_xero_tenant_company_mapping,
    send_ignition_renewal_client_comms_email,
    extract_ignition_renewal_document_id,
    serialize_sync_run,
    serialize_xero_rate_limit,
    serialize_ignition_sync_run,
    serialize_me_report_sync_run,
    serialize_operation_run,
    add_supplier_reconciliation_client,
    delete_supplier_reconciliation_client,
    send_supplier_reconciliation_email,
    barclays_connect_status_payload,
    build_barclays_authorize_url,
    complete_barclays_oauth_callback,
    supplier_payments_payload,
    supplier_payments_settle,
    supplier_payments_settle_via_barclays,
    send_supplier_payment_remittance_advices,
    supplier_reconciliation_contact_options_payload,
    contact_archive_review_payload,
    contact_archive_bulk_archive_payload,
    contact_archive_client_register_sync_payload,
    supplier_reconciliation_extract,
    supplier_reconciliation_payload,
    sync_customer_note_to_xero,
    sync_invoice_promise_to_xero,
    sync_invoice_note_to_xero,
    sync_invoice_status_to_xero,
    sync_payment_plan_to_xero,
    sync_run_has_working_data,
    send_me_report_email,
    send_ignition_renewals_email,
    sync_payroll_headcount_with_ignition,
    sync_payroll_headcount_workspace,
    juk_equity_payload,
    update_control_status,
    update_ignition_renewal_run,
    update_bank_statement_account,
    update_me_report_client,
    update_me_report_client_status,
    update_me_report_exception,
    update_me_report_mapping,
    update_me_report_settings,
    upsert_payroll_headcount_workspace,
    apply_pi_clearing_credit_notes,
    delete_pi_clearing_run,
    save_pi_clearing_step1_fix,
    void_pi_clearing_credit_note,
    xero_lock_date_overview_payload,
    xero_lock_date_mismatch_payload,
    xero_lock_date_mismatch_pdf,
    xero_chart_of_accounts_payload,
    xero_vat_returns_payload,
    xero_vat_return_transactions_by_tenant,
    xero_scope_audit_payload,
    xero_set_tenant_transactions_no_vat,
    xero_vat_coded_transactions_by_tenant,
    bank_statement_payload,
    bulk_update_invoice_status,
    bulk_send_me_report_emails,
    bulk_upload_me_report_submission_pdfs,
    bm_tasks_vat_preview_payload,
    bm_tasks_vat_saved_payload,
    categorise_bank_statement_transactions,
    exchange_gmail_code_for_tokens,
    fetch_gmail_profile,
    gmail_authorize_url,
    gmail_oauth_configured,
    merge_me_report_duplicate_contact,
    merge_me_report_contacts,
    rename_me_report_nominal_account,
    delete_me_report_draft_sales_invoice,
    me_report_juk_invoice_check,
    mark_me_report_purchases_paid_personally,
    delete_me_report_unreconciled_transaction,
    juksib_apply_override,
    juksib_automation_payload,
    juksib_batch_excel_report,
    run_juksib_automation_now,
    juksib_batch_audit,
    juksib_bulk_update_invoice_status,
    juksib_include_excluded_invoices,
    juksib_get_batch,
    juksib_import_batch,
    juksib_list_batches,
    juksib_delete_batch,
    juksib_revert_batch_to_draft,
    juksib_publish_batch,
    juksib_source_invoice_pdf as juksib_source_invoice_pdf_bytes,
    start_juksib_automation_worker,
    update_juksib_automation_settings,
    vault_analyze_files,
    vault_assign_files_to_client,
    vault_delete_file,
    vault_file_content,
    vault_payload,
    vault_upload_files,
    queue_bank_statement_retry,
    queue_bank_statement_upload,
    store_gmail_connection,
    upload_me_report_submission_pdf,
    _process_bank_statement_upload,
)
from .usage_metrics import deployment_updates_payload, usage_detail_payload, usage_overview_payload
from .foxit_esign import (
    FoxitESignConfigurationError,
    foxit_esign_cancel_request,
    foxit_esign_configured,
    foxit_esign_resend_request,
    foxit_esign_send_request,
    foxit_esign_status_request,
)
from .ignition import (
    IgnitionConfigurationError,
    create_pkce_verifier,
    exchange_ignition_code_for_tokens,
    ignition_authorize_url,
    store_ignition_connection,
)
from .hmrc_648 import (
    capture_hmrc_64_8_code,
    create_hmrc_64_8_request,
    hmrc_64_8_export_csv,
    hmrc_64_8_history,
    hmrc_64_8_payload,
    hmrc_mtd_oauth_callback,
    hmrc_mtd_oauth_disconnect,
    hmrc_mtd_oauth_start,
    hmrc_mtd_oauth_status,
    send_hmrc_64_8_reminder,
    submit_hmrc_64_8_request,
    update_hmrc_64_8_request,
)
from .xero import XeroConfigurationError, exchange_code_for_tokens, fetch_connections, fetch_user_profile, store_login
from .snackccountancy import (
    SNACK_SESSION_LABEL,
    calculate_snackccountancy_basket,
    clear_snack_session_cookie,
    set_snack_session_cookie,
    snack_create_payment,
    snack_customer_summary,
    snack_dashboard_payload,
    snack_handle_stripe_webhook,
    snack_claim_paid_order_session,
    snack_login_with_email,
    snack_logout,
    snack_orders_admin,
    snack_orders_for_customer,
    snack_products_admin_upsert,
    snack_products_payload,
    snack_session_context_from_request,
    snack_customers_admin,
    snack_customer_admin_patch,
    snack_order_admin_patch,
    _create_snack_session,
)

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_PANEL_DIR = BASE_DIR.parent / "WebPanel"
LEGACY_CONSOLE_PATH = BASE_DIR / "static" / "console.html"
SNACKCCOUNTANCY_PATH = BASE_DIR / "static" / "Snackccountancy.html"
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Credit Control Backend", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_panel_origins()),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
PANEL_ENTRY_PATHS = {
    "/",
    "/console",
    "/standalone.html",
    "/app.js",
    "/styles.css",
}
PUBLIC_STATIC_ASSETS = {
    "/static/styles.css",
    "/static/jACCOUNTANCYBLUEHORIZONTLE.PNG",
}
PUBLIC_API_PREFIXES = (
    "/api/snackccountancy/",
)
PUBLIC_API_PATHS = {
    "/api/auth/login-approval/status",
    "/api/auth/login-approval/complete",
    "/api/auth/login-approval/resend",
    "/api/device/start",
    "/api/device/poll",
    "/api/stripe/snackccountancy-webhook",
    "/api/hmrc-64-8/oauth/callback",
    "/api/ignition/callback",
}
RBAC_DEFAULT_READ_ROLES = {"owner", "admin", "manager", "staff", "read_only"}
RBAC_DEFAULT_WRITE_ROLES = {"owner", "admin", "manager", "staff"}
RBAC_OWNER_ADMIN_ROLES = {"owner", "admin"}
RBAC_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
RBAC_RULES = {
    "/api/panel/session": {"read": RBAC_DEFAULT_READ_ROLES, "write": RBAC_DEFAULT_READ_ROLES},
    "/api/security/": {"read": {"owner", "admin", "manager"}, "write": RBAC_OWNER_ADMIN_ROLES},
    "/api/developer/": {"read": RBAC_OWNER_ADMIN_ROLES, "write": RBAC_OWNER_ADMIN_ROLES},
    "/api/panel/factory-reset": {"read": RBAC_OWNER_ADMIN_ROLES, "write": RBAC_OWNER_ADMIN_ROLES},
}
CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_EXEMPT_PATHS = {
    "/api/stripe/snackccountancy-webhook",
}


def _normalise_path_for_guard(path: str) -> str:
    if path == "/":
        return path
    cleaned = str(path or "/").strip() or "/"
    return cleaned.rstrip("/") or "/"


def _is_public_api_path(request_path: str) -> bool:
    if request_path in PUBLIC_API_PATHS:
        return True
    return any(request_path.startswith(prefix) for prefix in PUBLIC_API_PREFIXES)


def _is_public_web_path(request_path: str) -> bool:
    if request_path in {"/login", "/login/approval", "/health"}:
        return True
    if request_path.startswith("/auth/xero/"):
        return True
    if request_path.startswith("/auth/login-approval/"):
        return True
    if request_path.startswith("/device"):
        return True
    return False


def _normalise_rbac_role(user: dict | None) -> str:
    role = str((user or {}).get("role") or "").strip().lower()
    role_aliases = {
        "finance_admin": "admin",
        "client_manager": "manager",
        "readonly": "read_only",
    }
    if role in role_aliases:
        return role_aliases[role]
    if role in {"owner", "admin", "manager", "staff", "read_only"}:
        return role
    return "staff"


def _allowed_roles_for_api_path(request_path: str, request_method: str) -> set[str]:
    mode = "read" if request_method in RBAC_READ_ONLY_METHODS else "write"
    for prefix, policy in RBAC_RULES.items():
        if request_path.startswith(prefix):
            return set(policy.get(mode) or RBAC_DEFAULT_READ_ROLES)
    if mode == "read":
        return set(RBAC_DEFAULT_READ_ROLES)
    return set(RBAC_DEFAULT_WRITE_ROLES)


@app.middleware("http")
async def enforce_panel_login(request: Request, call_next):
    request_path = _normalise_path_for_guard(request.url.path)
    request_method = str(request.method or "").upper()
    if request_method == "OPTIONS":
        return await call_next(request)
    user = current_user_from_request(request)
    is_authenticated = user is not None

    if not is_authenticated:
        if request_path.startswith("/api/") and not _is_public_api_path(request_path):
            if wants_json(request):
                return JSONResponse(
                    {"detail": "Authentication required. Sign in with Xero to continue."},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )
            return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

        if request_path in PANEL_ENTRY_PATHS:
            return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

        if request_path.startswith("/static/") and request_path not in PUBLIC_STATIC_ASSETS:
            if request_path.endswith(".html") or request_path.endswith(".js"):
                return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

        if request_path.endswith(".html") and not _is_public_web_path(request_path):
            return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)

    if request_path.startswith("/api/") and not _is_public_api_path(request_path) and is_authenticated:
        allowed_roles = _allowed_roles_for_api_path(request_path, request_method)
        actor_role = _normalise_rbac_role(user)
        is_super_admin = bool((user or {}).get("is_super_admin"))
        if not is_super_admin and actor_role not in allowed_roles:
            return JSONResponse(
                {
                    "detail": (
                        f"Role '{actor_role}' is not permitted for this endpoint. "
                        f"Allowed roles: {', '.join(sorted(allowed_roles))}."
                    )
                },
                status_code=status.HTTP_403_FORBIDDEN,
            )
    return await call_next(request)


@app.middleware("http")
async def enforce_cookie_csrf_for_unsafe_methods(request: Request, call_next):
    request_path = _normalise_path_for_guard(request.url.path)
    request_method = str(request.method or "").upper()
    panel_session_cookie = request.cookies.get(COOKIE_NAME)
    if (
        panel_session_cookie
        and request_method in CSRF_UNSAFE_METHODS
        and request_path not in CSRF_EXEMPT_PATHS
    ):
        require_cookie_csrf(request)
    return await call_next(request)


logger = logging.getLogger(__name__)
BACKGROUND_JOB_MAX_WORKERS = 8
background_job_executor = ThreadPoolExecutor(
    max_workers=BACKGROUND_JOB_MAX_WORKERS,
    thread_name_prefix="panel-bg",
)
LOGIN_APPROVAL_TTL_SECONDS = 60


def _submit_background_job(name: str, target, *args, **kwargs) -> None:
    def _log_background_failure(done_future) -> None:
        try:
            done_future.result()
        except Exception:
            logger.exception("Background job failed: %s", name)

    try:
        future = background_job_executor.submit(target, *args, **kwargs)
        future.add_done_callback(_log_background_failure)
        return
    except RuntimeError as exc:
        # Deployment restarts can close the pooled executor while requests are still arriving.
        if "cannot schedule new futures after shutdown" not in str(exc).lower():
            raise
        logger.warning("Executor unavailable while queuing background job '%s'; using thread fallback", name)

    def _run_fallback_job() -> None:
        try:
            target(*args, **kwargs)
        except Exception:
            logger.exception("Background job failed (fallback thread): %s", name)

    fallback_thread = threading.Thread(
        target=_run_fallback_job,
        name=f"panel-fallback-{name}",
        daemon=True,
    )
    fallback_thread.start()


def _run_async_job(coroutine_factory, *args, **kwargs) -> None:
    asyncio.run(coroutine_factory(*args, **kwargs))


@app.on_event("startup")
def startup() -> None:
    ensure_schema()
    install_sync_signal_handlers()
    start_companies_house_auto_sync_worker()
    start_juksib_automation_worker()


def template_context(request: Request, **extra):
    return {"request": request, "user": current_user_from_request(request), **extra}


def reusable_xero_user(request: Request) -> dict | None:
    user = current_user_from_request(request)
    if not (user and user.get("id")):
        return None
    try:
        get_xero_connection_for_user(user["id"])
        return user
    except HTTPException:
        return None


def panel_session_response(user: dict) -> JSONResponse:
    session_token = create_session(user["id"], "Web panel")
    panel = {}
    panel_error = None
    active_sync_run = None
    rate_limit = None
    try:
        panel = panel_payload(user)
    except Exception as exc:
        logger.exception("Unable to build reusable panel session payload")
        panel_error = {
            "message": "The backend could not build the cached ledger panel payload.",
            "error": str(exc) or exc.__class__.__name__,
            "type": exc.__class__.__name__,
        }
        panel = {
            "organisation": {
                "name": "",
                "status": "Cached ledger unavailable",
                "lastSync": "Backend panel payload failed",
                "xeroConnected": False,
            },
            "dashboard": {
                "totalReceivables": 0,
                "totalOverdue": 0,
                "openInvoices": 0,
                "accountsNeedingAction": 0,
                "potentialInterest": 0,
            },
            "customers": [],
            "cacheStatus": {},
            "databaseMetrics": {"error": panel_error["error"]},
            "audit": [],
            "selectedInvoice": None,
        }
    try:
        active_sync_run = active_sync_run_for_user(user)
    except Exception:
        logger.exception("Unable to read active sync run for panel session")
    try:
        rate_limit = active_xero_rate_limit_for_user(user)
    except Exception:
        logger.exception("Unable to read Xero rate limit for panel session")
    response = JSONResponse(
        jsonable_encoder({
            "status": "ok",
            "sessionToken": session_token,
            **panel,
            "currentUser": serialise_current_user(user),
            "panelError": panel_error,
            "activeSyncRun": serialize_sync_run(active_sync_run) if active_sync_run else None,
            "xeroRateLimit": serialize_xero_rate_limit(rate_limit),
        })
    )
    set_session_cookie(response, session_token)
    return response


def xero_connected_redirect(request: Request, redirect_to: str) -> RedirectResponse | None:
    user = reusable_xero_user(request)
    if not user:
        return None

    session_token = create_session(user["id"], "Web panel")
    redirect_to = add_query_params(redirect_to, {"xero": "connected"})
    response = RedirectResponse(redirect_to, status_code=status.HTTP_302_FOUND)
    set_session_cookie(response, session_token)
    return response


def serialise_current_user(user: dict | None) -> dict | None:
    if not user:
        return None
    full_name = str(user.get("full_name") or user.get("name") or "").strip()
    email = str(user.get("email") or "").strip().lower()
    role = str(user.get("role") or "staff").strip().lower() or "staff"
    status_value = str(user.get("status") or "active").strip().lower() or "active"
    if not (full_name or email):
        return None
    return {
        "id": str(user.get("id") or ""),
        "fullName": full_name or email,
        "email": email,
        "role": role,
        "status": status_value,
        "isSuperAdmin": bool(user.get("is_super_admin")),
        "lastLoginAt": user.get("last_login_at"),
        "lastApprovedLoginAt": user.get("last_approved_login_at"),
    }


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return "application/json" in accept or "application/json" in content_type


def _normalise_origin(origin: str | None) -> str:
    return str(origin or "").strip().rstrip("/")


def _csrf_origin_allowed(origin: str | None) -> bool:
    candidate = _normalise_origin(origin)
    if not candidate:
        return False
    return candidate in allowed_panel_origins()


def require_cookie_csrf(request: Request) -> None:
    authorization = str(request.headers.get("authorization") or "").strip().lower()
    if authorization.startswith("bearer "):
        return
    origin = request.headers.get("origin")
    if _csrf_origin_allowed(origin):
        return
    referer = request.headers.get("referer")
    if referer:
        parts = urlsplit(referer)
        referer_origin = f"{parts.scheme}://{parts.netloc}"
        if _csrf_origin_allowed(referer_origin):
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")


def require_auth_app_client(request: Request, user: dict) -> None:
    app_client_header = str(request.headers.get("x-jenius-auth-client") or "").strip().lower()
    app_device_header = str(request.headers.get("x-jenius-auth-device") or "").strip()
    session_label = str((user or {}).get("session_label") or "").strip().lower()
    has_app_session = session_label.startswith("jenius auth")
    has_app_headers = app_client_header in {"ios", "iphone", "jenius-auth-ios"} and bool(app_device_header)
    if has_app_session or has_app_headers:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Jenius Auth iPhone app verification is required for this action.",
    )


def _security_is_admin(user: dict) -> bool:
    role = str((user or {}).get("role") or "").strip().lower()
    return bool((user or {}).get("is_super_admin")) or role in {"owner", "admin", "finance_admin"}


def require_security_admin(user: dict) -> dict:
    if _security_is_admin(user):
        return user
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to manage security settings.",
    )


def _security_founder_email() -> str:
    return "jay@jaccountancy.co.uk"


def _security_actor_is_owner_or_super_admin(user_row: dict | None) -> bool:
    role = str((user_row or {}).get("role") or "").strip().lower()
    return bool((user_row or {}).get("is_super_admin")) or role == "owner"


def _security_target_is_privileged(user_row: dict | None) -> bool:
    role = str((user_row or {}).get("role") or "").strip().lower()
    return bool((user_row or {}).get("is_super_admin")) or role == "owner"


def _security_is_founder(user_row: dict | None) -> bool:
    email = str((user_row or {}).get("email") or "").strip().lower()
    return email == _security_founder_email()


def _normalise_security_role(role_value: str | None) -> str:
    role = str(role_value or "").strip().lower()
    if role in {"owner", "admin", "manager", "staff", "read_only", "readonly"}:
        return "read_only" if role == "readonly" else role
    return "staff"


def _normalise_security_status(status_value: str | None) -> str:
    status_text = str(status_value or "").strip().lower()
    if status_text in {"active", "pending", "pending_invitation", "suspended"}:
        return status_text
    return "active"


def _security_can_manage_target(actor: dict, target: dict) -> None:
    if _security_is_founder(target) and not bool(actor.get("is_super_admin")) and str(actor.get("role") or "").strip().lower() != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The founding owner account can only be managed by Owner/Super Admin.",
        )
    if _security_target_is_privileged(target) and not _security_actor_is_owner_or_super_admin(actor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Owner/Super Admin can manage owner or super admin accounts.",
        )


def _security_assert_role_assignment_allowed(actor: dict, role: str, is_super_admin: bool) -> None:
    if (role == "owner" or is_super_admin) and not _security_actor_is_owner_or_super_admin(actor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Owner/Super Admin can assign owner or super admin permissions.",
        )


def _security_record_audit_event(
    actor: dict,
    target_user_id: str,
    event_type: str,
    payload: dict | None = None,
) -> None:
    actor_user_id = str((actor or {}).get("id") or "").strip() or None
    event_payload = payload if isinstance(payload, dict) else {}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (entity_type, entity_id, event_type, payload, user_id)
                VALUES (%s, %s, %s, %s::jsonb, %s)
                """,
                (
                    "security_user",
                    str(target_user_id or ""),
                    str(event_type or "security.user.event"),
                    json.dumps(event_payload),
                    actor_user_id,
                ),
            )
        connection.commit()


def _security_create_user(payload: dict, actor: dict) -> dict:
    email = str(payload.get("email") or "").strip()
    email_normalised = email.lower()
    full_name = str(payload.get("full_name") or payload.get("name") or "").strip()
    role = _normalise_security_role(payload.get("role"))
    status_value = _normalise_security_status(payload.get("status") or "active")
    requested_is_super_admin = bool(payload.get("is_super_admin"))
    notes = str(payload.get("notes") or "").strip()
    if not email_normalised or "@" not in email_normalised:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid email is required.")
    if not full_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A full name is required.")
    _security_assert_role_assignment_allowed(actor, role, requested_is_super_admin)
    if email_normalised == _security_founder_email():
        if not _security_actor_is_owner_or_super_admin(actor):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Owner/Super Admin can modify the founding owner account.",
            )
        role = "owner"
        status_value = "active"
        requested_is_super_admin = True
    action_type = "security.user.created"
    target_user_id = ""
    final_role = role
    final_is_super_admin = requested_is_super_admin or role == "owner"
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM users
                WHERE lower(email) = lower(%s)
                """,
                (email_normalised,),
            )
            existing = cursor.fetchone()
            if existing:
                _security_can_manage_target(actor, existing)
                is_founder = email_normalised == _security_founder_email()
                final_role = "owner" if is_founder else role
                final_is_super_admin = True if is_founder else (requested_is_super_admin or role == "owner")
                _security_assert_role_assignment_allowed(actor, final_role, final_is_super_admin)
                cursor.execute(
                    """
                    UPDATE users
                    SET
                        full_name = %s,
                        role = %s,
                        status = %s,
                        auth_method = 'xero_only',
                        two_factor_method = 'none',
                        is_super_admin = %s,
                        notes = CASE WHEN %s = '' THEN notes ELSE %s END,
                        updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (
                        full_name,
                        final_role,
                        "active" if is_founder else status_value,
                        final_is_super_admin,
                        notes,
                        notes,
                        existing["id"],
                    ),
                )
                action_type = "security.user.updated"
            else:
                _security_assert_role_assignment_allowed(actor, final_role, final_is_super_admin)
                cursor.execute(
                    """
                    INSERT INTO users (
                        email,
                        full_name,
                        role,
                        status,
                        auth_method,
                        two_factor_method,
                        is_super_admin,
                        notes,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, 'xero_only', 'none', %s, %s, NOW())
                    RETURNING *
                    """,
                    (
                        email_normalised,
                        full_name,
                        final_role,
                        status_value,
                        final_is_super_admin,
                        notes,
                    ),
                )
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to create or update user.")
            target_user_id = str(row.get("id") or "")
        connection.commit()
    if target_user_id:
        _security_record_audit_event(
            actor,
            target_user_id,
            action_type,
            {
                "email": email_normalised,
                "full_name": full_name,
                "role": final_role,
                "status": status_value if final_role != "owner" else "active",
                "is_super_admin": bool(final_is_super_admin),
            },
        )
    return row


def _security_change_user_status(target_user_id: str, status_value: str, actor: dict) -> dict:
    actor_id = str((actor or {}).get("id") or "").strip()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id = %s", (target_user_id,))
            target = cursor.fetchone()
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
            if actor_id and actor_id == str(target.get("id") or ""):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot change your own account status.")
            _security_can_manage_target(actor, target)
            previous_status = str(target.get("status") or "active").strip().lower()
            next_status = _normalise_security_status(status_value)
            cursor.execute(
                """
                UPDATE users
                SET status = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (next_status, target_user_id),
            )
            row = cursor.fetchone()
        connection.commit()
    _security_record_audit_event(
        actor,
        target_user_id,
        "security.user.status_changed",
        {"from": previous_status, "to": next_status},
    )
    return row


def _security_force_logout(target_user_id: str, actor: dict) -> None:
    actor_id = str((actor or {}).get("id") or "").strip()
    deleted_sessions = 0
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id = %s", (target_user_id,))
            target = cursor.fetchone()
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
            if actor_id and actor_id == str(target.get("id") or ""):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot force logout your own account.")
            _security_can_manage_target(actor, target)
            cursor.execute("DELETE FROM sessions WHERE user_id = %s", (target_user_id,))
            deleted_sessions = int(cursor.rowcount or 0)
        connection.commit()
    _security_record_audit_event(
        actor,
        target_user_id,
        "security.user.force_logout",
        {"deleted_sessions": deleted_sessions},
    )


def _security_delete_user(target_user_id: str, actor: dict) -> None:
    actor_id = str((actor or {}).get("id") or "").strip()
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id = %s", (target_user_id,))
            target = cursor.fetchone()
            if target is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
            if actor_id and actor_id == str(target.get("id") or ""):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot remove your own account.")
            if _security_is_founder(target):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="The founding owner account cannot be removed.")
            _security_can_manage_target(actor, target)
            deleted_email = str(target.get("email") or "").strip().lower()
            deleted_role = str(target.get("role") or "").strip().lower()
            cursor.execute("DELETE FROM users WHERE id = %s", (target_user_id,))
        connection.commit()
    _security_record_audit_event(
        actor,
        target_user_id,
        "security.user.deleted",
        {"email": deleted_email, "role": deleted_role},
    )


def companies_house_bulk_submission_error_detail(exc: Exception) -> dict:
    error_text = str(exc) or exc.__class__.__name__
    lowered = error_text.lower()
    detail = {
        "message": "Unexpected server error while processing Companies House bulk submission.",
        "error": error_text,
        "type": exc.__class__.__name__,
    }
    if "on conflict specification" in lowered and "no unique or exclusion constraint" in lowered:
        detail["message"] = (
            "Companies House bulk submission failed before dispatch because the backend database schema "
            "does not match the expected idempotency conflict rule."
        )
        detail["code"] = "CH_SUBMISSION_SCHEMA_MISMATCH"
        detail["hint"] = (
            "Deploy/restart the backend so startup schema updates are applied, then retry bulk submission."
        )
    return detail


def _security_users_payload() -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    email,
                    full_name,
                    role,
                    status,
                    auth_method,
                    two_factor_method,
                    is_super_admin,
                    notes,
                    xero_user_id,
                    created_at,
                    updated_at,
                    last_login_at,
                    last_approved_login_at
                FROM users
                ORDER BY
                    CASE
                        WHEN lower(role) = 'owner' OR is_super_admin THEN 0
                        WHEN lower(status) = 'active' THEN 1
                        WHEN lower(status) IN ('pending', 'pending_invitation', 'pending_approval', 'invited') THEN 2
                        ELSE 3
                    END,
                    COALESCE(last_login_at, created_at) DESC
                """
            )
            users = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT
                    s.user_id,
                    MAX(s.last_seen_at) AS last_seen_at,
                    MAX(s.created_at) AS session_created_at,
                    COUNT(*) FILTER (WHERE s.expires_at > NOW()) AS active_sessions
                FROM sessions s
                GROUP BY s.user_id
                """
            )
            session_rows = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT
                    user_id,
                    COUNT(*) FILTER (WHERE status = 'pending' AND expires_at > NOW()) AS pending_device_requests
                FROM device_logins
                WHERE user_id IS NOT NULL
                GROUP BY user_id
                """
            )
            device_rows = cursor.fetchall() or []
        connection.commit()

    sessions_by_user_id = {str(row.get("user_id") or ""): row for row in session_rows}
    device_by_user_id = {str(row.get("user_id") or ""): row for row in device_rows}

    users_out: list[dict] = []
    counts = {
        "activeUsers": 0,
        "pendingInvites": 0,
        "suspendedUsers": 0,
        "ownerUsers": 0,
    }

    for row in users:
        user_id = str(row.get("id") or "")
        status_value = str(row.get("status") or "active").strip().lower()
        if status_value == "active":
            counts["activeUsers"] += 1
        elif status_value in {"pending", "pending_invitation", "pending_approval", "invited"}:
            counts["pendingInvites"] += 1
        elif status_value in {"suspended", "disabled", "inactive"}:
            counts["suspendedUsers"] += 1
        if str(row.get("role") or "").strip().lower() == "owner" or bool(row.get("is_super_admin")):
            counts["ownerUsers"] += 1

        session_row = sessions_by_user_id.get(user_id, {})
        device_row = device_by_user_id.get(user_id, {})
        users_out.append(
            {
                **row,
                "active_sessions": int(session_row.get("active_sessions") or 0),
                "last_seen_at": session_row.get("last_seen_at"),
                "session_created_at": session_row.get("session_created_at"),
                "pending_device_requests": int(device_row.get("pending_device_requests") or 0),
            }
        )

    return {
        "summary": counts,
        "users": users_out,
    }


def _security_audit_payload(limit: int = 120) -> dict:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    s.id,
                    s.user_id,
                    u.email,
                    u.full_name,
                    s.label,
                    s.created_at,
                    s.last_seen_at,
                    s.expires_at
                FROM sessions s
                LEFT JOIN users u ON u.id = s.user_id
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            session_events = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT
                    d.id,
                    d.user_id,
                    u.email,
                    u.full_name,
                    d.status,
                    d.created_at,
                    d.completed_at,
                    d.expires_at
                FROM device_logins d
                LEFT JOIN users u ON u.id = d.user_id
                ORDER BY d.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            device_events = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT
                    la.id,
                    la.status,
                    la.requested_from,
                    la.requested_ip,
                    la.requested_at,
                    la.expires_at,
                    la.approved_at,
                    la.denied_at,
                    u.email,
                    u.full_name
                FROM login_approval_attempts la
                LEFT JOIN users u ON u.id = la.user_id
                ORDER BY la.requested_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            app_approval_events = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT
                    ae.id,
                    ae.entity_id,
                    ae.event_type,
                    ae.payload,
                    ae.created_at,
                    actor.email AS actor_email,
                    actor.full_name AS actor_full_name
                FROM audit_events ae
                LEFT JOIN users actor ON actor.id = ae.user_id
                WHERE ae.entity_type = 'security_user'
                ORDER BY ae.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            management_events = cursor.fetchall() or []
        connection.commit()

    audit_events: list[dict] = []
    for row in session_events:
        audit_events.append(
            {
                "id": f"session:{row['id']}",
                "type": "xero_login_session",
                "status": "success",
                "email": row.get("email") or "",
                "full_name": row.get("full_name") or "",
                "label": row.get("label") or "",
                "created_at": row.get("created_at"),
                "completed_at": row.get("last_seen_at"),
                "expires_at": row.get("expires_at"),
            }
        )
    for row in device_events:
        status_value = str(row.get("status") or "").strip().lower()
        audit_events.append(
            {
                "id": f"device:{row['id']}",
                "type": "device_login_approval",
                "status": status_value or "pending",
                "email": row.get("email") or "",
                "full_name": row.get("full_name") or "",
                "label": "Jenius Auth device approval",
                "created_at": row.get("created_at"),
                "completed_at": row.get("completed_at"),
                "expires_at": row.get("expires_at"),
            }
        )
    for row in app_approval_events:
        status_value = str(row.get("status") or "").strip().lower()
        completed_at = row.get("approved_at") or row.get("denied_at")
        audit_events.append(
            {
                "id": f"login-approval:{row['id']}",
                "type": "jenius_auth_login_approval",
                "status": status_value or "pending",
                "email": row.get("email") or "",
                "full_name": row.get("full_name") or "",
                "label": f"{row.get('requested_from') or 'Unknown device'} · {_mask_ip(row.get('requested_ip') or '')}",
                "created_at": row.get("requested_at"),
                "completed_at": completed_at,
                "expires_at": row.get("expires_at"),
            }
        )
    for row in management_events:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        status_value = str(payload.get("to") or payload.get("status") or "success").strip().lower()
        actor_label = str(row.get("actor_full_name") or row.get("actor_email") or "").strip()
        target_label = str(payload.get("email") or payload.get("full_name") or row.get("entity_id") or "").strip()
        audit_events.append(
            {
                "id": f"security-user:{row['id']}",
                "type": "security_user_management",
                "status": status_value or "success",
                "email": actor_label,
                "full_name": actor_label,
                "label": f"{row.get('event_type') or 'security.user.event'} · {target_label}".strip(" ·"),
                "created_at": row.get("created_at"),
                "completed_at": row.get("created_at"),
                "expires_at": None,
            }
        )

    audit_events.sort(key=lambda event: str(event.get("created_at") or ""), reverse=True)
    return {"events": audit_events[:limit]}


def xero_login_error_response(
    message: str,
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
    provider: str = "Xero",
    action_href: str = "/login",
    action_label: str = "Back to login",
) -> HTMLResponse:
    safe_message = escape(message)
    safe_provider = escape(provider)
    safe_action_href = escape(action_href, quote=True)
    safe_action_label = escape(action_label)
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{safe_provider} connection failed</title>
            <style>
                body {{
                    margin: 0;
                    min-height: 100vh;
                    display: grid;
                    place-items: center;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f5f8ff;
                    color: #1e2f4d;
                }}
                main {{
                    width: min(560px, calc(100vw - 40px));
                    padding: 32px;
                    border-radius: 20px;
                    background: #fff;
                    box-shadow: 0 18px 60px rgba(41, 79, 148, 0.14);
                }}
                h1 {{ margin: 0 0 12px; font-size: 28px; }}
                p {{ margin: 0 0 22px; color: #65738e; line-height: 1.55; }}
                a {{
                    display: inline-flex;
                    padding: 12px 18px;
                    border-radius: 999px;
                    color: #fff;
                    background: #1d67f2;
                    text-decoration: none;
                    font-weight: 700;
                }}
            </style>
        </head>
        <body>
            <main>
                <h1>{safe_provider} connection failed</h1>
                <p>{safe_message}</p>
                <a href="{safe_action_href}">{safe_action_label}</a>
            </main>
        </body>
        </html>
        """,
        status_code=status_code,
    )


def add_query_params(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url or "/")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def add_fragment_params(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url or "/")
    fragment = dict(parse_qsl(parts.fragment, keep_blank_values=True))
    fragment.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, urlencode(fragment)))


def normalise_oauth_redirect(url: str | None) -> str:
    candidate = url or "/"
    parts = urlsplit(candidate)
    if not parts.scheme and not parts.netloc:
        return candidate
    origin = f"{parts.scheme}://{parts.netloc}".rstrip("/")
    return candidate if origin in allowed_panel_origins() else "/"


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": get_settings().app_env}


def _login_role_label(user: dict | None) -> str:
    role = str((user or {}).get("role") or "").strip().lower()
    if role in {"admin", "practice_admin", "owner"}:
        return "Practice admin"
    if role in {"manager", "team_lead"}:
        return "Team lead"
    if role in {"accountant", "analyst"}:
        return "Analyst"
    if role in {"staff", "member"}:
        return "Team member"
    return "Team member"


def _login_landing_payload(request: Request) -> dict:
    settings = get_settings()
    user = current_user_from_request(request)
    viewer_name = str((user or {}).get("full_name") or (user or {}).get("name") or "").strip()
    role_label = _login_role_label(user) if user else ""
    xero_configured = bool(settings.xero_client_id and settings.xero_client_secret and settings.xero_redirect_uri)
    gmail_configured = gmail_oauth_configured()

    services = {
        "xero": {"label": "Xero", "state": "setup", "detail": "Connect your account to continue."},
        "gmail": {"label": "Gmail", "state": "setup", "detail": "Connect to send and schedule updates."},
        "hmrc": {"label": "HMRC", "state": "setup", "detail": "Connect to enable filing workflows."},
    }
    if not xero_configured:
        services["xero"] = {"label": "Xero", "state": "unavailable", "detail": "OAuth is not configured in this environment."}
    if not gmail_configured:
        services["gmail"] = {"label": "Gmail", "state": "unavailable", "detail": "OAuth is not configured in this environment."}

    if user and user.get("id"):
        try:
            connection = get_xero_connection_for_user(user["id"])
            tenant_name = str(connection.get("tenant_name") or "").strip()
            services["xero"] = {
                "label": "Xero",
                "state": "connected",
                "detail": f"Connected to {tenant_name}." if tenant_name else "Connected and ready.",
            }
        except HTTPException:
            if xero_configured:
                services["xero"] = {"label": "Xero", "state": "disconnected", "detail": "Not connected for this user."}

        gmail_connected = False
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT 1
                    FROM gmail_connections
                    WHERE user_id = %s
                      AND status = 'connected'
                    LIMIT 1
                    """,
                    (user["id"],),
                )
                gmail_connected = bool(cursor.fetchone())
            connection.commit()
        if gmail_configured:
            services["gmail"] = {
                "label": "Gmail",
                "state": "connected" if gmail_connected else "disconnected",
                "detail": "Connected and ready." if gmail_connected else "Not connected for this user.",
            }

        hmrc_status = hmrc_mtd_oauth_status(user)
        hmrc_connected = bool(hmrc_status.get("connected"))
        hmrc_configured = bool(hmrc_status.get("configured"))
        services["hmrc"] = {
            "label": "HMRC",
            "state": "connected" if hmrc_connected else ("disconnected" if hmrc_configured else "unavailable"),
            "detail": "Connected and ready." if hmrc_connected else ("Not connected for this user." if hmrc_configured else "OAuth is not configured in this environment."),
        }

    active_users = None
    connected_workspaces = None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM users WHERE COALESCE(status, 'active') = 'active'")
            row = cursor.fetchone() or {}
            active_users = int(row.get("count") or 0)

            cursor.execute("SELECT COUNT(*) AS count FROM xero_connections")
            row = cursor.fetchone() or {}
            connected_workspaces = int(row.get("count") or 0)
        connection.commit()

    return {
        "status": "ok",
        "viewer": {
            "name": viewer_name,
            "roleLabel": role_label,
        },
        "services": services,
        "proof": {
            "activeUsers": active_users,
            "connectedWorkspaces": connected_workspaces,
            "automations": 12,
        },
        "metrics": {
            "integrations": 4,
            "availability": "24/7",
            "consoleCount": 1,
        },
        "updates": [
            {
                "date": "June 17, 2026",
                "title": "Landing refresh",
                "summary": "Improved sign-in UX, trust surfaces, and mobile readability.",
            },
            {
                "date": "June 14, 2026",
                "title": "Workflow upgrades",
                "summary": "Expanded Companies House and ledger task visibility.",
            },
            {
                "date": "June 8, 2026",
                "title": "Security hardening",
                "summary": "Strengthened role checks and session controls.",
            },
        ],
    }


@app.get("/api/login/landing")
def api_login_landing(request: Request):
    return _login_landing_payload(request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = current_user_from_request(request)
    if user and user.get("id"):
        response = xero_connected_redirect(request, "/")
        if response:
            return response
    viewer_name = str((user or {}).get("full_name") or (user or {}).get("name") or "").strip()
    first_name = viewer_name.split(" ", 1)[0] if viewer_name else ""
    return templates.TemplateResponse(
        request,
        "login.html",
        template_context(
            request,
            login_welcome_name=first_name,
            login_role_label=_login_role_label(user) if user else "",
        ),
    )


@app.get("/login/approval", response_class=HTMLResponse)
def login_approval_page(request: Request, token: str = Query("")):
    _ = token
    if current_user_from_request(request):
        return RedirectResponse("/", status_code=status.HTTP_302_FOUND)
    return RedirectResponse("/login", status_code=status.HTTP_302_FOUND)


@app.get("/auth/xero/start")
def auth_xero_start(
    request: Request,
    redirect_to: str = "/",
    force: int = 0,
    include_payroll: int = 0,
    include_all_scopes: int = 1,
):
    redirect_to = normalise_oauth_redirect(redirect_to)
    if not force:
        response = xero_connected_redirect(request, redirect_to)
        if response:
            return response
    state_token = start_oauth_state(redirect_to=redirect_to)
    settings = get_settings()
    request_all_scopes = bool(include_all_scopes)
    request_payroll_scopes = bool(include_payroll) and (
        bool(settings.xero_enable_payroll_scopes)
        or configured_xero_scopes_include_payroll(settings.xero_scopes)
    )
    if request_all_scopes:
        request_payroll_scopes = True
    return RedirectResponse(
        xero_authorize_url(
            state_token,
            prompt_consent=bool(force),
            include_payroll_scopes=request_payroll_scopes,
            include_all_scopes=request_all_scopes,
        ),
        status_code=status.HTTP_302_FOUND,
    )


@app.get("/auth/xero/connected")
def auth_xero_connected():
    return RedirectResponse(add_query_params("/", {"xero": "connected"}), status_code=status.HTTP_302_FOUND)


@app.get("/auth/gmail/start")
def auth_gmail_start(redirect_to: str = "/", user: dict = Depends(require_panel_user)):
    redirect_to = normalise_oauth_redirect(redirect_to)
    if not gmail_oauth_configured():
        return xero_login_error_response("Gmail OAuth is not configured. Add GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REDIRECT_URI before connecting Gmail.", status.HTTP_500_INTERNAL_SERVER_ERROR, provider="Gmail")
    state_token = start_oauth_state(redirect_to=redirect_to, user_id=user["id"], provider="gmail")
    return RedirectResponse(gmail_authorize_url(state_token), status_code=status.HTTP_302_FOUND)


@app.get("/auth/gmail/callback")
async def auth_gmail_callback(request: Request, code: str, state: str):
    try:
        state_row = consume_oauth_state(state)
        if state_row.get("provider") not in ("gmail",):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth state was not created for Gmail.")
        user = current_user_or_oauth_state_user(request, state_row)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in before connecting Gmail.")
        token_payload = await exchange_gmail_code_for_tokens(code)
        profile = await fetch_gmail_profile(token_payload["access_token"])
        store_gmail_connection(user, token_payload, profile)
        redirect_to = normalise_oauth_redirect(state_row["redirect_to"] or "/")
        return RedirectResponse(add_query_params(redirect_to, {"gmail": "connected"}), status_code=status.HTTP_302_FOUND)
    except HTTPException as exc:
        logger.warning("Gmail callback failed: %s", exc.detail)
        return xero_login_error_response(str(exc.detail), exc.status_code, provider="Gmail")
    except Exception:
        logger.exception("Unhandled Gmail callback failure")
        return xero_login_error_response("An unexpected server error occurred while completing the Gmail connection.", provider="Gmail")


def queue_ignition_sync(user: dict) -> tuple[dict | None, bool]:
    try:
        sync_run, started = request_ignition_sync_run(user)
        if started:
            _submit_background_job("ignition_sync", run_ignition_sync_job, dict(user), str(sync_run["id"]))
        return sync_run, started
    except Exception:
        logger.exception("Unable to queue Ignition sync")
        return None, False


def current_user_or_oauth_state_user(request: Request, state_row: dict) -> dict | None:
    user = current_user_from_request(request)
    if user:
        return user

    user_id = state_row.get("user_id")
    if not user_id:
        return None

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            row = cursor.fetchone()
        connection.commit()
    return row


def build_ignition_authorize_url(user: dict, redirect_to: str) -> str:
    redirect_to = normalise_oauth_redirect(redirect_to)
    verifier = create_pkce_verifier()
    state_token = start_oauth_state(
        redirect_to=redirect_to,
        user_id=user["id"],
        provider="ignition",
        code_verifier=verifier,
    )
    return ignition_authorize_url(state_token, verifier)


def recover_ignition_state_error(request: Request, message: str, status_code: int) -> HTMLResponse | RedirectResponse:
    user = current_user_from_request(request)
    if user:
        try:
            authorize_url = build_ignition_authorize_url(user, "/#credit-control-ignition")
        except IgnitionConfigurationError as exc:
            return xero_login_error_response(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR, provider="Ignition")
        return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)

    return xero_login_error_response(
        f"{message} Sign in again, then start Ignition from the panel.",
        status_code,
        provider="Ignition",
        action_href="/login",
        action_label="Sign in again",
    )


@app.get("/auth/ignition/start")
def auth_ignition_start(redirect_to: str = "/", user: dict = Depends(require_panel_user)):
    try:
        authorize_url = build_ignition_authorize_url(user, redirect_to)
    except IgnitionConfigurationError as exc:
        return xero_login_error_response(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR, provider="Ignition")
    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@app.get("/auth/barclays/start")
def auth_barclays_start(redirect_to: str = "/", user: dict = Depends(require_panel_user)):
    redirect_to = normalise_oauth_redirect(redirect_to)
    authorize_url = build_barclays_authorize_url(user, redirect_to)
    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@app.get("/api/barclays/connect")
def api_barclays_connect(request: Request, redirect_to: str = "/", user: dict = Depends(require_panel_user)):
    redirect_to = normalise_oauth_redirect(redirect_to)
    authorize_url = build_barclays_authorize_url(user, redirect_to)
    if wants_json(request):
        return {"status": "ok", "authorizationUrl": authorize_url}
    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@app.get("/auth/barclays/callback")
@app.get("/api/barclays/callback")
async def auth_barclays_callback(request: Request, code: str, state: str):
    try:
        state_row = consume_oauth_state(state)
        if state_row.get("provider") not in ("barclays",):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth state was not created for Barclays.")
        user = current_user_or_oauth_state_user(request, state_row)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in before connecting Barclays.")
        await complete_barclays_oauth_callback(user, code)
        redirect_to = normalise_oauth_redirect(state_row["redirect_to"] or "/")
        return RedirectResponse(add_query_params(redirect_to, {"barclays": "connected"}), status_code=status.HTTP_302_FOUND)
    except HTTPException as exc:
        logger.warning("Barclays callback failed: %s", exc.detail)
        return xero_login_error_response(str(exc.detail), exc.status_code, provider="Barclays")
    except Exception:
        logger.exception("Unhandled Barclays callback failure")
        return xero_login_error_response("An unexpected server error occurred while completing the Barclays connection.", provider="Barclays")


@app.get("/api/barclays/status")
def api_barclays_status(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "barclays": barclays_connect_status_payload(user)}


@app.get("/api/ignition/connect")
def api_ignition_connect(request: Request, redirect_to: str = "/", user: dict = Depends(require_panel_user)):
    try:
        authorize_url = build_ignition_authorize_url(user, redirect_to)
    except IgnitionConfigurationError as exc:
        if wants_json(request):
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail={"message": str(exc)}) from exc
        return xero_login_error_response(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR, provider="Ignition")

    if wants_json(request):
        return {"status": "ok", "authorizationUrl": authorize_url}
    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@app.get("/api/ignition/callback")
@app.get("/auth/ignition/callback")
async def auth_ignition_callback(request: Request, code: str, state: str):
    try:
        state_row = consume_oauth_state(state)
        if state_row.get("provider") not in ("ignition",):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Ignition OAuth state.")
        user = current_user_or_oauth_state_user(request, state_row)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to Jenius before connecting Ignition.")
        token_payload = await exchange_ignition_code_for_tokens(code, state_row.get("code_verifier") or "")
        store_ignition_connection(user, token_payload)
        sync_run, sync_started = queue_ignition_sync(user)
        redirect_to = normalise_oauth_redirect(state_row["redirect_to"] or "/")
        redirect_params = {"ignition": "connected"}
        if sync_run:
            redirect_params["ignition_sync_run"] = str(sync_run["id"])
            redirect_params["ignition_sync_started"] = "1" if sync_started else "0"
        return RedirectResponse(add_query_params(redirect_to, redirect_params), status_code=status.HTTP_302_FOUND)
    except HTTPException as exc:
        logger.warning("Ignition callback failed: %s", exc.detail)
        detail = exc.detail
        message = str(detail.get("message") if isinstance(detail, dict) else detail)
        if exc.status_code == status.HTTP_400_BAD_REQUEST and "state" in message.lower():
            return recover_ignition_state_error(request, message, exc.status_code)
        return xero_login_error_response(message, exc.status_code, provider="Ignition")
    except IgnitionConfigurationError as exc:
        logger.warning("Ignition callback failed: %s", exc)
        return xero_login_error_response(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR, provider="Ignition")
    except Exception:
        logger.exception("Unhandled Ignition callback failure")
        return xero_login_error_response("An unexpected server error occurred while completing the Ignition connection.", provider="Ignition")


def _request_ip_address(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return str(request.client.host).strip()
    return ""


def _request_device_label(request: Request) -> str:
    user_agent = str(request.headers.get("user-agent") or "").strip()
    if user_agent:
        return user_agent[:220]
    return "Unknown device"


def _mask_ip(ip_value: str) -> str:
    value = str(ip_value or "").strip()
    if not value:
        return "Unknown location"
    if ":" in value:
        return f"Approx IPv6 ({value[:6]}...)"
    parts = value.split(".")
    if len(parts) == 4:
        return f"Approx IP {parts[0]}.{parts[1]}.x.x"
    return f"Approx IP {value}"


def _format_approval_row(row: dict) -> dict:
    status_value = str(row.get("status") or "pending").strip().lower()
    return {
        "id": row.get("id"),
        "status": status_value,
        "requestedFrom": row.get("requested_from") or "Unknown device",
        "requestedIp": row.get("requested_ip") or "",
        "locationHint": _mask_ip(row.get("requested_ip") or ""),
        "requestedAt": row.get("requested_at"),
        "expiresAt": row.get("expires_at"),
        "approvedAt": row.get("approved_at"),
        "deniedAt": row.get("denied_at"),
    }


def _require_xero_identity_user(user: dict) -> dict:
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    auth_method = str(user.get("auth_method") or "").strip().lower()
    if auth_method not in {"xero_only", "xero"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is not configured for Xero authentication.")
    if not str(user.get("xero_user_id") or "").strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Xero identity is required before approving login requests in Jenius Auth.",
        )
    return user


def _mark_auth_app_presence(user: dict) -> None:
    user_id = str((user or {}).get("id") or "").strip()
    if not user_id:
        return
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET
                    auth_app_enrolled_at = COALESCE(auth_app_enrolled_at, NOW()),
                    auth_app_last_seen_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (user_id,),
            )
        connection.commit()


def _create_login_approval_attempt(user: dict, request: Request) -> dict:
    approval_token = f"{uuid4().hex}{uuid4().hex}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=LOGIN_APPROVAL_TTL_SECONDS)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO login_approval_attempts (
                    approval_token,
                    user_id,
                    status,
                    requested_from,
                    requested_ip,
                    requested_at,
                    expires_at
                )
                VALUES (%s, %s, 'pending', %s, %s, NOW(), %s)
                RETURNING *
                """,
                (
                    approval_token,
                    user["id"],
                    _request_device_label(request),
                    _request_ip_address(request),
                    expires_at,
                ),
            )
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to create login approval attempt.")
    return row


def _expire_approval_attempt_if_due(approval_token: str) -> dict | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET status = CASE
                    WHEN status = 'pending' AND expires_at <= NOW() THEN 'expired'
                    ELSE status
                END
                WHERE approval_token = %s
                RETURNING *
                """,
                (approval_token,),
            )
            row = cursor.fetchone()
        connection.commit()
    return row


def _queue_initial_sync_for_user_id(user_id: str) -> tuple[dict | None, bool]:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            user = cursor.fetchone()
        connection.commit()
    if not user:
        return None, False
    return queue_initial_xero_sync(user)


def queue_initial_xero_sync(user: dict) -> tuple[dict | None, bool]:
    try:
        sync_run, started = request_sync_run(user)
        if started:
            _submit_background_job("initial_xero_sync", run_sync_job, dict(user), str(sync_run["id"]))
        return sync_run, started
    except Exception as exc:
        logger.exception("Unable to queue initial Xero sync after login")
        record_sync_start_failure(user, exc)
        return None, False


@app.get("/auth/xero/callback")
async def auth_xero_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    try:
        if error:
            message = f"Xero authorisation failed: {error}."
            description_text = str(error_description or "").strip()
            if description_text:
                message = f"{message} {description_text}"
            if str(error).strip().lower() == "invalid_scope":
                message = (
                    "Xero authorisation failed because an invalid scope was requested. "
                    "Disable payroll scopes (or only request them when your Xero app is approved for payroll), then retry."
                )
            return xero_login_error_response(message, status.HTTP_400_BAD_REQUEST)
        if not code or not state:
            return xero_login_error_response(
                "Xero authorisation did not return a valid code/state pair. Please retry the connection.",
                status.HTTP_400_BAD_REQUEST,
            )
        state_row = consume_oauth_state(state)
        token_payload = await exchange_code_for_tokens(code)
        profile = await fetch_user_profile(token_payload["access_token"])
        connections = await fetch_connections(token_payload["access_token"])
        login = store_login(profile, token_payload, connections)

        if state_row["device_code"]:
            session_token = approve_device_code_by_state(state_row["device_code"], login["user"]["id"])
            response = RedirectResponse("/device/complete?approved=1", status_code=status.HTTP_302_FOUND)
            set_session_cookie(response, session_token)
            return response

        user_id = str(login["user"]["id"])
        session_token = create_session(user_id, "Web panel")
        sync_run, sync_started = _queue_initial_sync_for_user_id(user_id)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE users
                    SET last_approved_login_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (user_id,),
                )
            connection.commit()

        redirect_to = normalise_oauth_redirect(state_row.get("redirect_to") or "/")
        redirect_to = add_query_params(redirect_to, {"xero": "connected"})
        if sync_run:
            redirect_to = add_query_params(
                redirect_to,
                {
                    "sync_run": str(sync_run["id"]),
                    "sync_started": "1" if sync_started else "0",
                },
            )
        response = RedirectResponse(redirect_to, status_code=status.HTTP_302_FOUND)
        set_session_cookie(response, session_token)
        return response
    except HTTPException as exc:
        logger.warning("Xero callback failed: %s", exc.detail)
        detail = exc.detail
        if isinstance(detail, dict):
            message = str(detail.get("message") or detail)
        else:
            message = str(detail)
        return xero_login_error_response(message, exc.status_code)
    except Exception:
        logger.exception("Unhandled Xero callback failure")
        return xero_login_error_response(
            "An unexpected server error occurred while completing the Xero connection. Check the Railway deploy logs for the full traceback.",
        )


@app.get("/api/auth/login-approval/status")
def api_login_approval_status(token: str = Query(..., min_length=16)):
    attempt = _expire_approval_attempt_if_due(token)
    if attempt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Login approval attempt not found.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET last_polled_at = NOW()
                WHERE id = %s
                """,
                (attempt["id"],),
            )
        connection.commit()
    formatted = _format_approval_row(attempt)
    return {
        "status": str(attempt.get("status") or "pending").lower(),
        "expiresAt": attempt.get("expires_at"),
        "requestedAt": attempt.get("requested_at"),
        "requestedFrom": formatted.get("requestedFrom"),
        "locationHint": formatted.get("locationHint"),
        "completeUrl": "/api/auth/login-approval/complete",
        "completeMethod": "POST",
    }


@app.post("/api/auth/login-approval/complete")
async def api_login_approval_complete(request: Request):
    require_cookie_csrf(request)
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    token = str(payload.get("token") or "").strip()
    if len(token) < 16:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Approval token is required.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET status = 'consumed'
                WHERE approval_token = %s
                  AND status = 'approved'
                RETURNING user_id
                """,
                (token,),
            )
            consumed_row = cursor.fetchone()
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET status = 'expired'
                WHERE approval_token = %s
                  AND status = 'pending'
                  AND expires_at <= NOW()
                RETURNING status
                """,
                (token,),
            )
            cursor.execute(
                """
                SELECT status
                FROM login_approval_attempts
                WHERE approval_token = %s
                """,
                (token,),
            )
            status_row = cursor.fetchone()
        connection.commit()

    if status_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Login approval attempt not found.")

    current_status = str(status_row.get("status") or "pending").strip().lower()
    if consumed_row:
        user_id = str(consumed_row["user_id"])
        session_token = create_session(user_id, "Web panel")
        sync_run, sync_started = _queue_initial_sync_for_user_id(user_id)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE users
                    SET last_approved_login_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (user_id,),
                )
            connection.commit()
        redirect_to = "/"
        redirect_to = add_query_params(redirect_to, {"xero": "connected", "approval": "approved"})
        if sync_run:
            redirect_to = add_query_params(
                redirect_to,
                {
                    "sync_run": str(sync_run["id"]),
                    "sync_started": "1" if sync_started else "0",
                },
            )
        response = JSONResponse({"status": "approved", "redirectUrl": redirect_to})
        set_session_cookie(response, session_token)
        return response

    if current_status == "consumed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This login approval has already been completed.")
    if current_status == "denied":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Login request was denied in Jenius Auth.")
    if current_status == "expired":
        raise HTTPException(status_code=status.HTTP_408_REQUEST_TIMEOUT, detail="Login approval timed out after 60 seconds.")
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Login approval is still pending.")


@app.post("/api/auth/login-approval/resend")
async def api_login_approval_resend(request: Request):
    require_cookie_csrf(request)
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    token = str(payload.get("token") or "").strip()
    if len(token) < 16:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Approval token is required.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET
                    requested_at = NOW(),
                    expires_at = NOW() + (%s || ' seconds')::interval
                WHERE approval_token = %s
                  AND status = 'pending'
                RETURNING *
                """,
                (str(LOGIN_APPROVAL_TTL_SECONDS), token),
            )
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        attempt = _expire_approval_attempt_if_due(token)
        if attempt is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Login approval attempt not found.")
        return {"status": str(attempt.get("status") or "pending").lower(), "expiresAt": attempt.get("expires_at")}
    return {"status": "pending", "expiresAt": row.get("expires_at")}


@app.get("/auth/login-approval/complete")
def auth_login_approval_complete(token: str = Query(..., min_length=16)):
    # Legacy compatibility route. Completion is now POST-only to prevent repeated
    # session minting via bookmarked or replayed URLs.
    return RedirectResponse(add_query_params("/login/approval", {"token": token}), status_code=status.HTTP_302_FOUND)


@app.get("/api/auth/login-approval/pending")
def api_login_approval_pending(request: Request, user: dict = Depends(require_panel_user)):
    user = _require_xero_identity_user(user)
    require_auth_app_client(request, user)
    _mark_auth_app_presence(user)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET status = 'expired'
                WHERE user_id = %s
                  AND status = 'pending'
                  AND expires_at <= NOW()
                """,
                (user["id"],),
            )
            cursor.execute(
                """
                SELECT
                    id,
                    status,
                    requested_from,
                    requested_ip,
                    requested_at,
                    expires_at,
                    approved_at,
                    denied_at
                FROM login_approval_attempts
                WHERE user_id = %s
                  AND status = 'pending'
                ORDER BY requested_at DESC
                LIMIT 20
                """,
                (user["id"],),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return {"items": [_format_approval_row(row) for row in rows]}


@app.get("/api/auth/login-approval/history")
def api_login_approval_history(
    request: Request,
    limit: int = Query(30, ge=1, le=200),
    user: dict = Depends(require_panel_user),
):
    user = _require_xero_identity_user(user)
    require_auth_app_client(request, user)
    _mark_auth_app_presence(user)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET status = 'expired'
                WHERE user_id = %s
                  AND status = 'pending'
                  AND expires_at <= NOW()
                """,
                (user["id"],),
            )
            cursor.execute(
                """
                SELECT
                    id,
                    status,
                    requested_from,
                    requested_ip,
                    requested_at,
                    expires_at,
                    approved_at,
                    denied_at
                FROM login_approval_attempts
                WHERE user_id = %s
                ORDER BY requested_at DESC
                LIMIT %s
                """,
                (user["id"], limit),
            )
            rows = cursor.fetchall() or []
        connection.commit()
    return {"items": [_format_approval_row(row) for row in rows]}


@app.get("/api/auth/login-approval/inbox")
def api_login_approval_inbox(
    request: Request,
    pending_limit: int = Query(20, ge=1, le=100),
    history_limit: int = Query(40, ge=1, le=200),
    user: dict = Depends(require_panel_user),
):
    user = _require_xero_identity_user(user)
    require_auth_app_client(request, user)
    _mark_auth_app_presence(user)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET status = 'expired'
                WHERE user_id = %s
                  AND status = 'pending'
                  AND expires_at <= NOW()
                """,
                (user["id"],),
            )
            cursor.execute(
                """
                SELECT
                    id,
                    status,
                    requested_from,
                    requested_ip,
                    requested_at,
                    expires_at,
                    approved_at,
                    denied_at
                FROM login_approval_attempts
                WHERE user_id = %s
                  AND status = 'pending'
                ORDER BY requested_at DESC
                LIMIT %s
                """,
                (user["id"], pending_limit),
            )
            pending_rows = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT
                    id,
                    status,
                    requested_from,
                    requested_ip,
                    requested_at,
                    expires_at,
                    approved_at,
                    denied_at
                FROM login_approval_attempts
                WHERE user_id = %s
                ORDER BY requested_at DESC
                LIMIT %s
                """,
                (user["id"], history_limit),
            )
            history_rows = cursor.fetchall() or []
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending_count,
                    COUNT(*) FILTER (WHERE status = 'approved') AS approved_count,
                    COUNT(*) FILTER (WHERE status = 'denied') AS denied_count,
                    COUNT(*) FILTER (WHERE status = 'expired') AS expired_count,
                    COUNT(*) FILTER (WHERE status = 'consumed') AS consumed_count
                FROM login_approval_attempts
                WHERE user_id = %s
                """,
                (user["id"],),
            )
            counts_row = cursor.fetchone() or {}
        connection.commit()

    pending_items = [_format_approval_row(row) for row in pending_rows]
    history_items = [_format_approval_row(row) for row in history_rows]
    return {
        "pending": pending_items,
        "history": history_items,
        "summary": {
            "pending": int(counts_row.get("pending_count") or 0),
            "approved": int(counts_row.get("approved_count") or 0),
            "denied": int(counts_row.get("denied_count") or 0),
            "expired": int(counts_row.get("expired_count") or 0),
            "consumed": int(counts_row.get("consumed_count") or 0),
        },
        "meta": {
            "userId": user["id"],
            "xeroUserId": user.get("xero_user_id") or "",
            "serverTime": datetime.now(timezone.utc),
        },
    }


@app.post("/api/auth/login-approval/{attempt_id}/decision")
async def api_login_approval_decision(attempt_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_cookie_csrf(request)
    user = _require_xero_identity_user(user)
    require_auth_app_client(request, user)
    _mark_auth_app_presence(user)
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    decision = str(payload.get("decision") or "").strip().lower()
    if decision not in {"approve", "deny"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Decision must be 'approve' or 'deny'.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_approval_attempts
                SET
                    status = CASE
                        WHEN status = 'pending' AND expires_at > NOW() THEN %s
                        WHEN status = 'pending' AND expires_at <= NOW() THEN 'expired'
                        ELSE status
                    END,
                    approved_by_user_id = CASE WHEN %s = 'approved' THEN %s ELSE approved_by_user_id END,
                    approved_at = CASE WHEN %s = 'approved' THEN NOW() ELSE approved_at END,
                    denied_at = CASE WHEN %s = 'denied' THEN NOW() ELSE denied_at END
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'pending'
                RETURNING *
                """,
                (
                    "approved" if decision == "approve" else "denied",
                    "approved" if decision == "approve" else "denied",
                    user["id"],
                    "approved" if decision == "approve" else "denied",
                    "approved" if decision == "approve" else "denied",
                    attempt_id,
                    user["id"],
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    SELECT status
                    FROM login_approval_attempts
                    WHERE id = %s
                      AND user_id = %s
                    """,
                    (attempt_id, user["id"]),
                )
                row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval attempt not found.")
    return {"status": str(row.get("status") or "pending").lower()}


def approve_device_code_by_state(device_code: str, user_id: str) -> str:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT verification_code
                FROM device_logins
                WHERE device_code = %s
                  AND expires_at > NOW()
                """,
                (device_code,),
            )
            row = cursor.fetchone()
        connection.commit()

    if row is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Expired device code.")

    return approve_device_code(row["verification_code"], user_id)


@app.post("/auth/logout")
def logout(request: Request):
    require_cookie_csrf(request)
    session_token = request.cookies.get(COOKIE_NAME)
    if session_token:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(session_token),))
            connection.commit()
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    return response


@app.get("/", response_class=HTMLResponse)
def console_page(user: dict = Depends(require_user)):
    webpanel_index = WEB_PANEL_DIR / "index.html"
    if webpanel_index.exists():
        return FileResponse(webpanel_index, headers={"Cache-Control": "no-store, max-age=0"})
    logger.warning("WebPanel index not found at %s; serving legacy console", webpanel_index)
    return FileResponse(LEGACY_CONSOLE_PATH, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/styles.css")
def webpanel_styles(user: dict = Depends(require_user)):
    styles_path = WEB_PANEL_DIR / "styles.css"
    if styles_path.exists():
        return FileResponse(styles_path)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="styles.css not found.")


@app.get("/app.js")
def webpanel_script(user: dict = Depends(require_user)):
    script_path = WEB_PANEL_DIR / "app.js"
    if script_path.exists():
        return FileResponse(script_path)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="app.js not found.")


@app.get("/standalone.html", response_class=HTMLResponse)
def webpanel_standalone(user: dict = Depends(require_user)):
    standalone_path = WEB_PANEL_DIR / "standalone.html"
    if standalone_path.exists():
        return FileResponse(standalone_path)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="standalone.html not found.")


@app.get("/console", response_class=HTMLResponse)
def legacy_console_page(user: dict = Depends(require_user)):
    return FileResponse(LEGACY_CONSOLE_PATH, headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/snackccountancy", response_class=HTMLResponse)
def snackccountancy_page():
    return FileResponse(SNACKCCOUNTANCY_PATH)


@app.get("/snackccountancy/account", response_class=HTMLResponse)
def snackccountancy_account_page():
    return FileResponse(SNACKCCOUNTANCY_PATH)


@app.get("/snackccountancy/success", response_class=HTMLResponse)
def snackccountancy_success_page(order_number: str = ""):
    safe_order = escape(order_number or "Pending")
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Snackccountancy payment success</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin:0; min-height:100vh; display:grid; place-items:center; background:#f4f8ff; color:#173149; }}
                main {{ width:min(560px, calc(100vw - 36px)); background:#fff; border:1px solid #d7e4ef; border-radius:18px; padding:24px; }}
                h1 {{ margin:0 0 10px; }}
                p {{ margin:0 0 16px; color:#617791; }}
                a {{ display:inline-flex; border-radius:10px; padding:10px 14px; background:#0a3b8d; color:#fff; text-decoration:none; font-weight:700; }}
            </style>
        </head>
        <body>
            <main>
                <h1>Payment received</h1>
                <p>Order number: <strong>{safe_order}</strong></p>
                <a href="/snackccountancy">Back to Snackccountancy</a>
            </main>
        </body>
        </html>
        """
    )


@app.get("/snackccountancy/cancelled", response_class=HTMLResponse)
def snackccountancy_cancelled_page():
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Snackccountancy payment cancelled</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin:0; min-height:100vh; display:grid; place-items:center; background:#f4f8ff; color:#173149; }
                main { width:min(560px, calc(100vw - 36px)); background:#fff; border:1px solid #d7e4ef; border-radius:18px; padding:24px; }
                h1 { margin:0 0 10px; }
                p { margin:0 0 16px; color:#617791; }
                a { display:inline-flex; border-radius:10px; padding:10px 14px; background:#0a3b8d; color:#fff; text-decoration:none; font-weight:700; }
            </style>
        </head>
        <body>
            <main>
                <h1>Payment not completed</h1>
                <p>You have not been charged.</p>
                <a href="/snackccountancy">Try again</a>
            </main>
        </body>
        </html>
        """
    )


@app.get("/jenius/tools/snackccountancy", response_class=HTMLResponse)
def jenius_snackccountancy_page(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "snackccountancy_tool.html",
        template_context(request, snack_dashboard=snack_dashboard_payload(), snack_user=user),
    )


@app.get("/api/snackccountancy/products")
def api_snackccountancy_products():
    return snack_products_payload()


@app.get("/api/snackccountancy/customer/me")
def api_snackccountancy_customer_me(request: Request):
    context = snack_session_context_from_request(request)
    return snack_customer_summary(context.customer)


@app.post("/api/snackccountancy/auth/email")
async def api_snackccountancy_auth_email(request: Request):
    payload = await request.json()
    email = str((payload or {}).get("email") or "").strip()
    name = str((payload or {}).get("name") or "").strip()
    customer = snack_login_with_email(email=email, name=name)
    session_token = _create_snack_session(str(customer["id"]), device_label=SNACK_SESSION_LABEL)
    response = JSONResponse(snack_customer_summary(customer))
    set_snack_session_cookie(response, session_token)
    return response


@app.post("/api/snackccountancy/auth/logout")
def api_snackccountancy_auth_logout(request: Request):
    context = snack_session_context_from_request(request)
    snack_logout(context.token)
    response = JSONResponse({"status": "ok"})
    clear_snack_session_cookie(response)
    return response


@app.post("/api/snackccountancy/calculate-basket")
async def api_snackccountancy_calculate_basket(request: Request):
    payload = await request.json()
    context = snack_session_context_from_request(request)
    basket = calculate_snackccountancy_basket(payload, customer=context.customer)
    return {"basket": basket, "customer": snack_customer_summary(context.customer)}


@app.post("/api/snackccountancy/create-payment")
async def api_snackccountancy_create_payment(request: Request):
    payload = await request.json()
    context = snack_session_context_from_request(request)
    return snack_create_payment(payload, customer=context.customer)


@app.post("/api/snackccountancy/claim-paid-session")
async def api_snackccountancy_claim_paid_session(request: Request):
    payload = await request.json()
    order_number = str((payload or {}).get("order_number") or "").strip()
    payment_intent_id = str((payload or {}).get("payment_intent_id") or "").strip()
    result = snack_claim_paid_order_session(order_number=order_number, payment_intent_id=payment_intent_id)
    response = JSONResponse({"status": "ok", "customer": result["customer"]})
    set_snack_session_cookie(response, result["session_token"])
    return response


@app.get("/api/snackccountancy/orders/me")
def api_snackccountancy_orders_me(request: Request):
    context = snack_session_context_from_request(request)
    if not context.customer:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to view order history.")
    return snack_orders_for_customer(str(context.customer["id"]))


@app.post("/api/stripe/snackccountancy-webhook")
async def api_snackccountancy_stripe_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("Stripe-Signature")
    result = snack_handle_stripe_webhook(body=body, signature=signature)
    return JSONResponse(result)


@app.get("/api/jenius/snackccountancy/dashboard")
def api_jenius_snackccountancy_dashboard(user: dict = Depends(require_panel_user)):
    return snack_dashboard_payload()


@app.get("/api/jenius/snackccountancy/orders")
def api_jenius_snackccountancy_orders(user: dict = Depends(require_panel_user)):
    return snack_orders_admin()


@app.get("/api/jenius/snackccountancy/customers")
def api_jenius_snackccountancy_customers(user: dict = Depends(require_panel_user)):
    return snack_customers_admin()


@app.get("/api/jenius/snackccountancy/products")
def api_jenius_snackccountancy_products(user: dict = Depends(require_panel_user)):
    return snack_products_payload()


@app.post("/api/jenius/snackccountancy/products")
async def api_jenius_snackccountancy_upsert_products(request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "manage Snackccountancy products")
    payload = await request.json()
    return snack_products_admin_upsert(payload)


@app.patch("/api/jenius/snackccountancy/products/{product_id}")
async def api_jenius_snackccountancy_patch_product(product_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "manage Snackccountancy products")
    payload = await request.json()
    payload["id"] = product_id
    return snack_products_admin_upsert(payload)


@app.patch("/api/jenius/snackccountancy/customers/{customer_id}")
async def api_jenius_snackccountancy_patch_customer(customer_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "manage Snackccountancy customers")
    payload = await request.json()
    return snack_customer_admin_patch(customer_id, payload)


@app.patch("/api/jenius/snackccountancy/orders/{order_id}")
async def api_jenius_snackccountancy_patch_order(order_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "manage Snackccountancy orders")
    payload = await request.json()
    return snack_order_admin_patch(order_id, payload)


@app.get("/api/jenius/snackccountancy/export/orders.csv")
def api_jenius_snackccountancy_export_orders_csv(user: dict = Depends(require_panel_user)):
    data = snack_orders_admin().get("orders", [])
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "order_number",
            "status",
            "customer",
            "customer_email",
            "subtotal_pence",
            "weekly_discount_pence",
            "milestone_discount_pence",
            "total_paid_pence",
            "currency",
            "stripe_payment_intent_id",
            "is_10th_order_reward",
            "double_reward_active",
            "created_at",
            "paid_at",
        ],
    )
    writer.writeheader()
    for row in data:
        writer.writerow(row)
    return Response(output.getvalue(), media_type="text/csv")


@app.get("/api/jenius/snackccountancy/export/customers.csv")
def api_jenius_snackccountancy_export_customers_csv(user: dict = Depends(require_panel_user)):
    data = snack_customers_admin().get("customers", [])
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "id",
            "name",
            "email",
            "auth_provider",
            "total_orders",
            "total_cans",
            "lifetime_spend_pence",
            "lifetime_savings_pence",
            "created_at",
            "last_login_at",
        ],
    )
    writer.writeheader()
    for row in data:
        writer.writerow(row)
    return Response(output.getvalue(), media_type="text/csv")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, user: dict = Depends(require_user)):
    xero_connected = False
    try:
        get_xero_connection_for_user(user["id"])
        xero_connected = True
    except HTTPException:
        xero_connected = False

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        template_context(
            request,
            dashboard=dashboard_payload(),
            xero_connected=xero_connected,
        ),
    )


@app.post("/sync/run")
async def trigger_sync(user: dict = Depends(require_user)):
    sync_run, started = request_sync_run(user)
    if started:
        await run_sync(user, str(sync_run["id"]))
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "customers.html",
        template_context(request, customers=list_customers()),
    )


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_page(customer_id: str, request: Request, user: dict = Depends(require_user)):
    detail = customer_detail(customer_id)
    return templates.TemplateResponse(
        request,
        "customer_detail.html",
        template_context(request, **detail),
    )


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_page(invoice_id: str, request: Request, user: dict = Depends(require_user)):
    detail = invoice_detail(invoice_id)
    return templates.TemplateResponse(
        request,
        "invoice_detail.html",
        template_context(request, **detail),
    )


@app.post("/invoices/{invoice_id}/notes")
async def invoice_add_note(invoice_id: str, user: dict = Depends(require_user), body: str = Form(...)):
    note_body = add_note(invoice_id, user, body)
    await sync_invoice_note_to_xero(invoice_id, user, note_body)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/invoices/{invoice_id}/promises")
async def invoice_add_promise(
    invoice_id: str,
    user: dict = Depends(require_user),
    promised_amount: str = Form(...),
    promised_date: str = Form(...),
    note: str = Form(""),
):
    add_promise(invoice_id, user, promised_amount, promised_date, note)
    await sync_invoice_promise_to_xero(invoice_id, user, promised_amount, promised_date, note)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/invoices/{invoice_id}/status")
async def invoice_set_status(
    invoice_id: str,
    user: dict = Depends(require_user),
    status_value: str = Form(...),
    note: str = Form(""),
):
    update_control_status(invoice_id, user, status_value, note)
    await sync_invoice_status_to_xero(invoice_id, user, status_value, note)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/device", response_class=HTMLResponse)
def device_page(request: Request, code: str | None = None):
    return templates.TemplateResponse(request, "device.html", template_context(request, verification_code=code, approved=False))


@app.post("/device/approve")
def device_approve(verification_code: str = Form(...), user: dict = Depends(require_user)):
    approve_device_code(verification_code, user["id"])
    return RedirectResponse("/device/complete?approved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/device/complete", response_class=HTMLResponse)
def device_complete(request: Request, approved: int = 0):
    return templates.TemplateResponse(request, "device.html", template_context(request, verification_code=None, approved=bool(approved)))


@app.get("/api/device/start")
def api_device_start():
    device = create_device_login()
    state_token = start_oauth_state(device_code=device["device_code"])
    return {
        "device_code": device["device_code"],
        "verification_code": device["verification_code"],
        "verification_uri": f'{get_settings().base_url}/device?code={device["verification_code"]}',
        "login_uri": xero_authorize_url(state_token),
        "expires_at": device["expires_at"],
    }


@app.get("/api/device/poll")
def api_device_poll(device_code: str = Query(...)):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT device_logins.*
                FROM device_logins
                WHERE device_code = %s
                """,
                (device_code,),
            )
            row = cursor.fetchone()
        connection.commit()

    if row is None or row["expires_at"] < __import__("datetime").datetime.now(__import__("datetime").timezone.utc):
        return JSONResponse({"status": "expired"}, status_code=status.HTTP_410_GONE)
    if row["status"] != "approved":
        return {"status": "pending"}

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT users.*
                FROM device_logins
                JOIN users ON users.id = device_logins.user_id
                WHERE device_logins.device_code = %s
                """,
                (device_code,),
            )
            user = cursor.fetchone()
        connection.commit()

    return {
        "status": "approved",
        "session_token": row["session_token"],
        "user": {"email": user["email"], "full_name": user["full_name"]},
    }


@app.get("/api/dashboard")
def api_dashboard(user: dict = Depends(require_api_user)):
    return dashboard_payload()


@app.get("/api/panel")
def api_panel(user: dict = Depends(require_panel_user)):
    try:
        panel_error = None
        try:
            panel = panel_payload(user)
        except Exception as exc:
            logger.exception("Unable to build panel payload")
            panel_error = {
                "message": "The backend could not build the cached ledger panel payload.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
            }
            panel = {
                "organisation": {
                    "name": "",
                    "status": "Cached ledger unavailable",
                    "lastSync": "Backend panel payload failed",
                    "xeroConnected": False,
                },
                "dashboard": {
                    "totalReceivables": 0,
                    "totalOverdue": 0,
                    "openInvoices": 0,
                    "accountsNeedingAction": 0,
                    "potentialInterest": 0,
                },
                "customers": [],
                "cacheStatus": {},
                "databaseMetrics": {"error": panel_error["error"]},
                "audit": [],
                "selectedInvoice": None,
            }
        try:
            active_sync_run = active_sync_run_for_user(user)
        except Exception:
            logger.exception("Unable to read active sync run for panel")
            active_sync_run = None
        try:
            rate_limit = active_xero_rate_limit_for_user(user)
        except Exception:
            logger.exception("Unable to read Xero rate limit for panel")
            rate_limit = None
        return {
            **panel,
            "currentUser": serialise_current_user(user),
            "panelError": panel_error,
            "activeSyncRun": serialize_sync_run(active_sync_run) if active_sync_run else None,
            "xeroRateLimit": serialize_xero_rate_limit(rate_limit),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unhandled /api/panel failure")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Unable to load panel data right now.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
            },
        ) from exc


@app.post("/api/panel/session")
def api_panel_session(request: Request, user: dict = Depends(require_panel_user)):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in allowed_panel_origins():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This panel origin is not allowed.")

    try:
        get_xero_connection_for_user(user["id"])
    except HTTPException:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Xero has not been connected yet.")
    return panel_session_response(user)


@app.get("/api/insights")
async def api_insights(user: dict = Depends(require_panel_user)):
    return await insights_payload(user)


@app.get("/api/company-calendar")
async def api_company_calendar(user: dict = Depends(require_panel_user)):
    return await company_calendar_payload(user)


@app.post("/api/practice-packs/generate")
async def api_practice_pack_generate(
    month: str = Form(""),
    history: str = Form(""),
    client_file: UploadFile = File(..., alias="clientFile"),
    task_file: UploadFile = File(..., alias="taskFile"),
    user: dict = Depends(require_panel_user),
):
    client_bytes, task_bytes = await asyncio.gather(client_file.read(), task_file.read())
    return await practice_pack_payload(
        user,
        client_bytes,
        task_bytes,
        month,
        client_file.filename or "BM Client File CSV",
        task_file.filename or "BM Task File CSV",
        history,
    )


@app.get("/api/practice-packs/runs")
def api_practice_pack_runs(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "runs": list_retained_practice_pack_runs(user)}


@app.get("/api/practice-packs/runs/{run_id}/download")
def api_practice_pack_download(run_id: str, user: dict = Depends(require_panel_user)):
    download = retained_practice_pack_download(user, run_id)
    headers = {"Content-Disposition": f'attachment; filename="{download["filename"]}"'}
    return Response(content=download["bytes"], media_type=download["contentType"], headers=headers)


@app.post("/api/risk-assessments/generate")
async def api_risk_assessments_generate(request: Request, user: dict = Depends(require_panel_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    clients = (body or {}).get("clients") or []
    return await generate_risk_assessments_payload(user, clients)


@app.post("/api/risk-assessments/export-zip")
async def api_risk_assessments_export_zip(request: Request, user: dict = Depends(require_panel_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    assessments = (body or {}).get("assessments") or []
    payload = build_risk_assessments_zip_payload(user, assessments)
    headers = {"Content-Disposition": f'attachment; filename="{payload["filename"]}"'}
    return Response(content=payload["bytes"], media_type=payload["contentType"], headers=headers)


@app.post("/api/risk-assessments/xero-preview")
async def api_risk_assessments_xero_preview(request: Request, user: dict = Depends(require_panel_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    assessments = (body or {}).get("assessments") or []
    return preview_risk_assessments_xero_payload(user, assessments)


@app.post("/api/risk-assessments/xero-send")
async def api_risk_assessments_xero_send(request: Request, user: dict = Depends(require_panel_user)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    assessments = (body or {}).get("assessments") or []
    assessment_ids = (body or {}).get("assessmentIds") or []
    return await send_risk_assessments_to_xero_payload(user, assessments, assessment_ids)


@app.post("/api/panel/sync")
async def api_panel_sync(request: Request, user: dict = Depends(require_panel_user)):
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        sync_options = normalise_sync_options((body or {}).get("syncOptions") or body)
        sync_run, started = request_sync_run(user, sync_options)
        if started:
            _submit_background_job("xero_sync", run_sync_job, dict(user), str(sync_run["id"]), sync_options)
        return {
            "status": "queued" if started else "running",
            "started": started,
            "syncRun": serialize_sync_run(sync_run),
            "syncOptions": sync_options,
        }
    except HTTPException as exc:
        record_sync_start_failure(user, exc)
        raise
    except Exception as exc:
        logger.exception("Unable to queue Xero panel sync")
        record_sync_start_failure(user, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "Unable to start Xero sync.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
            },
        ) from exc


@app.get("/api/panel/sync/{sync_run_id}")
def api_panel_sync_status(sync_run_id: str, user: dict = Depends(require_panel_user)):
    sync_run = get_sync_run(user, sync_run_id)
    rate_limit = active_xero_rate_limit_for_user(user)
    payload = {
        "status": sync_run["status"],
        "syncRun": serialize_sync_run(sync_run),
        "workingDataReady": sync_run_has_working_data(sync_run),
        "xeroRateLimit": serialize_xero_rate_limit(rate_limit),
    }
    if sync_run["status"] == "completed":
        try:
            payload["panel"] = panel_payload(user)
        except Exception as exc:
            logger.exception("Unable to build sync panel payload")
            payload["panelError"] = {
                "message": "Sync data is available, but the refreshed panel payload could not be built.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
            }
    return payload


@app.get("/api/developer/logs")
def api_developer_logs(limit: int = Query(120, ge=1, le=300), user: dict = Depends(require_panel_user)):
    try:
        return {"logs": list_developer_logs(user, limit), "runtime": runtime_diagnostics_payload()}
    except Exception as exc:
        logger.exception("Unable to load developer logs")
        now_iso = datetime.now(timezone.utc).isoformat()
        return {
            "logs": [
                {
                    "id": f"developer.logs.error:{now_iso}",
                    "level": "error",
                    "source": "server",
                    "eventType": "developer.logs.unavailable",
                    "message": str(exc) or exc.__class__.__name__,
                    "payload": {"type": exc.__class__.__name__},
                    "createdAt": now_iso,
                    "syncRunId": "",
                    "syncStatus": "",
                }
            ],
            "runtime": runtime_diagnostics_payload(),
        }


@app.post("/api/developer/logs/clear")
def api_developer_logs_clear(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **clear_developer_logs(user)}


@app.get("/api/settings/usage")
def api_settings_usage(days: int = Query(30, ge=1, le=180), user: dict = Depends(require_panel_user)):
    return usage_overview_payload(user, days=days)


@app.get("/api/settings/usage/detail")
def api_settings_usage_detail(
    provider: str = Query("xero"),
    days: int = Query(30, ge=1, le=180),
    feature: str = Query(""),
    page: str = Query(""),
    endpoint: str = Query(""),
    user: dict = Depends(require_panel_user),
):
    return usage_detail_payload(
        user,
        days=days,
        provider=provider,
        feature=feature,
        page=page,
        endpoint=endpoint,
    )


@app.get("/api/settings/releases")
def api_settings_releases(
    limit: int = Query(120, ge=1, le=2000),
    user: dict = Depends(require_panel_user),
):
    return deployment_updates_payload(user, limit=limit)


@app.get("/api/security/users")
def api_security_users(user: dict = Depends(require_panel_user)):
    require_security_admin(user)
    return {"status": "ok", **_security_users_payload()}


@app.get("/api/security/audit")
def api_security_audit(
    limit: int = Query(120, ge=20, le=500),
    user: dict = Depends(require_panel_user),
):
    require_security_admin(user)
    return {"status": "ok", **_security_audit_payload(limit=limit)}


@app.get("/api/security/overview")
def api_security_overview(user: dict = Depends(require_panel_user)):
    require_security_admin(user)
    users_payload = _security_users_payload()
    audit_payload = _security_audit_payload(limit=40)
    summary = users_payload.get("summary") or {}
    return {
        "status": "ok",
        "overview": {
            "activeUsers": int(summary.get("activeUsers") or 0),
            "pendingInvites": int(summary.get("pendingInvites") or 0),
            "suspendedUsers": int(summary.get("suspendedUsers") or 0),
            "ownerUsers": int(summary.get("ownerUsers") or 0),
            "recentEvents": len(audit_payload.get("events") or []),
            "xeroOnlyAuthEnforced": True,
            "appApprovalRequired": False,
        },
        "recentEvents": audit_payload.get("events") or [],
    }


@app.post("/api/security/users")
async def api_security_create_user(request: Request, user: dict = Depends(require_panel_user)):
    require_security_admin(user)
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    created = _security_create_user(payload if isinstance(payload, dict) else {}, user)
    return {"status": "ok", "created": created, **_security_users_payload()}


@app.post("/api/security/users/{target_user_id}/status")
async def api_security_update_user_status(target_user_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_security_admin(user)
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    status_value = str((payload or {}).get("status") or "").strip()
    if not status_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status is required.")
    updated = _security_change_user_status(target_user_id, status_value, user)
    return {"status": "ok", "updated": updated, **_security_users_payload()}


@app.post("/api/security/users/{target_user_id}/force-logout")
def api_security_force_logout(target_user_id: str, user: dict = Depends(require_panel_user)):
    require_security_admin(user)
    _security_force_logout(target_user_id, user)
    return {"status": "ok", **_security_users_payload()}


@app.delete("/api/security/users/{target_user_id}")
def api_security_delete_user(target_user_id: str, user: dict = Depends(require_panel_user)):
    require_security_admin(user)
    _security_delete_user(target_user_id, user)
    return {"status": "ok", **_security_users_payload()}


@app.post("/api/xero/disconnect")
async def api_xero_disconnect(request: Request, user: dict = Depends(require_panel_user)):
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    tenant_id = str(payload.get("tenantId") or "").strip()
    disconnect_all = bool(payload.get("disconnectAll"))
    return {"status": "ok", **disconnect_xero(user, tenant_id=tenant_id, disconnect_all=disconnect_all)}


@app.get("/api/xero/chart-of-accounts")
async def api_xero_chart_of_accounts(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **await xero_chart_of_accounts_payload(user)}


@app.get("/api/xero/scope-audit")
def api_xero_scope_audit(
    tenant_id: str = Query("", alias="tenantId"),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **xero_scope_audit_payload(user, tenant_id=tenant_id)}


@app.get("/api/contact-archive/review")
async def api_contact_archive_review(
    force: bool = Query(False, alias="force"),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", "contactArchive": await contact_archive_review_payload(user, force_refresh=force)}


@app.post("/api/contact-archive/archive")
async def api_contact_archive_archive(request: Request, user: dict = Depends(require_panel_write_user)):
    try:
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        payload = {}
    return {"status": "ok", "contactArchive": await contact_archive_bulk_archive_payload(user, payload if isinstance(payload, dict) else {})}


@app.post("/api/contact-archive/client-register-sync")
async def api_contact_archive_client_register_sync(request: Request, user: dict = Depends(require_panel_write_user)):
    try:
        payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        payload = {}
    return {"status": "ok", "contactArchiveSync": await contact_archive_client_register_sync_payload(user, payload)}


@app.post("/api/xero/posting-settings")
async def api_xero_posting_settings(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    settings_payload = await save_posting_settings(user, payload)
    return {"status": "ok", **settings_payload, "panel": panel_payload(user)}


@app.post("/api/pi-clearing-account/setup")
async def api_pi_clearing_account_setup(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    settings_payload = await save_pi_clearing_account_setup(user, payload if isinstance(payload, dict) else {})
    return {"status": "ok", **settings_payload}


@app.get("/api/xero/lock-dates")
async def api_xero_lock_dates(
    force: bool = Query(False, alias="force"),
    user: dict = Depends(require_panel_user),
):
    if force:
        try:
            sync_xero_lock_date_company_records(user, {"limit": 1000})
        except Exception as exc:
            logger.warning("Unable to resync mapped Companies House records during lock-date refresh: %s", exc)
    return {"status": "ok", **await xero_lock_date_overview_payload(user, force_refresh=force)}


@app.post("/api/xero/lock-dates/populate-mappings")
async def api_xero_lock_dates_populate_mappings(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {"status": "ok", **populate_xero_lock_date_company_numbers(user, payload)}


@app.get("/api/xero/lock-dates/mismatches")
async def api_xero_lock_date_mismatches(
    force: bool = Query(False, alias="force"),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **await xero_lock_date_mismatch_payload(user, force_refresh=force)}


@app.post("/api/xero/lock-dates/{tenant_id}/fix")
async def api_xero_lock_date_fix(tenant_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", **await fix_xero_lock_date_mismatch(user, tenant_id)}


@app.get("/api/xero/lock-dates/mismatches/report.pdf")
async def api_xero_lock_date_mismatches_pdf(
    force: bool = Query(False, alias="force"),
    user: dict = Depends(require_panel_user),
):
    payload = await xero_lock_date_mismatch_payload(user, force_refresh=force)
    pdf_bytes, filename = xero_lock_date_mismatch_pdf(payload.get("rows") or [], str(payload.get("generatedAt") or ""))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/xero/tenant-mappings/{tenant_id}")
async def api_xero_tenant_mapping_save(tenant_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    mapping = save_xero_tenant_company_mapping(user, tenant_id, payload)
    return {"status": "ok", "mapping": mapping}


@app.get("/api/companies-house/settings")
def api_companies_house_settings_get(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "settings": get_companies_house_settings()}


@app.post("/api/companies-house/settings")
async def api_companies_house_settings_save(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    updated = save_companies_house_settings(user, payload)
    return {"status": "ok", "settings": updated}


@app.post("/api/companies-house/settings/test-connection")
async def api_companies_house_settings_test_connection(request: Request, user: dict = Depends(require_panel_user)):
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    return {"status": "ok", "result": test_companies_house_connection(payload)}


@app.post("/api/companies-house/auth-code-register/upload")
async def api_companies_house_auth_code_register_upload(
    file: UploadFile = File(...),
    user: dict = Depends(require_panel_user),
):
    content = await file.read()
    result = upload_auth_code_register_csv(user, content, file.filename or "auth-code-register.csv")
    return {"status": "ok", "result": result}


@app.post("/api/companies-house/auth-code-register/preview")
async def api_companies_house_auth_code_register_preview(
    file: UploadFile = File(...),
    user: dict = Depends(require_panel_user),
):
    content = await file.read()
    preview = preview_auth_code_register_csv(content, file.filename or "auth-code-register.csv")
    return {"status": "ok", "preview": preview}


@app.post("/api/companies-house/tasks/preview")
async def api_companies_house_tasks_preview(
    file: UploadFile = File(...),
    user: dict = Depends(require_panel_user),
):
    content = await file.read()
    payload = await bm_tasks_vat_preview_payload(user, content, file.filename or "bm-tasks.csv")
    return {"status": "ok", **payload}


@app.get("/api/companies-house/tasks")
def api_companies_house_tasks(user: dict = Depends(require_panel_user)):
    payload = bm_tasks_vat_saved_payload(user)
    return {"status": "ok", **payload}


@app.post("/api/companies-house/auth-code-register/commit")
async def api_companies_house_auth_code_register_commit(
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    preview = payload.get("preview") or {}
    apply_deletes = bool(payload.get("applyDeletes", False))
    result = commit_auth_code_register_import(user, preview, apply_deletes=apply_deletes)
    return {"status": "ok", "result": result}


@app.get("/api/companies-house/auth-code-register")
def api_companies_house_auth_code_register_list(
    limit: int = Query(300, ge=20, le=5000),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **list_auth_code_register(limit=limit)}


@app.post("/api/companies-house/auth-code-register/populate")
async def api_companies_house_auth_code_register_populate(
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    result = populate_auth_codes_from_register(user, payload)
    return {"status": "ok", "result": result}


@app.post("/api/companies-house/auth-code-register/{row_id}")
async def api_companies_house_auth_code_register_update(
    row_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    result = update_auth_code_register_row(user, row_id, payload)
    return {"status": "ok", **result}


@app.get("/api/companies-house/auth-code-register/{row_id}/client-page")
def api_companies_house_auth_code_register_client_page(
    row_id: str,
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **get_auth_register_client_page(row_id)}


@app.patch("/api/companies-house/auth-code-register/{row_id}/client-page")
async def api_companies_house_auth_code_register_client_page_save(
    row_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    return {"status": "ok", **save_auth_register_client_page(user, row_id, payload)}


@app.post("/api/companies-house/auth-code-register/{row_id}/client-page/notes")
async def api_companies_house_auth_code_register_client_page_add_note(
    row_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    return {"status": "ok", **add_auth_register_client_note(user, row_id, payload)}


@app.post("/api/code-breaker/workspace-snapshot")
async def api_code_breaker_workspace_snapshot(
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    try:
        result = await code_breaker_workspace_snapshot(user, payload)
        return {"status": "ok", "result": result}
    except Exception as exc:
        logger.exception("Equity Montior workspace snapshot failed")
        as_at_value = str((payload or {}).get("asAtDate") or (payload or {}).get("yearEndDate") or "").strip()
        fallback_as_at = as_at_value or datetime.now(timezone.utc).date().isoformat()
        error_text = str(exc)[:300]
        return {
            "status": "error",
            "result": {
                "asAtDate": fallback_as_at,
                "tenantId": str((payload or {}).get("tenantId") or ""),
                "tenantName": "",
                "companyNumber": str((payload or {}).get("companyNumber") or ""),
                "ch": {
                    "companyName": "",
                    "netAssets": None,
                    "source": "unavailable",
                    "reason": f"Equity Montior snapshot failed: {error_text}",
                    "lastFiledDate": None,
                    "latestSubmissionCompletedAt": None,
                },
                "xero": {
                    "netAssets": None,
                    "source": "unavailable",
                    "reason": f"Equity Montior snapshot failed: {error_text}",
                },
                "match": {
                    "matches": False,
                    "difference": None,
                },
                "postFilingAnalysis": {
                    "engine": "error",
                    "summary": "",
                    "reason": f"Equity Montior snapshot failed: {error_text}",
                    "submissionCompletedAt": None,
                    "targetDifference": None,
                    "candidateCount": 0,
                    "candidateRows": [],
                    "confidence": 0,
                    "warnings": [],
                    "explainedDifference": None,
                    "residualDifference": None,
                    "transactions": [],
                },
                "error": error_text,
            },
        }


@app.get("/api/company-secretarial/filings")
def api_company_secretarial_list(
    limit: int = Query(500, ge=20, le=2000),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **list_company_secretarial_filings(limit=limit)}


@app.post("/api/company-secretarial/filings")
async def api_company_secretarial_create(
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    return {"status": "ok", "filing": create_company_secretarial_filing(user, payload)}


@app.patch("/api/company-secretarial/filings/{filing_id}")
async def api_company_secretarial_patch(
    filing_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    return {"status": "ok", "filing": patch_company_secretarial_filing(user, filing_id, payload)}


@app.post("/api/company-secretarial/filings/{filing_id}/validate")
async def api_company_secretarial_validate(
    filing_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {"status": "ok", "filing": validate_company_secretarial_filing(user, filing_id, payload)}


@app.post("/api/company-secretarial/filings/{filing_id}/submit")
async def api_company_secretarial_submit(
    filing_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    payload["_requestMeta"] = {
        "ip": request.client.host if request.client else "",
        "forwardedFor": request.headers.get("x-forwarded-for") or "",
        "userAgent": request.headers.get("user-agent") or "",
        "device": request.headers.get("sec-ch-ua-platform") or "",
    }
    return {"status": "ok", "filing": submit_company_secretarial_filing(user, filing_id, payload)}


@app.post("/api/company-secretarial/filings/{filing_id}/complete")
async def api_company_secretarial_complete(
    filing_id: str,
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", "filing": complete_company_secretarial_filing(user, filing_id)}


@app.get("/api/companies-house/dashboard")
def api_companies_house_dashboard(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **companies_house_dashboard_summary()}


@app.get("/api/companies-house/companies")
def api_companies_house_list(
    search: str = Query("", alias="search"),
    internal_status: str = Query("", alias="internalStatus"),
    missing_auth: bool = Query(False, alias="missingAuth"),
    due_soon: bool = Query(False, alias="dueSoon"),
    overdue: bool = Query(False, alias="overdue"),
    xero_connected: bool = Query(False, alias="xeroConnected"),
    user: dict = Depends(require_panel_user),
):
    companies = list_companies({
        "search": search,
        "internalStatus": internal_status,
        "missingAuth": missing_auth,
        "dueSoon": due_soon,
        "overdue": overdue,
        "xeroConnected": xero_connected,
    })
    return {"status": "ok", "companies": companies}


@app.get("/api/companies-house/companies/{company_id}")
def api_companies_house_company_detail(company_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "company": get_company_detail(company_id)}


@app.patch("/api/companies-house/companies/{company_id}")
async def api_companies_house_company_update(
    company_id: str, request: Request, user: dict = Depends(require_panel_user)
):
    payload = await request.json()
    try:
        return {"status": "ok", "company": update_company(company_id, payload, user)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unable to update Companies House company %s", company_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "Unable to save Companies House company changes.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
                "companyId": company_id,
            },
        ) from exc


@app.delete("/api/companies-house/companies/{company_id}")
def api_companies_house_company_delete(company_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "result": delete_company(company_id, user)}


@app.post("/api/companies-house/import/clients/preview")
async def api_companies_house_import_clients_preview(
    file: UploadFile = File(...),
    user: dict = Depends(require_panel_user),
):
    content = await file.read()
    preview = parse_clients_import(content, file.filename or "clients.csv")
    return {"status": "ok", "preview": preview}


@app.post("/api/companies-house/import/clients/commit")
async def api_companies_house_import_clients_commit(
    request: Request, user: dict = Depends(require_panel_user)
):
    payload = await request.json()
    preview = payload.get("preview") or {}
    return {"status": "ok", "result": commit_clients_import(user, preview)}


@app.get("/api/companies-house/imports")
def api_companies_house_imports_list(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "imports": list_companies_house_imports()}


@app.get("/api/companies-house/submissions/attempts")
def api_companies_house_submission_attempts_list(
    limit: int = Query(200, ge=1, le=1000),
    company_id: str = Query("", alias="companyId"),
    user: dict = Depends(require_panel_user),
):
    company_id_value = company_id.strip() or None
    return {
        "status": "ok",
        "attempts": list_submission_attempts(limit=limit, company_id=company_id_value),
    }


@app.get("/api/companies-house/submissions/report")
def api_companies_house_submission_report(
    limit: int = Query(500, ge=1, le=5000),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **submission_reconciliation_report(limit=limit)}


@app.get("/api/companies-house/submissions/{submission_reference}/raw-response")
def api_companies_house_submission_raw_response(
    submission_reference: str,
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", "result": get_submission_raw_response(submission_reference)}


@app.get("/api/companies-house/submissions/attempts/export.csv")
def api_companies_house_submission_attempts_export(
    limit: int = Query(5000, ge=1, le=20000),
    user: dict = Depends(require_panel_user),
):
    content = export_submission_attempts_csv(limit=limit)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="companies-house-submission-attempts.csv"'},
    )


@app.get("/api/companies-house/submissions/support-report.txt")
def api_companies_house_support_report(
    limit: int = Query(50, ge=1, le=500),
    status_filter: str = Query("rejected", alias="status"),
    company_id: str | None = Query(None),
    submission_id: str | None = Query(None),
    user: dict = Depends(require_panel_user),
):
    content = export_companies_house_support_report(
        limit=limit,
        status_filter=status_filter,
        company_id=company_id,
        submission_id=submission_id,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="companies-house-support-report-{stamp}.txt"'},
    )


@app.get("/api/companies-house/dead-letters")
def api_companies_house_dead_letters(
    limit: int = Query(200, ge=1, le=1000),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", "deadLetters": list_dead_letters(limit=limit)}


@app.post("/api/companies-house/dead-letters/replay")
async def api_companies_house_dead_letters_replay(
    request: Request, user: dict = Depends(require_panel_user)
):
    payload = await request.json()
    return {"status": "ok", "result": replay_dead_letter_submissions(user, payload)}


@app.post("/api/companies-house/submissions/bulk")
async def api_companies_house_submit_bulk(
    request: Request, user: dict = Depends(require_panel_user)
):
    payload = await request.json()
    # Defensive self-heal in case the running instance is on an older schema.
    ensure_schema()
    try:
        # Run the preflight synchronously so the user gets immediate, actionable
        # validation errors instead of having to poll a queued job that will fail.
        bulk_submit_confirmation_statements(user, payload, preflight_only=True)
        job_id = create_bulk_submission_job(user, payload)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unexpected Companies House bulk submission queue failure")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=companies_house_bulk_submission_error_detail(exc),
        ) from exc
    _submit_background_job(
        f"ch_bulk_submission:{job_id}",
        run_bulk_submission_job,
        job_id,
        user,
        payload,
    )
    return {"status": "ok", "jobId": job_id}


@app.get("/api/companies-house/submissions/bulk-jobs/{job_id}")
def api_companies_house_bulk_job_status(
    job_id: str,
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", "job": get_bulk_submission_job(job_id)}


@app.post("/api/companies-house/submissions/invoices/bulk")
async def api_companies_house_invoice_bulk(
    request: Request, user: dict = Depends(require_panel_user)
):
    payload = await request.json()
    return {"status": "ok", "result": await bulk_raise_submission_invoices(user, payload)}


@app.post("/api/companies-house/sync")
async def api_companies_house_sync(
    request: Request, user: dict = Depends(require_panel_user)
):
    payload = await request.json()
    return {"status": "ok", "result": sync_companies_house_companies(user, payload)}


@app.post("/api/companies-house/submissions/reconcile")
async def api_companies_house_reconcile_submissions(
    request: Request, user: dict = Depends(require_panel_user)
):
    payload = await request.json()
    return {"status": "ok", "result": run_companies_house_submission_reconciliation(payload)}


@app.get("/api/hmrc-64-8")
def api_hmrc_64_8(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **hmrc_64_8_payload(user)}


@app.get("/api/hmrc-64-8/oauth/status")
def api_hmrc_64_8_oauth_status(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "oauth": hmrc_mtd_oauth_status(user)}


@app.post("/api/hmrc-64-8/oauth/start")
async def api_hmrc_64_8_oauth_start(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **hmrc_mtd_oauth_start(user, redirect_to=payload.get("redirectTo"))}


@app.get("/api/hmrc-64-8/oauth/callback")
def api_hmrc_64_8_oauth_callback(code: str = Query(""), state: str = Query("")):
    result = hmrc_mtd_oauth_callback(code=code, state=state)
    redirect_to = result.get("redirectTo") or "/credit-control-hmrc-64-8s"
    separator = "&" if "?" in redirect_to else "?"
    return RedirectResponse(f"{redirect_to}{separator}hmrcMtdConnected=1")


@app.post("/api/hmrc-64-8/oauth/disconnect")
def api_hmrc_64_8_oauth_disconnect(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "oauth": hmrc_mtd_oauth_disconnect(user)}


@app.get("/api/hmrc-64-8/export.csv")
def api_hmrc_64_8_export(user: dict = Depends(require_panel_user)):
    return Response(
        content=hmrc_64_8_export_csv(user),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="hmrc-64-8-requests.csv"'},
    )


@app.get("/api/hmrc-64-8/history")
def api_hmrc_64_8_history(
    limit: int = Query(500, ge=1, le=2000),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **hmrc_64_8_history(user, limit=limit)}


@app.post("/api/hmrc-64-8/requests")
async def api_hmrc_64_8_create(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "request": create_hmrc_64_8_request(user, payload), **hmrc_64_8_payload(user)}


@app.post("/api/hmrc-64-8/requests/{request_id}")
async def api_hmrc_64_8_update(request_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "request": update_hmrc_64_8_request(user, request_id, payload), **hmrc_64_8_payload(user)}


@app.post("/api/hmrc-64-8/requests/{request_id}/submit")
async def api_hmrc_64_8_submit(request_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "request": submit_hmrc_64_8_request(user, request_id, payload), **hmrc_64_8_payload(user)}


@app.post("/api/hmrc-64-8/requests/{request_id}/capture-code")
async def api_hmrc_64_8_capture_code(request_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "request": capture_hmrc_64_8_code(user, request_id, payload), **hmrc_64_8_payload(user)}


@app.post("/api/hmrc-64-8/requests/{request_id}/reminder")
async def api_hmrc_64_8_reminder(request_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "request": send_hmrc_64_8_reminder(user, request_id, payload), **hmrc_64_8_payload(user)}


@app.post("/api/panel/factory-reset")
def api_panel_factory_reset(user: dict = Depends(require_panel_user)):
    reset = factory_reset_console(user)
    return {"status": "ok", "reset": reset, "panel": panel_payload(user)}


@app.post("/api/late-payment-charges")
async def api_late_payment_charges(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    charges = await create_late_payment_charges(
        user,
        payload.get("invoiceIds") or [],
        payload.get("chargeSelections") or payload.get("charges") or [],
    )
    return {"status": "ok", **charges, "panel": panel_payload(user)}


@app.post("/api/late-payment-charges/run")
async def api_late_payment_charges_run(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    invoice_ids = payload.get("invoiceIds") or []
    options = {"chargeSelections": payload.get("chargeSelections") or payload.get("charges") or []}
    operation_run = request_operation_run(user, "late_payment_charges", invoice_ids, options)
    _submit_background_job(
        "late_payment_charges_operation",
        _run_async_job,
        run_invoice_operation_job,
        dict(user),
        str(operation_run["id"]),
        "late_payment_charges",
        invoice_ids,
        options,
    )
    return {"status": "queued", "operationRun": serialize_operation_run(operation_run)}


@app.post("/api/write-offs")
async def api_write_offs(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    write_offs = await create_bad_debt_write_offs(user, payload.get("invoiceIds") or [])
    return {"status": "ok", **write_offs, "panel": panel_payload(user)}


@app.post("/api/write-offs/run")
async def api_write_offs_run(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    invoice_ids = payload.get("invoiceIds") or []
    operation_run = request_operation_run(user, "bad_debt_write_offs", invoice_ids)
    _submit_background_job(
        "bad_debt_write_offs_operation",
        _run_async_job,
        run_invoice_operation_job,
        dict(user),
        str(operation_run["id"]),
        "bad_debt_write_offs",
        invoice_ids,
    )
    return {"status": "queued", "operationRun": serialize_operation_run(operation_run)}


@app.get("/api/xero/pending-actions")
def api_xero_pending_actions(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **pending_xero_actions_payload(user)}


@app.post("/api/xero/pending-actions")
async def api_xero_pending_actions_queue(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    action_type = payload.get("actionType") or payload.get("operationType") or ""
    invoice_ids = payload.get("invoiceIds") or []
    options = payload.get("options") or {}
    if payload.get("chargeSelections") or payload.get("charges"):
        options = {**options, "chargeSelections": payload.get("chargeSelections") or payload.get("charges") or []}
    return {"status": "ok", **queue_pending_xero_action(user, action_type, invoice_ids, options)}


@app.post("/api/xero/pending-actions/sync")
async def api_xero_pending_actions_sync(user: dict = Depends(require_panel_user)):
    result = await process_pending_xero_actions(user)
    return {"status": "ok", **result, "panel": panel_payload(user)}


@app.get("/api/operations/{operation_run_id}")
def api_operation_status(operation_run_id: str, user: dict = Depends(require_panel_user)):
    operation_run = get_operation_run(user, operation_run_id)
    payload = {
        "status": operation_run["status"],
        "operationRun": serialize_operation_run(operation_run),
    }
    if operation_run["status"] == "completed":
        try:
            payload["panel"] = panel_payload(user)
        except Exception as exc:
            logger.exception("Unable to build completed operation panel payload")
            payload["panelError"] = {
                "message": "Operation completed, but the refreshed panel payload could not be built.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
            }
    return payload


@app.get("/api/jashflow")
def api_jashflow(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "jashflow": jashflow_payload(user)}


@app.post("/api/jashflow/loans")
async def api_create_jashflow_loan(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "jashflow": create_jashflow_loan(user, payload)}


@app.put("/api/jashflow/loans/{loan_id}")
async def api_update_jashflow_loan(loan_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "jashflow": update_jashflow_loan(user, loan_id, payload)}


@app.delete("/api/jashflow/loans/{loan_id}")
def api_delete_jashflow_loan(loan_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "jashflow": delete_jashflow_loan(user, loan_id)}


@app.post("/api/jashflow/loans/{loan_id}/payments")
async def api_add_jashflow_payment(loan_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "jashflow": add_jashflow_payment(user, loan_id, payload)}


@app.post("/api/jashflow/loans/{loan_id}/charges")
async def api_add_jashflow_charge(loan_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "jashflow": add_jashflow_charge(user, loan_id, payload)}


@app.post("/api/jashflow/settings")
async def api_save_jashflow_settings(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "jashflow": save_jashflow_settings(user, payload)}


@app.post("/api/jashflow/interest-posts")
async def api_post_jashflow_interest(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **await post_jashflow_interest_invoice(user, payload)}


@app.post("/api/jashflow/interest-preview")
async def api_jashflow_interest_preview(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "preview": jashflow_interest_preview(user, payload)}


@app.get("/api/ignition")
def api_ignition(
    include_dashboard: bool = Query(default=True, description="Include heavy dashboard aggregates."),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", "ignition": ignition_payload(user, include_dashboard=include_dashboard)}


@app.post("/api/ignition/sync")
def api_ignition_sync(user: dict = Depends(require_panel_user)):
    sync_run, started = request_ignition_sync_run(user)
    if started:
        _submit_background_job("ignition_sync_api", run_ignition_sync_job, dict(user), str(sync_run["id"]))
    return {"status": sync_run["status"], "started": started, "ignitionSyncRun": serialize_ignition_sync_run(sync_run)}


@app.get("/api/ignition/sync/{sync_run_id}")
def api_ignition_sync_status(sync_run_id: str, user: dict = Depends(require_panel_user)):
    sync_run = get_ignition_sync_run(user, sync_run_id)
    return {"status": sync_run["status"], "ignitionSyncRun": serialize_ignition_sync_run(sync_run)}


@app.get("/api/ignition/renewals")
def api_ignition_renewals(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "renewals": ignition_renewals_payload(user)}


@app.get("/api/micro-analyzer/clients")
def api_micro_analyzer_clients(user: dict = Depends(require_panel_user)):
    try:
        return {"status": "ok", **micro_analyzer_clients_payload(user)}
    except Exception as exc:
        logger.exception("Unable to load micro-analyzer clients", extra={"user_id": user.get("id"), "error": str(exc)})
        return {
            "status": "ok",
            "clients": [],
            "count": 0,
            "warning": "Micro-analyzer clients could not be loaded. Check backend logs for details.",
        }


@app.get("/api/vault")
def api_vault(
    search: str = Query("", alias="search"),
    folder: str = Query("", alias="folder"),
    tag: str = Query("", alias="tag"),
    client: str = Query("", alias="client"),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **vault_payload(user, search=search, folder=folder, tag=tag, client=client)}


@app.post("/api/vault/analyze")
async def api_vault_analyze(
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_panel_user),
):
    payloads: list[dict] = []
    for file in files or []:
        payloads.append(
            {
                "filename": file.filename or "document",
                "content_type": file.content_type or "",
                "file_bytes": await file.read(),
            }
        )
    return {"status": "ok", **await vault_analyze_files(user, payloads)}


@app.post("/api/vault/upload")
async def api_vault_upload(
    files: list[UploadFile] = File(...),
    manifest: str = Form("{}"),
    user: dict = Depends(require_panel_user),
):
    manifest_payload = {}
    try:
        candidate = json.loads(manifest or "{}")
        manifest_payload = candidate if isinstance(candidate, dict) else {}
    except Exception:
        manifest_payload = {}
    payloads: list[dict] = []
    for file in files or []:
        payloads.append(
            {
                "filename": file.filename or "document",
                "content_type": file.content_type or "",
                "file_bytes": await file.read(),
            }
        )
    return {"status": "ok", **await vault_upload_files(user, payloads, manifest_payload)}


@app.get("/api/vault/files/{file_id}/content")
def api_vault_file_content(file_id: str, user: dict = Depends(require_panel_user)):
    file_bytes, filename, content_type = vault_file_content(user, file_id)
    safe_name = str(filename or "file").replace('"', "'")
    return Response(content=file_bytes, media_type=content_type, headers={"Content-Disposition": f'inline; filename="{safe_name}"'})


@app.delete("/api/vault/files/{file_id}")
def api_vault_delete_file(file_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", **vault_delete_file(user, file_id)}


@app.post("/api/vault/assign")
def api_vault_assign(payload: dict | None = Body(default=None), user: dict = Depends(require_panel_user)):
    file_ids = payload.get("fileIds") if isinstance(payload, dict) else []
    client_query = payload.get("client") if isinstance(payload, dict) else ""
    return {"status": "ok", **vault_assign_files_to_client(user, file_ids if isinstance(file_ids, list) else [], str(client_query or ""))}


@app.post("/api/ignition/renewals/populate-client-ids")
def api_populate_ignition_renewal_client_ids(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **populate_ignition_renewal_candidate_client_ids(user)}


@app.post("/api/ignition/renewals/document-id/extract")
async def api_extract_ignition_renewal_document_id(
    file: UploadFile = File(...),
    user: dict = Depends(require_panel_user),
):
    _ = user
    file_bytes = await file.read()
    return {"status": "ok", **extract_ignition_renewal_document_id(file.filename or "engagement-letter.pdf", file.content_type or "application/pdf", file_bytes)}


@app.get("/api/ignition/renewals/{run_id}")
def api_ignition_renewal_run(run_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "renewals": ignition_renewals_payload(user, run_id)}


@app.get("/api/ignition/renewals/{run_id}/audit-history")
def api_ignition_renewal_audit_history(run_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "auditHistory": ignition_renewals_audit_history(user, run_id)}


@app.post("/api/ignition/renewals/run")
async def api_create_ignition_renewal_run(request: Request, user: dict = Depends(require_panel_user)):
    try:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return {"status": "ok", **await create_ignition_renewal_run(user, payload if isinstance(payload, dict) else {})}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Unable to create Ignition renewal run")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "message": "Unable to create Ignition renewal run.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
            },
        ) from exc


@app.post("/api/ignition/renewals/ineligible")
async def api_mark_ignition_renewals_ineligible(request: Request, user: dict = Depends(require_panel_user)):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **mark_ignition_renewal_proposals_ineligible(user, payload if isinstance(payload, dict) else {})}


@app.post("/api/ignition/renewals/ineligible/restore")
async def api_restore_ignition_renewals_to_eligible(request: Request, user: dict = Depends(require_panel_user)):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **restore_ignition_renewal_proposals_to_eligible(user, payload if isinstance(payload, dict) else {})}


@app.post("/api/ignition/renewals/{run_id}")
async def api_update_ignition_renewal_run(run_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **update_ignition_renewal_run(user, run_id, payload)}


@app.delete("/api/ignition/renewals/{run_id}")
def api_delete_ignition_renewal_run(run_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", **delete_ignition_renewal_run(user, run_id)}


@app.post("/api/ignition/renewals/{run_id}/email")
async def api_send_ignition_renewals_email(run_id: str, request: Request, user: dict = Depends(require_panel_user)):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **await send_ignition_renewals_email(user, run_id, payload if isinstance(payload, dict) else {})}


@app.post("/api/ignition/renewals/{run_id}/client-comms/send")
async def api_send_ignition_renewal_client_comms(run_id: str, request: Request, user: dict = Depends(require_panel_user)):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **await send_ignition_renewal_client_comms_email(user, run_id, payload if isinstance(payload, dict) else {})}


@app.get("/api/ignition/renewals/{run_id}/email-preview")
async def api_ignition_renewals_email_preview(run_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "email": await ignition_renewals_email_preview(user, run_id)}


@app.post("/api/ignition/renewals/{run_id}/finalise")
async def api_finalise_ignition_renewals(run_id: str, request: Request, user: dict = Depends(require_panel_user)):
    await request.body()
    return {"status": "ok", **await finalise_ignition_renewals(user, run_id)}


@app.post("/api/ignition/renewals/{run_id}/unlock")
async def api_unlock_ignition_renewals(run_id: str, request: Request, user: dict = Depends(require_panel_user)):
    await request.body()
    return {"status": "ok", **await unlock_ignition_renewals(user, run_id)}


@app.get("/api/ignition/renewals/{run_id}/pdf")
def api_ignition_renewals_pdf(run_id: str, user: dict = Depends(require_panel_user)):
    pdf_bytes, filename = ignition_renewals_report_pdf(user, run_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/juksib/batches")
async def api_juksib_batches(
    limit: int = Query(default=30, ge=1, le=200),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **await juksib_list_batches(user, limit=limit)}


@app.post("/api/juksib/batches/import")
async def api_juksib_import_batch(request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "import JUKSIB batches")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **await juksib_import_batch(user, payload if isinstance(payload, dict) else {})}


@app.get("/api/juksib/batches/{batch_id}")
async def api_juksib_batch(batch_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", **await juksib_get_batch(user, batch_id)}


@app.delete("/api/juksib/batches/{batch_id}")
async def api_juksib_delete_batch(batch_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", **await juksib_delete_batch(user, batch_id)}


@app.post("/api/juksib/batches/{batch_id}/revert-to-draft")
async def api_juksib_revert_to_draft(batch_id: str, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "revert JUKSIB batches to draft")
    return {"status": "ok", **await juksib_revert_batch_to_draft(user, batch_id)}


@app.post("/api/juksib/batches/{batch_id}/invoices/status")
async def api_juksib_bulk_status(batch_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "update JUKSIB invoice statuses")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **await juksib_bulk_update_invoice_status(user, batch_id, payload if isinstance(payload, dict) else {})}


@app.post("/api/juksib/batches/{batch_id}/invoices/override")
async def api_juksib_override(batch_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "apply JUKSIB match overrides")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **await juksib_apply_override(user, batch_id, payload if isinstance(payload, dict) else {})}


@app.post("/api/juksib/batches/{batch_id}/include-excluded")
async def api_juksib_include_excluded(batch_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "include excluded JUKSIB invoices")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **await juksib_include_excluded_invoices(user, batch_id, payload if isinstance(payload, dict) else {})}


@app.post("/api/juksib/batches/{batch_id}/publish")
async def api_juksib_publish(batch_id: str, request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "publish JUKSIB invoices to Xero")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", **await juksib_publish_batch(user, batch_id, payload if isinstance(payload, dict) else {})}


@app.get("/api/juksib/batches/{batch_id}/audit")
async def api_juksib_audit(batch_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", **await juksib_batch_audit(user, batch_id)}


@app.get("/api/juksib/automation")
def api_juksib_automation(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "automation": juksib_automation_payload(user)}


@app.post("/api/juksib/automation/settings")
async def api_update_juksib_automation_settings(request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "change JUKSIB automation settings")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    return {"status": "ok", "automation": update_juksib_automation_settings(user, payload if isinstance(payload, dict) else {})}


@app.post("/api/juksib/automation/run-now")
async def api_run_juksib_automation_now(request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "run JUKSIB automation")
    await request.body()
    return {"status": "ok", "automation": await run_juksib_automation_now(user)}


@app.get("/api/juksib/source-invoices/{invoice_id}/pdf")
async def api_juksib_source_invoice_pdf(invoice_id: str, user: dict = Depends(require_panel_user)):
    file_bytes, filename = await juksib_source_invoice_pdf_bytes(user, invoice_id)
    safe_filename = str(filename or "juk-invoice.pdf").replace('"', "")
    return Response(
        content=file_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename}"'},
    )


@app.get("/api/juksib/batches/{batch_id}/excel")
async def api_juksib_batch_excel(batch_id: str, user: dict = Depends(require_panel_user)):
    workbook_bytes, filename = await juksib_batch_excel_report(user, batch_id)
    safe_filename = str(filename or "juksib-report.xlsx").replace('"', "")
    return Response(
        content=workbook_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'},
    )


@app.get("/api/me-report")
def api_me_report(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": me_report_payload(user)}


@app.get("/api/client-call-stats")
def api_client_call_stats(
    date_from: str = Query("", alias="dateFrom"),
    date_to: str = Query("", alias="dateTo"),
    staff_member: str = Query("", alias="staffMember"),
    client_manager: str = Query("", alias="clientManager"),
    client_id: str = Query("", alias="clientId"),
    direction: str = Query("", alias="direction"),
    outcome: str = Query("", alias="outcome"),
    match_status: str = Query("", alias="matchStatus"),
    import_file_id: str = Query("", alias="importFileId"),
    search: str = Query("", alias="search"),
    page: int = Query(1, ge=1),
    page_size: int = Query(250, ge=25, le=1000, alias="pageSize"),
    user: dict = Depends(require_panel_user),
):
    filters = {
        "dateFrom": date_from,
        "dateTo": date_to,
        "staffMember": staff_member,
        "clientManager": client_manager,
        "clientId": client_id,
        "direction": direction,
        "outcome": outcome,
        "matchStatus": match_status,
        "importFileId": import_file_id,
        "search": search,
    }
    return {"status": "ok", "clientCallStats": call_stats_dashboard_payload(user, filters, page=page, page_size=page_size)}


@app.post("/api/client-call-stats/import/preview")
async def api_client_call_stats_import_preview(
    file: UploadFile = File(...),
    mapping: str = Form(""),
    user: dict = Depends(require_panel_user),
):
    parsed_mapping = {}
    if str(mapping or "").strip():
        try:
            payload = json.loads(mapping)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mapping must be valid JSON.") from exc
        parsed_mapping = payload if isinstance(payload, dict) else {}
    content = await file.read()
    return {
        "status": "ok",
        "preview": call_stats_import_preview(user, content, mapping=parsed_mapping),
    }


@app.post("/api/client-call-stats/import/commit")
async def api_client_call_stats_import_commit(
    file: UploadFile = File(...),
    mapping: str = Form(""),
    source_provider: str = Form("", alias="sourceProvider"),
    user: dict = Depends(require_panel_user),
):
    require_panel_write_user(user, "import client call logs")
    parsed_mapping = {}
    if str(mapping or "").strip():
        try:
            payload = json.loads(mapping)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mapping must be valid JSON.") from exc
        parsed_mapping = payload if isinstance(payload, dict) else {}
    content = await file.read()
    commit_result = call_stats_import_commit(
        user,
        content,
        file.filename or "call-log.csv",
        source_provider=source_provider,
        mapping=parsed_mapping,
    )
    return {
        "status": "ok",
        "result": commit_result,
        "clientCallStats": call_stats_dashboard_payload(user, {}),
    }


@app.post("/api/client-call-stats/resync")
async def api_client_call_stats_resync(request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "re-sync call stats")
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    trigger_source = str((payload or {}).get("triggerSource") or "manual").strip() if isinstance(payload, dict) else "manual"
    reason = str((payload or {}).get("reason") or "Manual Re-Sync").strip() if isinstance(payload, dict) else "Manual Re-Sync"
    result = call_stats_resync(user, trigger_source=trigger_source, reason=reason)
    return {"status": "ok", "result": result, "clientCallStats": call_stats_dashboard_payload(user, {})}


@app.get("/api/client-call-stats/extensions")
def api_client_call_stats_extensions(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "extensions": call_stats_extension_directory_payload()}


@app.post("/api/client-call-stats/extensions")
async def api_client_call_stats_save_extension(request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "edit internal extension directory")
    payload = await request.json()
    row = call_stats_save_extension(user, payload if isinstance(payload, dict) else {})
    return {"status": "ok", "extension": row, "extensions": call_stats_extension_directory_payload()}


@app.get("/api/client-call-stats/unmatched")
def api_client_call_stats_unmatched(limit: int = Query(100, ge=1, le=1000), user: dict = Depends(require_panel_user)):
    return {"status": "ok", "unmatchedNumbers": call_stats_unmatched_numbers(user, limit=limit)}


@app.post("/api/client-call-stats/unmatched/action")
async def api_client_call_stats_unmatched_action(request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "manage unmatched call numbers")
    payload = await request.json()
    result = call_stats_apply_number_action(user, payload if isinstance(payload, dict) else {})
    return {"status": "ok", "result": result, "clientCallStats": call_stats_dashboard_payload(user, {})}


@app.get("/api/client-call-stats/clients/{client_id}")
def api_client_call_stats_client_logs(
    client_id: str,
    date_from: str = Query("", alias="dateFrom"),
    date_to: str = Query("", alias="dateTo"),
    staff_member: str = Query("", alias="staffMember"),
    direction: str = Query("", alias="direction"),
    outcome: str = Query("", alias="outcome"),
    search: str = Query("", alias="search"),
    user: dict = Depends(require_panel_user),
):
    filters = {
        "dateFrom": date_from,
        "dateTo": date_to,
        "staffMember": staff_member,
        "direction": direction,
        "outcome": outcome,
        "search": search,
    }
    return {"status": "ok", "clientCallLogs": call_stats_client_logs_payload(user, client_id, filters)}


@app.post("/api/client-call-stats/ai-report")
async def api_client_call_stats_ai_report(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    report = await call_stats_generate_ai_report(user, payload if isinstance(payload, dict) else {})
    return {"status": "ok", "report": report}


@app.get("/api/client-call-stats/ai-reports")
def api_client_call_stats_ai_reports(limit: int = Query(24, ge=1, le=100), user: dict = Depends(require_panel_user)):
    return {"status": "ok", "reports": call_stats_ai_reports_history(user, limit=limit)}


@app.post("/api/client-call-stats/filter-presets/suggest")
async def api_client_call_stats_filter_presets_suggest(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    result = await call_stats_suggest_filter_presets(user, payload if isinstance(payload, dict) else {})
    return {"status": "ok", **result}


@app.get("/api/pi-clearing-account")
def api_pi_clearing_account(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **pi_clearing_payload(user)}


@app.post("/api/pi-clearing-account/run")
async def api_run_pi_clearing_account_workflow(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {"status": "ok", **await run_pi_clearing_workflow(user, payload if isinstance(payload, dict) else {})}


@app.delete("/api/pi-clearing-account/runs/{run_id}")
async def api_delete_pi_clearing_account_run(run_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {"status": "ok", **await delete_pi_clearing_run(user, run_id, payload if isinstance(payload, dict) else {})}


@app.post("/api/pi-clearing-account/runs/{run_id}/credit-notes")
async def api_apply_pi_clearing_account_credit_notes(
    run_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {
        "status": "ok",
        **await apply_pi_clearing_credit_notes(user, run_id, payload if isinstance(payload, dict) else {}),
    }


@app.post("/api/pi-clearing-account/runs/{run_id}/rows/{run_row_id}/step1-fix")
async def api_save_pi_clearing_step1_fix(
    run_id: str,
    run_row_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {
        "status": "ok",
        **await save_pi_clearing_step1_fix(user, run_id, run_row_id, payload if isinstance(payload, dict) else {}),
    }


@app.get("/api/pi-clearing-account/runs/{run_id}/dry-run.pdf")
def api_pi_clearing_account_dry_run_pdf(
    run_id: str,
    row_ids: str = Query("", alias="rowIds"),
    user: dict = Depends(require_panel_user),
):
    selected_row_ids = [item.strip() for item in str(row_ids or "").split(",") if item.strip()]
    pdf_bytes, filename = pi_clearing_dry_run_pdf(user, run_id, selected_row_ids)
    safe_filename = str(filename or "pi-clearing-dry-run.pdf").replace('"', "")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename}"'},
    )


@app.post("/api/pi-clearing-account/runs/{run_id}/credit-notes/{credit_note_record_id}/void")
async def api_void_pi_clearing_account_credit_note(
    run_id: str,
    credit_note_record_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    await request.body()
    return {"status": "ok", **await void_pi_clearing_credit_note(user, run_id, credit_note_record_id)}


@app.get("/api/payroll-headcount")
def api_payroll_headcount(user: dict = Depends(require_panel_user)):
    try:
        return {"status": "ok", **payroll_headcount_payload(user)}
    except HTTPException:
        raise
    except Exception as exc:
        error_id = str(uuid4())
        logger.exception(
            "payroll_headcount_payload_failed user_id=%s phase=route_handler error_id=%s",
            user.get("id"),
            error_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Payroll headcount payload failed due to an unexpected backend error.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
                "phase": "route_handler",
                "errorId": error_id,
            },
        ) from exc


@app.get("/api/juk-equity")
async def api_juk_equity(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **await juk_equity_payload(user)}


@app.post("/api/payroll-headcount/workspaces")
async def api_upsert_payroll_headcount_workspace(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    tenant_id = str((payload or {}).get("tenantId") or "").strip() if isinstance(payload, dict) else ""
    auto_sync = bool((payload or {}).get("autoSync")) if isinstance(payload, dict) else False
    workspace = upsert_payroll_headcount_workspace(user, tenant_id)
    if auto_sync:
        result = await sync_payroll_headcount_workspace(user, tenant_id)
        return {"status": "ok", **result}
    return {"status": "ok", "workspace": workspace, "payrollHeadcount": payroll_headcount_payload(user)}


@app.post("/api/payroll-headcount/workspaces/{tenant_id}/sync")
async def api_sync_payroll_headcount_workspace(tenant_id: str, user: dict = Depends(require_panel_user)):
    try:
        return {"status": "ok", **await sync_payroll_headcount_workspace(user, tenant_id)}
    except HTTPException:
        raise
    except Exception as exc:
        error_id = str(uuid4())
        logger.exception(
            "payroll_headcount_workspace_sync_failed user_id=%s tenant_id=%s phase=route_handler error_id=%s",
            user.get("id"),
            str(tenant_id or "").strip(),
            error_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Payroll headcount sync failed due to an unexpected backend error.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
                "tenantId": str(tenant_id or "").strip(),
                "phase": "route_handler",
                "errorId": error_id,
            },
        ) from exc


@app.post("/api/payroll-headcount/ignition-sync")
async def api_sync_payroll_headcount_ignition(request: Request, user: dict = Depends(require_panel_user)):
    await request.body()
    return {"status": "ok", **sync_payroll_headcount_with_ignition(user)}


@app.post("/api/me-report/settings")
async def api_update_me_report_settings(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **update_me_report_settings(user, payload)}


@app.post("/api/me-report/bulk-submissions")
async def api_bulk_upload_me_report_submissions(
    files: list[UploadFile] = File(...),
    manual_matches: str = Form("", alias="manualMatches"),
    tenant_id: str = Form("", alias="tenantId"),
    user: dict = Depends(require_panel_user),
):
    parsed_manual_matches = {}
    if str(manual_matches or "").strip():
        try:
            loaded_matches = json.loads(manual_matches)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Manual ME Report matches must be valid JSON.") from exc
        parsed_manual_matches = loaded_matches if isinstance(loaded_matches, dict) else {}
    file_payloads = []
    for index, file in enumerate(files):
        filename = file.filename or "overview-report"
        manual_match_value = parsed_manual_matches.get(str(index))
        if manual_match_value in (None, ""):
            manual_match_value = parsed_manual_matches.get(filename)
        manual_xero_contact_id = ""
        manual_client_name = ""
        if isinstance(manual_match_value, dict):
            mode = str(manual_match_value.get("mode") or "").strip().lower()
            if mode == "create_client" or bool(manual_match_value.get("createClient")):
                manual_client_name = str(
                    manual_match_value.get("clientName")
                    or manual_match_value.get("name")
                    or ""
                ).strip()
            else:
                manual_xero_contact_id = str(
                    manual_match_value.get("xeroContactId")
                    or manual_match_value.get("contactId")
                    or ""
                ).strip()
        else:
            manual_xero_contact_id = str(manual_match_value or "").strip()
        file_payloads.append(
            {
                "index": index,
                "filename": filename,
                "content_type": file.content_type or "application/octet-stream",
                "content": await file.read(),
                "manual_xero_contact_id": manual_xero_contact_id,
                "manual_client_name": manual_client_name,
            }
        )
    return {
        "status": "ok",
        **await bulk_upload_me_report_submission_pdfs(user, file_payloads, tenant_id=tenant_id or None),
    }


@app.post("/api/me-report/clients")
async def api_create_me_report_client(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": create_me_report_client(user, payload)}


@app.post("/api/me-report/clients/{client_id}")
async def api_update_me_report_client(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": update_me_report_client(user, client_id, payload)}


@app.post("/api/me-report/clients/{client_id}/ct-comps")
async def api_extract_me_report_ct_comps(
    client_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(require_panel_user),
):
    return {
        "status": "ok",
        "ctComps": await extract_me_report_ct_comps_loss(
            user,
            client_id,
            file.filename or "ct-computation.pdf",
            file.content_type or "application/pdf",
            await file.read(),
        ),
    }


@app.post("/api/me-report/clients/{client_id}/status")
async def api_update_me_report_client_status(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": update_me_report_client_status(user, client_id, payload)}


@app.delete("/api/me-report/clients/{client_id}")
def api_delete_me_report_client(client_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": delete_me_report_client(user, client_id)}


@app.post("/api/me-report/clients/{client_id}/connect-xero")
def api_connect_me_report_client_xero(client_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": connect_me_report_client_to_current_xero(user, client_id)}


@app.post("/api/me-report/clients/{client_id}/sync")
async def api_me_report_sync(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    sync_run, started = request_me_report_sync_run(user, client_id, payload if isinstance(payload, dict) else {})
    if started:
        _submit_background_job("me_report_sync", run_me_report_sync_job, dict(user), str(sync_run["id"]))
    return {"status": sync_run["status"], "started": started, "meReportSyncRun": serialize_me_report_sync_run(sync_run)}


@app.get("/api/me-report/sync/{sync_run_id}")
def api_me_report_sync_status(sync_run_id: str, user: dict = Depends(require_panel_user)):
    sync_run = get_me_report_sync_run(user, sync_run_id)
    payload = {"status": sync_run["status"], "meReportSyncRun": serialize_me_report_sync_run(sync_run)}
    if sync_run["status"] == "completed":
        payload["meReport"] = me_report_payload(user)
    return payload


@app.post("/api/me-report/mappings/{mapping_id}")
async def api_update_me_report_mapping(mapping_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": update_me_report_mapping(user, mapping_id, payload)}


@app.post("/api/me-report/exceptions/{exception_id}")
async def api_update_me_report_exception(exception_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": update_me_report_exception(user, exception_id, payload)}


@app.post("/api/me-report/exceptions/{exception_id}/merge-contact")
async def api_merge_me_report_duplicate_contact(exception_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": await merge_me_report_duplicate_contact(user, exception_id)}


@app.post("/api/me-report/clients/{client_id}/xero/contacts/merge")
async def api_merge_me_report_contacts(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": await merge_me_report_contacts(user, client_id, payload if isinstance(payload, dict) else {})}


@app.delete("/api/me-report/clients/{client_id}/xero/sales-invoices/{invoice_id}")
async def api_delete_me_report_draft_sales_invoice(client_id: str, invoice_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": await delete_me_report_draft_sales_invoice(user, client_id, invoice_id)}


@app.post("/api/me-report/clients/{client_id}/xero/purchases/mark-paid-personally")
async def api_mark_me_report_purchases_paid_personally(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": await mark_me_report_purchases_paid_personally(user, client_id, payload if isinstance(payload, dict) else {})}


@app.post("/api/me-report/clients/{client_id}/juk-invoice-check")
async def api_me_report_juk_invoice_check(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **await me_report_juk_invoice_check(user, client_id, payload if isinstance(payload, dict) else {})}


@app.delete("/api/me-report/clients/{client_id}/xero/unreconciled-transactions/{transaction_id}")
async def api_delete_me_report_unreconciled_transaction(client_id: str, transaction_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": await delete_me_report_unreconciled_transaction(user, client_id, transaction_id)}


@app.post("/api/me-report/clients/{client_id}/xero/nominal-accounts/rename")
async def api_rename_me_report_nominal_account(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": await rename_me_report_nominal_account(user, client_id, payload if isinstance(payload, dict) else {})}


@app.post("/api/me-report/clients/{client_id}/reports")
async def api_generate_me_report(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **generate_me_report(user, client_id, payload)}


@app.post("/api/me-report/reports/{report_id}/send")
async def api_send_me_report(report_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **await send_me_report_email(user, report_id, payload)}


@app.post("/api/me-report/reports/bulk-send")
async def api_bulk_send_me_report(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    report_ids = payload.get("reportIds") if isinstance(payload, dict) else []
    return {"status": "ok", **await bulk_send_me_report_emails(user, report_ids if isinstance(report_ids, list) else [])}


@app.post("/api/me-report/clients/{client_id}/submissions")
async def api_upload_me_report_submission(
    client_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(require_panel_user),
):
    content = await file.read()
    _, payload = await upload_me_report_submission_pdf(
        user,
        client_id,
        file.filename or "management-accounts",
        file.content_type or "application/octet-stream",
        content,
    )
    return {"status": "ok", "meReport": payload}


@app.delete("/api/me-report/submissions/{submission_id}")
def api_delete_me_report_submission(submission_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": delete_me_report_submission(user, submission_id)}


@app.get("/api/me-report/reports/{report_id}/download", response_class=HTMLResponse)
def api_download_me_report(report_id: str, user: dict = Depends(require_panel_user)):
    return HTMLResponse(me_report_report_html(user, report_id))


@app.get("/api/me-report/reports/{report_id}/download.pdf")
def api_download_me_report_pdf(report_id: str, user: dict = Depends(require_panel_user)):
    pdf_bytes, filename = me_report_report_pdf(user, report_id)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/bank-statements")
def api_bank_statements(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "bankStatements": bank_statement_payload(user)}


@app.post("/api/bank-statements/clients")
async def api_add_bank_statement_client(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "bankStatements": add_bank_statement_client(user, payload)}


@app.delete("/api/bank-statements/clients/{client_id}")
def api_delete_bank_statement_client(client_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "bankStatements": delete_bank_statement_client(user, client_id)}


@app.post("/api/bank-statements/clients/{client_id}/accounts")
async def api_create_bank_statement_account(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "bankStatements": create_bank_statement_account(user, client_id, payload)}


@app.post("/api/bank-statements/accounts/{account_id}")
async def api_update_bank_statement_account(account_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "bankStatements": update_bank_statement_account(user, account_id, payload)}


@app.post("/api/bank-statements/accounts/{account_id}/uploads")
async def api_upload_bank_statement(
    account_id: str,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_panel_user),
):
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload at least one PDF bank statement.")
    result = bank_statement_payload(user)
    for upload in files:
        content = await upload.read()
        result, account, upload_id = queue_bank_statement_upload(
            user,
            account_id,
            upload.filename or "bank-statement.pdf",
            upload.content_type or "application/pdf",
            content,
        )
        background_tasks.add_task(
            _process_bank_statement_upload,
            dict(user),
            account,
            upload_id,
            upload.filename or "bank-statement.pdf",
            upload.content_type or "application/pdf",
            content,
        )
    return {"status": "ok", "bankStatements": result}


@app.post("/api/bank-statements/uploads/{upload_id}/retry")
async def api_retry_bank_statement_upload(
    upload_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_panel_user),
):
    result, account, retry_upload_id, filename, content_type, file_bytes = queue_bank_statement_retry(user, upload_id)
    background_tasks.add_task(
        _process_bank_statement_upload,
        dict(user),
        account,
        retry_upload_id,
        filename,
        content_type,
        file_bytes,
        True,
    )
    return {"status": "ok", "bankStatements": result}


@app.delete("/api/bank-statements/uploads/{upload_id}")
def api_delete_bank_statement_upload(upload_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "bankStatements": delete_bank_statement_upload(user, upload_id)}


@app.get("/api/bank-statements/uploads/{upload_id}/source")
def api_bank_statement_upload_source(upload_id: str, user: dict = Depends(require_panel_user)):
    file_bytes, filename, content_type = bank_statement_upload_source_file(user, upload_id)
    safe_filename = str(filename or "bank-statement.pdf").replace('"', "")
    return Response(
        content=file_bytes,
        media_type=content_type or "application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename}"'},
    )


@app.post("/api/bank-statements/transactions/{transaction_id}/override")
async def api_override_bank_statement_transaction(transaction_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "bankStatements": override_bank_statement_transaction(user, transaction_id, payload)}


@app.post("/api/bank-statements/accounts/{account_id}/transactions/categorise")
async def api_categorise_bank_statement_transactions(account_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "bankStatements": categorise_bank_statement_transactions(user, account_id, payload)}


ESIGN_OUTSTANDING_STATUSES = ("draft", "prepared", "sent", "viewed")
ESIGN_COMPLETED_STATUSES = ("signed", "completed")


def _parse_esign_due_date(raw_value) -> str | None:
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Due date must be in YYYY-MM-DD format.") from exc


def _serialise_esign_row(row: dict) -> dict:
    return {
        "id": str(row.get("id") or ""),
        "clientId": str(row.get("client_id") or ""),
        "clientName": str(row.get("client_name") or ""),
        "documentType": str(row.get("document_type") or ""),
        "documentTitle": str(row.get("document_title") or ""),
        "recipientName": str(row.get("recipient_name") or ""),
        "recipientEmail": str(row.get("recipient_email") or ""),
        "message": str(row.get("message") or ""),
        "status": str(row.get("status") or "sent"),
        "dueDate": row.get("due_date").isoformat() if row.get("due_date") else None,
        "sentAt": row.get("sent_at").isoformat() if row.get("sent_at") else None,
        "viewedAt": row.get("viewed_at").isoformat() if row.get("viewed_at") else None,
        "completedAt": row.get("completed_at").isoformat() if row.get("completed_at") else None,
        "cancelledAt": row.get("cancelled_at").isoformat() if row.get("cancelled_at") else None,
        "createdAt": row.get("created_at").isoformat() if row.get("created_at") else None,
        "updatedAt": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        "externalProvider": str(row.get("external_provider") or "foxit_esign"),
        "externalRequestId": str(row.get("external_request_id") or ""),
    }


def _fetch_esign_requests(status_filter: str = "all") -> list[dict]:
    normalized = str(status_filter or "all").strip().lower()
    where_clause = ""
    params: tuple = ()
    if normalized == "outstanding":
        where_clause = "WHERE status = ANY(%s)"
        params = (list(ESIGN_OUTSTANDING_STATUSES),)
    elif normalized == "completed":
        where_clause = "WHERE status = ANY(%s)"
        params = (list(ESIGN_COMPLETED_STATUSES),)
    elif normalized not in ("all", ""):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported e-sign status filter.")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT *
                FROM esign_requests
                {where_clause}
                ORDER BY created_at DESC
                """,
                params,
            )
            rows = cursor.fetchall() or []
    return [_serialise_esign_row(row) for row in rows]


def _fetch_esign_request_by_id(request_id: str) -> dict | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM esign_requests
                WHERE id = %s
                LIMIT 1
                """,
                (request_id,),
            )
            row = cursor.fetchone()
    return row


def _fetch_esign_request_by_external_id(external_request_id: str) -> dict | None:
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM esign_requests
                WHERE external_request_id = %s
                LIMIT 1
                """,
                (external_request_id,),
            )
            row = cursor.fetchone()
    return row


def _is_local_esign_reference(external_request_id: str) -> bool:
    value = str(external_request_id or "").strip().lower()
    return value.startswith("mock_") or value.startswith("local_")


def _normalise_esign_status(raw_status: str | None) -> str:
    value = str(raw_status or "").strip().lower().replace(" ", "_").replace("-", "_")
    if value in {"draft", "prepared", "sent", "viewed", "signed", "completed", "declined", "expired", "cancelled", "failed"}:
        return value
    if value in {"in_progress", "processing", "out_for_signature", "waiting_for_signature"}:
        return "sent"
    if value in {"partially_signed"}:
        return "viewed"
    if value in {"done", "complete"}:
        return "completed"
    if value in {"canceled", "voided"}:
        return "cancelled"
    if value in {"rejected"}:
        return "declined"
    if value in {"error"}:
        return "failed"
    return "sent"


def _build_foxit_create_payload(payload: dict, *, client_name: str, recipient_email: str, document_title: str) -> dict:
    if isinstance(payload.get("foxitPayload"), dict):
        return payload["foxitPayload"]
    recipient_name = str(payload.get("recipientName") or "").strip() or client_name
    document_type = str(payload.get("documentType") or "").strip() or "other"
    message_text = str(payload.get("message") or "").strip()
    due_date = _parse_esign_due_date(payload.get("dueDate"))
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "document": {
            "title": document_title,
            "type": document_type,
        },
        "recipients": [
            {
                "name": recipient_name,
                "email": recipient_email,
                "role": "signer",
            }
        ],
        "message": message_text,
        "dueDate": due_date,
        "metadata": metadata,
    }


def _extract_foxit_status(payload: dict | None) -> str:
    if not isinstance(payload, dict):
        return "sent"
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = [
        payload.get("status"),
        payload.get("state"),
        payload.get("event"),
        data.get("status"),
        data.get("state"),
        data.get("event"),
        payload.get("event_type"),
        payload.get("eventType"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return _normalise_esign_status(value)
    return "sent"


def _update_esign_status_fields(request_id: str, status_value: str) -> None:
    now = datetime.now(timezone.utc)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE esign_requests
                SET status = %s,
                    viewed_at = CASE
                        WHEN %s = 'viewed' AND viewed_at IS NULL THEN %s
                        ELSE viewed_at
                    END,
                    completed_at = CASE
                        WHEN %s IN ('signed', 'completed') AND completed_at IS NULL THEN %s
                        ELSE completed_at
                    END,
                    cancelled_at = CASE
                        WHEN %s = 'cancelled' AND cancelled_at IS NULL THEN %s
                        ELSE cancelled_at
                    END,
                    updated_at = %s
                WHERE id = %s
                """,
                (status_value, status_value, now, status_value, now, status_value, now, now, request_id),
            )
        connection.commit()


@app.get("/api/esign/requests")
async def api_esign_requests(status_filter: str = Query(default="all", alias="status"), user: dict = Depends(require_panel_user)):
    return {"status": "ok", "requests": _fetch_esign_requests(status_filter)}


@app.post("/api/esign/requests")
async def api_create_esign_request(request: Request, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "create e-sign requests")
    payload = await request.json()
    client_name = str(payload.get("clientName") or "").strip()
    recipient_email = str(payload.get("recipientEmail") or "").strip()
    document_title = str(payload.get("documentTitle") or "").strip()
    if not client_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Client name is required.")
    if not recipient_email or "@" not in recipient_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A valid recipient email is required.")
    if not document_title:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Document title is required.")
    due_date = _parse_esign_due_date(payload.get("dueDate"))
    send_immediately = bool(payload.get("sendImmediately", True))
    provider_configured = foxit_esign_configured()
    now = datetime.now(timezone.utc)
    request_status = "prepared" if not send_immediately else "sent"
    sent_at = now if send_immediately else None
    external_request_id = f"local_{uuid4().hex}"
    metadata_payload = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metadata_payload = {**metadata_payload}
    metadata_payload["providerConfigured"] = provider_configured
    metadata_payload["foxitEnvironment"] = get_settings().foxit_env

    if send_immediately and provider_configured:
        foxit_payload = _build_foxit_create_payload(
            payload,
            client_name=client_name,
            recipient_email=recipient_email,
            document_title=document_title,
        )
        try:
            provider_result = await foxit_esign_send_request(foxit_payload)
            provider_request_id = str(provider_result.get("externalRequestId") or "").strip()
            external_request_id = provider_request_id or f"foxit_pending_{uuid4().hex}"
            request_status = _extract_foxit_status(provider_result.get("raw"))
            sent_at = now if request_status in {"sent", "viewed", "signed", "completed"} else None
            metadata_payload["foxitPayload"] = foxit_payload
            metadata_payload["foxitResponse"] = provider_result.get("raw")
        except FoxitESignConfigurationError as exc:
            request_status = "prepared"
            sent_at = None
            metadata_payload["foxitWarning"] = str(exc)
        except HTTPException as exc:
            request_status = "failed"
            sent_at = None
            metadata_payload["foxitError"] = exc.detail
            logger.warning("Foxit create request failed: %s", exc.detail)
    elif send_immediately and not provider_configured:
        request_status = "prepared"
        sent_at = None
        metadata_payload["foxitWarning"] = "Foxit credentials are not configured in this environment."

    created_id = None
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO esign_requests (
                    user_id,
                    client_id,
                    client_name,
                    document_type,
                    document_title,
                    recipient_name,
                    recipient_email,
                    message,
                    status,
                    due_date,
                    sent_at,
                    external_provider,
                    external_request_id,
                    metadata_json,
                    created_at,
                    updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'foxit_esign', %s, %s::jsonb, %s, %s
                )
                RETURNING id
                """,
                (
                    user.get("id"),
                    str(payload.get("clientId") or "").strip(),
                    client_name,
                    str(payload.get("documentType") or "").strip(),
                    document_title,
                    str(payload.get("recipientName") or "").strip(),
                    recipient_email,
                    str(payload.get("message") or "").strip(),
                    request_status,
                    due_date,
                    sent_at,
                    external_request_id,
                    json.dumps(metadata_payload),
                    now,
                    now,
                ),
            )
            row = cursor.fetchone() or {}
            created_id = str(row.get("id") or "")
        connection.commit()
    created = _fetch_esign_request_by_id(created_id)
    if not created:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to create e-sign request.")
    response_payload = {"status": "ok", "request": _serialise_esign_row(created), "providerConfigured": provider_configured}
    if request_status == "failed":
        response_payload["warning"] = "Foxit send failed; request was saved in failed status."
    return response_payload


@app.post("/api/esign/requests/{request_id}/cancel")
async def api_cancel_esign_request(request_id: str, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "cancel e-sign requests")
    row = _fetch_esign_request_by_id(request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="E-sign request not found.")
    current_status = str(row.get("status") or "").strip().lower()
    if current_status in ESIGN_COMPLETED_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Completed e-sign requests cannot be cancelled.")
    external_request_id = str(row.get("external_request_id") or "").strip()
    if external_request_id and not _is_local_esign_reference(external_request_id):
        if not foxit_esign_configured():
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Foxit eSign credentials are not configured.")
        await foxit_esign_cancel_request(external_request_id)
    _update_esign_status_fields(request_id, "cancelled")
    updated = _fetch_esign_request_by_id(request_id)
    return {"status": "ok", "request": _serialise_esign_row(updated or row)}


@app.post("/api/esign/requests/{request_id}/resend")
async def api_resend_esign_request(request_id: str, user: dict = Depends(require_panel_user)):
    require_panel_write_user(user, "resend e-sign requests")
    row = _fetch_esign_request_by_id(request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="E-sign request not found.")
    current_status = str(row.get("status") or "").strip().lower()
    if current_status in ESIGN_COMPLETED_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Completed e-sign requests cannot be resent.")
    if current_status == "cancelled":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cancelled e-sign requests cannot be resent.")
    external_request_id = str(row.get("external_request_id") or "").strip()
    if external_request_id and not _is_local_esign_reference(external_request_id):
        if not foxit_esign_configured():
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Foxit eSign credentials are not configured.")
        await foxit_esign_resend_request(external_request_id)
    now = datetime.now(timezone.utc)
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE esign_requests
                SET status = 'sent',
                    sent_at = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (now, now, request_id),
            )
        connection.commit()
    updated = _fetch_esign_request_by_id(request_id)
    return {"status": "ok", "request": _serialise_esign_row(updated or row)}


@app.get("/api/esign/requests/{request_id}/status")
async def api_esign_request_status(request_id: str, refresh: bool = Query(default=False), user: dict = Depends(require_panel_user)):
    row = _fetch_esign_request_by_id(request_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="E-sign request not found.")
    provider_payload = None
    external_request_id = str(row.get("external_request_id") or "").strip()
    if refresh and external_request_id and not _is_local_esign_reference(external_request_id) and foxit_esign_configured():
        try:
            provider_payload = await foxit_esign_status_request(external_request_id)
            live_status = _extract_foxit_status(provider_payload)
            _update_esign_status_fields(request_id, live_status)
            row = _fetch_esign_request_by_id(request_id) or row
        except HTTPException as exc:
            logger.warning("Foxit status refresh failed for %s: %s", request_id, exc.detail)
    return {
        "status": "ok",
        "request": _serialise_esign_row(row),
        "providerStatus": provider_payload,
    }


@app.post("/api/webhooks/foxit-esign")
async def api_foxit_esign_webhook(request: Request):
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook payload must be a JSON object.")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    external_request_id = str(
        payload.get("requestId")
        or payload.get("request_id")
        or payload.get("documentId")
        or payload.get("document_id")
        or data.get("requestId")
        or data.get("id")
        or ""
    ).strip()
    if not external_request_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook payload did not include request ID.")
    row = _fetch_esign_request_by_external_id(external_request_id)
    if row is None:
        return {"status": "ignored", "reason": "unknown_request_id", "requestId": external_request_id}
    next_status = _extract_foxit_status(payload)
    _update_esign_status_fields(str(row.get("id")), next_status)
    return {"status": "ok", "requestId": external_request_id, "appliedStatus": next_status}


@app.get("/api/supplier-reconciliation")
async def api_supplier_reconciliation(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "supplierReconciliation": await supplier_reconciliation_payload(user)}


@app.get("/api/supplier-reconciliation/contacts")
async def api_supplier_reconciliation_contacts(tenantId: str = Query(default=""), user: dict = Depends(require_panel_user)):
    return {"status": "ok", "supplierReconciliation": await supplier_reconciliation_contact_options_payload(user, tenantId or None)}


@app.post("/api/supplier-reconciliation/clients")
async def api_add_supplier_reconciliation_client(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "supplierReconciliation": await add_supplier_reconciliation_client(user, payload)}


@app.delete("/api/supplier-reconciliation/clients/{client_id}")
async def api_delete_supplier_reconciliation_client(client_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "supplierReconciliation": await delete_supplier_reconciliation_client(user, client_id)}


@app.post("/api/supplier-reconciliation/extract")
async def api_supplier_reconciliation_extract(
    xeroContactId: str | None = Form(default=None),
    tenantId: str | None = Form(default=None),
    file: UploadFile = File(...),
    user: dict = Depends(require_panel_user),
):
    file_bytes = await file.read()
    result = await supplier_reconciliation_extract(
        user,
        xeroContactId or "",
        tenantId or "",
        file.filename or "supplier-statement.pdf",
        file.content_type or "application/pdf",
        file_bytes,
    )
    return {"status": "ok", "supplierReconciliation": result}


@app.post("/api/supplier-reconciliation/email")
async def api_supplier_reconciliation_email(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "supplierReconciliationEmail": send_supplier_reconciliation_email(user, payload)}


@app.get("/api/supplier-payments")
async def api_supplier_payments(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **(await supplier_payments_payload(user))}


@app.post("/api/supplier-payments/settle")
async def api_supplier_payments_settle(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **(await supplier_payments_settle(user, payload))}


@app.post("/api/supplier-payments/settle-via-barclays")
async def api_supplier_payments_settle_via_barclays(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **(await supplier_payments_settle_via_barclays(user, payload))}


@app.post("/api/supplier-payments/remittance-advices")
async def api_supplier_payments_remittance_advices(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **send_supplier_payment_remittance_advices(user, payload)}


@app.get("/api/customers/{customer_id}/xero-transactions")
async def api_customer_xero_transactions(
    customer_id: str,
    pageLimit: int = Query(default=0, ge=0, le=25),
    includeDiagnostics: bool = Query(default=False),
    user: dict = Depends(require_panel_user),
):
    return await customer_xero_transactions(
        customer_id,
        user,
        page_limit=pageLimit if pageLimit > 0 else None,
        include_diagnostics=includeDiagnostics,
    )


@app.post("/api/customers/{customer_id}/xero-transactions/actions")
async def api_code_breaker_xero_transaction_action(customer_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return await code_breaker_apply_xero_transaction_action(customer_id, user, payload)


@app.get("/api/xero/vat-returns")
async def api_xero_vat_returns(tenantId: str = Query(default=""), user: dict = Depends(require_panel_user)):
    return {"status": "ok", **await xero_vat_returns_payload(user, tenant_id=tenantId or None)}


@app.get("/api/xero/vat-returns/{period_end}/transactions")
async def api_xero_vat_return_transactions_by_tenant(
    period_end: str,
    tenantId: str = Query(default=""),
    periodStart: str = Query(default=""),
    refresh: bool = Query(default=False),
    user: dict = Depends(require_panel_user),
):
    return await xero_vat_return_transactions_by_tenant(
        period_end,
        user,
        tenant_id=tenantId or "",
        period_start=periodStart or None,
        refresh=refresh,
    )


@app.get("/api/xero/tenants/{tenant_id}/vat-coded-transactions")
async def api_xero_vat_coded_transactions_by_tenant(
    tenant_id: str,
    refresh: bool = Query(default=False),
    user: dict = Depends(require_panel_user),
):
    return await xero_vat_coded_transactions_by_tenant(user, tenant_id=tenant_id, refresh=refresh)


@app.post("/api/xero/tenants/{tenant_id}/vat-coded-transactions/set-no-vat")
async def api_xero_set_tenant_transactions_no_vat(
    tenant_id: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    return await xero_set_tenant_transactions_no_vat(user, tenant_id=tenant_id, payload=payload if isinstance(payload, dict) else {})


@app.get("/api/customers/{customer_id}/vat-returns/{period_end}/transactions")
async def api_customer_vat_return_transactions(
    customer_id: str,
    period_end: str,
    tenantId: str = Query(default=""),
    periodStart: str = Query(default=""),
    refresh: bool = Query(default=False),
    user: dict = Depends(require_panel_user),
):
    return await customer_vat_return_transactions(
        customer_id,
        period_end,
        user,
        tenant_id=tenantId or None,
        period_start=periodStart or None,
        refresh=refresh,
    )


@app.get("/api/customers/{customer_id}/vat-returns/{period_end}/no-vat-suggestions")
async def api_customer_vat_no_vat_suggestions(
    customer_id: str,
    period_end: str,
    tenantId: str = Query(default=""),
    periodStart: str = Query(default=""),
    refresh: bool = Query(default=False),
    user: dict = Depends(require_panel_user),
):
    return await vat_no_vat_suggestions(
        customer_id,
        period_end,
        user,
        tenant_id=tenantId or None,
        period_start=periodStart or None,
        refresh=refresh,
    )


@app.get("/api/customers/{customer_id}/vat-returns/{period_end}/unreconciled")
async def api_customer_vat_return_unreconciled_transactions(
    customer_id: str,
    period_end: str,
    tenantId: str = Query(default=""),
    periodStart: str = Query(default=""),
    user: dict = Depends(require_panel_user),
):
    return await customer_vat_return_unreconciled_transactions(
        customer_id,
        period_end,
        user,
        tenant_id=tenantId or None,
        period_start=periodStart or None,
    )


@app.delete("/api/customers/{customer_id}/vat-returns/{period_end}/unreconciled/{transaction_id}")
async def api_delete_customer_vat_unreconciled_transaction(
    customer_id: str,
    period_end: str,
    transaction_id: str,
    tenantId: str = Query(default=""),
    periodStart: str = Query(default=""),
    user: dict = Depends(require_panel_user),
):
    return await delete_customer_vat_unreconciled_transaction(
        customer_id,
        period_end,
        transaction_id,
        user,
        tenant_id=tenantId or None,
        period_start=periodStart or None,
    )


@app.post("/api/customers/{customer_id}/vat-returns/{period_end}/apply-edits")
async def api_apply_customer_vat_transaction_edits(
    customer_id: str,
    period_end: str,
    request: Request,
    user: dict = Depends(require_panel_user),
):
    payload = await request.json()
    return await apply_customer_vat_transaction_edits(customer_id, period_end, user, payload)


@app.post("/api/customers/{customer_id}/allocations")
async def api_customer_allocate_credit(customer_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return await allocate_customer_credit(user, customer_id, payload)


@app.post("/api/invoices/{invoice_id}/notes")
async def api_invoice_add_note(invoice_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    body = str(payload.get("body", "")).strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Note body is required.")
    note_body = add_note(invoice_id, user, body)
    xero_note = await sync_invoice_note_to_xero(invoice_id, user, note_body)
    return {
        "status": "ok",
        "xeroNoteSynced": xero_note["synced"],
        "xeroNoteError": xero_note.get("error", ""),
        "invoice": invoice_detail(invoice_id),
    }


@app.post("/api/customers/{customer_id}/notes")
async def api_customer_add_note(customer_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    body = str(payload.get("body", "")).strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Note body is required.")
    add_customer_note(customer_id, user, body)
    xero_note = await sync_customer_note_to_xero(customer_id, user, body)
    return {
        "status": "ok",
        "xeroNoteSynced": xero_note["synced"],
        "xeroNoteError": xero_note.get("error", ""),
        "panel": panel_payload(user),
    }


@app.post("/api/invoices/{invoice_id}/promises")
async def api_invoice_add_promise(invoice_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    promised_amount = str(payload.get("promisedAmount", "")).strip()
    promised_date = str(payload.get("promisedDate", "")).strip()
    note = str(payload.get("note", "")).strip()
    if not promised_amount or not promised_date:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Promised amount and date are required.")
    add_promise(invoice_id, user, promised_amount, promised_date, note)
    xero_note = await sync_invoice_promise_to_xero(invoice_id, user, promised_amount, promised_date, note)
    return {
        "status": "ok",
        "xeroNoteSynced": xero_note["synced"],
        "xeroNoteError": xero_note.get("error", ""),
        "invoice": invoice_detail(invoice_id),
    }


@app.post("/api/customers/{customer_id}/payment-plans")
async def api_customer_create_payment_plan(customer_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    invoice_ids = payload.get("invoiceIds") or []
    try:
        duration_months = int(payload.get("durationMonths") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment plan duration is required.") from exc
    note = str(payload.get("note", "")).strip()
    plan = create_payment_plan(customer_id, user, invoice_ids, duration_months, note)
    xero_note = await sync_payment_plan_to_xero(customer_id, user, plan)
    return {
        "status": "ok",
        "paymentPlan": plan,
        "xeroNoteSynced": xero_note["synced"],
        "xeroNoteError": xero_note.get("error", ""),
        "panel": panel_payload(user),
    }


@app.post("/api/invoices/{invoice_id}/status")
async def api_invoice_set_status(invoice_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    status_value = str(payload.get("statusValue", "")).strip()
    note = str(payload.get("note", "")).strip()
    if not status_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status is required.")
    update_control_status(invoice_id, user, status_value, note)
    xero_note = await sync_invoice_status_to_xero(invoice_id, user, status_value, note)
    return {
        "status": "ok",
        "xeroNoteSynced": xero_note["synced"],
        "xeroNoteError": xero_note.get("error", ""),
        "invoice": invoice_detail(invoice_id),
    }


@app.post("/api/invoices/bulk-status")
async def api_bulk_invoice_status(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    result = await bulk_update_invoice_status(
        user,
        payload.get("invoiceIds") or [],
        payload.get("statusValue") or "",
        payload.get("note") or "",
    )
    return {"status": "ok", **result, "panel": panel_payload(user)}


@app.exception_handler(XeroConfigurationError)
async def xero_configuration_error_handler(_, exc: XeroConfigurationError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": str(exc)})
