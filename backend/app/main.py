import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from .auth import (
    allowed_panel_origins,
    approve_device_code,
    clear_session_cookie,
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
    dashboard_summary as companies_house_dashboard_summary,
    delete_company,
    export_companies_house_support_report,
    export_submission_attempts_csv,
    get_companies_house_settings,
    get_company_detail,
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
    submission_reconciliation_report,
    start_companies_house_auto_sync_worker,
    sync_companies_house_companies,
    test_companies_house_connection,
    upload_auth_code_register_csv,
    update_company,
    validate_company_secretarial_filing,
)
from .config import get_settings
from .database import ensure_schema, get_connection
from .security import create_session
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
    void_pi_clearing_credit_note,
    xero_lock_date_overview_payload,
    xero_lock_date_mismatch_payload,
    xero_lock_date_mismatch_pdf,
    xero_chart_of_accounts_payload,
    xero_vat_returns_payload,
    xero_vat_return_transactions_by_tenant,
    xero_scope_audit_payload,
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
    juksib_get_batch,
    juksib_import_batch,
    juksib_list_batches,
    juksib_delete_batch,
    juksib_publish_batch,
    juksib_source_invoice_pdf as juksib_source_invoice_pdf_bytes,
    start_juksib_automation_worker,
    update_juksib_automation_settings,
    vault_analyze_files,
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
    send_hmrc_64_8_reminder,
    submit_hmrc_64_8_request,
    update_hmrc_64_8_request,
)
from .xero import XeroConfigurationError, exchange_code_for_tokens, fetch_connections, fetch_user_profile, store_login

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_PANEL_DIR = BASE_DIR.parent / "WebPanel"
LEGACY_CONSOLE_PATH = BASE_DIR / "static" / "console.html"
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
logger = logging.getLogger(__name__)
BACKGROUND_JOB_MAX_WORKERS = 8
background_job_executor = ThreadPoolExecutor(
    max_workers=BACKGROUND_JOB_MAX_WORKERS,
    thread_name_prefix="panel-bg",
)


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
    if user and user.get("id"):
        try:
            get_xero_connection_for_user(user["id"])
            return user
        except HTTPException:
            pass

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT users.*
                FROM xero_connections
                JOIN users ON users.id = xero_connections.user_id
                ORDER BY xero_connections.updated_at DESC NULLS LAST, xero_connections.created_at DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
        connection.commit()
    return row


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
    redirect_to = add_fragment_params(redirect_to, {"panel_session": session_token})
    response = RedirectResponse(redirect_to, status_code=status.HTTP_302_FOUND)
    set_session_cookie(response, session_token)
    return response


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return "application/json" in accept or "application/json" in content_type


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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    user = current_user_from_request(request)
    if user and user.get("id"):
        response = xero_connected_redirect(request, "/")
        if response:
            return response
    return templates.TemplateResponse(request, "login.html", template_context(request))


@app.get("/auth/xero/start")
def auth_xero_start(
    request: Request,
    redirect_to: str = "/",
    force: int = 0,
    include_payroll: int = 0,
):
    redirect_to = normalise_oauth_redirect(redirect_to)
    if not force:
        response = xero_connected_redirect(request, redirect_to)
        if response:
            return response
    state_token = start_oauth_state(redirect_to=redirect_to)
    return RedirectResponse(
        xero_authorize_url(
            state_token,
            prompt_consent=bool(force),
            include_payroll_scopes=bool(include_payroll),
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

        session_token = create_session(login["user"]["id"], "Web panel")
        sync_run, sync_started = queue_initial_xero_sync(login["user"])
        redirect_to = normalise_oauth_redirect(state_row["redirect_to"] or "/")
        redirect_params = {"xero": "connected"}
        tenant_sync_summary = login.get("tenant_sync_summary") or {}
        redirect_params["xero_new_tenants"] = str(int(tenant_sync_summary.get("new_tenants_count") or 0))
        redirect_params["xero_refreshed_tenants"] = str(int(tenant_sync_summary.get("refreshed_existing_tenants_count") or 0))
        redirect_params["xero_total_tenants"] = str(int(tenant_sync_summary.get("total_tenants") or 0))
        new_tenant_names = tenant_sync_summary.get("new_tenant_names") or []
        if new_tenant_names:
            redirect_params["xero_new_tenant_names"] = "|".join(str(name or "").strip() for name in new_tenant_names if str(name or "").strip())
        if sync_run:
            redirect_params["sync_run"] = str(sync_run["id"])
            redirect_params["sync_started"] = "1" if sync_started else "0"
        redirect_to = add_query_params(redirect_to, redirect_params)
        redirect_to = add_fragment_params(redirect_to, {"panel_session": session_token})
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
def logout():
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_session_cookie(response)
    return response


@app.get("/", response_class=HTMLResponse)
def console_page():
    webpanel_index = WEB_PANEL_DIR / "index.html"
    if webpanel_index.exists():
        return FileResponse(webpanel_index)
    logger.warning("WebPanel index not found at %s; serving legacy console", webpanel_index)
    return FileResponse(LEGACY_CONSOLE_PATH)


@app.get("/styles.css")
def webpanel_styles():
    styles_path = WEB_PANEL_DIR / "styles.css"
    if styles_path.exists():
        return FileResponse(styles_path)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="styles.css not found.")


@app.get("/app.js")
def webpanel_script():
    script_path = WEB_PANEL_DIR / "app.js"
    if script_path.exists():
        return FileResponse(script_path)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="app.js not found.")


@app.get("/standalone.html", response_class=HTMLResponse)
def webpanel_standalone():
    standalone_path = WEB_PANEL_DIR / "standalone.html"
    if standalone_path.exists():
        return FileResponse(standalone_path)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="standalone.html not found.")


@app.get("/console", response_class=HTMLResponse)
def legacy_console_page():
    return FileResponse(LEGACY_CONSOLE_PATH)


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
def api_panel_session(request: Request):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in allowed_panel_origins():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This panel origin is not allowed.")

    user = reusable_xero_user(request)
    if not user:
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
    limit: int = Query(120, ge=1, le=300),
    user: dict = Depends(require_panel_user),
):
    return deployment_updates_payload(user, limit=limit)


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
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {"status": "ok", "contactArchive": await contact_archive_bulk_archive_payload(user, payload)}


@app.post("/api/contact-archive/client-register-sync")
async def api_contact_archive_client_register_sync(request: Request, user: dict = Depends(require_panel_write_user)):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {"status": "ok", "contactArchiveSync": await contact_archive_client_register_sync_payload(user, payload)}


@app.post("/api/xero/posting-settings")
async def api_xero_posting_settings(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    settings_payload = await save_posting_settings(user, payload)
    return {"status": "ok", **settings_payload, "panel": panel_payload(user)}


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
    apply_deletes = bool(payload.get("applyDeletes", True))
    result = commit_auth_code_register_import(user, preview, apply_deletes=apply_deletes)
    return {"status": "ok", "result": result}


@app.get("/api/companies-house/auth-code-register")
def api_companies_house_auth_code_register_list(
    limit: int = Query(300, ge=20, le=1000),
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
    return {"status": "ok", **micro_analyzer_clients_payload(user)}


@app.get("/api/vault")
def api_vault(
    search: str = Query("", alias="search"),
    folder: str = Query("", alias="folder"),
    tag: str = Query("", alias="tag"),
    user: dict = Depends(require_panel_user),
):
    return {"status": "ok", **vault_payload(user, search=search, folder=folder, tag=tag)}


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


@app.get("/api/pi-clearing-account")
def api_pi_clearing_account(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **pi_clearing_payload(user)}


@app.post("/api/pi-clearing-account/run")
async def api_run_pi_clearing_account_workflow(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    return {"status": "ok", **await run_pi_clearing_workflow(user, payload if isinstance(payload, dict) else {})}


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
    return {"status": "ok", **payroll_headcount_payload(user)}


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
    return {"status": "ok", **await sync_payroll_headcount_workspace(user, tenant_id)}


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
def api_me_report_sync(client_id: str, user: dict = Depends(require_panel_user)):
    sync_run, started = request_me_report_sync_run(user, client_id)
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
