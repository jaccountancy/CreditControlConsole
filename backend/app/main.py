import asyncio
import json
import logging
import threading
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
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
    require_user,
    set_session_cookie,
    start_oauth_state,
    xero_authorize_url,
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
    allocate_customer_credit,
    add_bank_statement_client,
    create_bad_debt_write_offs,
    create_bank_statement_account,
    create_late_payment_charges,
    create_payment_plan,
    customer_detail,
    customer_xero_transactions,
    dashboard_payload,
    disconnect_xero,
    factory_reset_console,
    get_xero_connection_for_user,
    get_operation_run,
    get_ignition_sync_run,
    insights_payload,
    ignition_payload,
    invoice_detail,
    install_sync_signal_handlers,
    add_jashflow_payment,
    create_jashflow_loan,
    jashflow_payload,
    post_jashflow_interest_invoice,
    save_jashflow_settings,
    list_customers,
    list_developer_logs,
    connect_me_report_client_to_current_xero,
    create_me_report_client,
    generate_me_report,
    get_me_report_sync_run,
    normalise_sync_options,
    panel_payload,
    get_sync_run,
    me_report_payload,
    me_report_report_html,
    record_sync_start_failure,
    request_me_report_sync_run,
    request_sync_run,
    request_ignition_sync_run,
    run_ignition_sync_job,
    run_me_report_sync_job,
    request_operation_run,
    run_invoice_operation_job,
    run_sync,
    run_sync_job,
    serialize_sync_run,
    serialize_ignition_sync_run,
    serialize_me_report_sync_run,
    serialize_operation_run,
    sync_customer_note_to_xero,
    sync_invoice_promise_to_xero,
    sync_invoice_note_to_xero,
    sync_invoice_status_to_xero,
    sync_payment_plan_to_xero,
    sync_run_has_working_data,
    update_control_status,
    update_me_report_exception,
    update_me_report_mapping,
    bank_statement_payload,
    bulk_update_invoice_status,
    merge_me_report_duplicate_contact,
    upload_bank_statement_pdf,
)
from .ignition import (
    IgnitionConfigurationError,
    create_pkce_verifier,
    exchange_ignition_code_for_tokens,
    ignition_authorize_url,
    store_ignition_connection,
)
from .xero import XeroConfigurationError, exchange_code_for_tokens, fetch_connections, fetch_user_profile, store_login

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Credit Control Backend", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_panel_origins()),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
logger = logging.getLogger(__name__)


@app.on_event("startup")
def startup() -> None:
    ensure_schema()
    install_sync_signal_handlers()


def template_context(request: Request, **extra):
    return {"request": request, "user": current_user_from_request(request), **extra}


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return "application/json" in accept or "application/json" in content_type


def xero_login_error_response(
    message: str,
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
    provider: str = "Xero",
) -> HTMLResponse:
    safe_message = escape(message)
    safe_provider = escape(provider)
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
                <a href="/login">Back to login</a>
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
        try:
            get_xero_connection_for_user(user["id"])
            return RedirectResponse(add_query_params("/", {"xero": "connected"}), status_code=status.HTTP_302_FOUND)
        except HTTPException:
            pass
    return templates.TemplateResponse(request, "login.html", template_context(request))


@app.get("/auth/xero/start")
def auth_xero_start(redirect_to: str = "/"):
    redirect_to = normalise_oauth_redirect(redirect_to)
    state_token = start_oauth_state(redirect_to=redirect_to)
    return RedirectResponse(xero_authorize_url(state_token), status_code=status.HTTP_302_FOUND)


@app.get("/auth/xero/connected")
def auth_xero_connected():
    return RedirectResponse(add_query_params("/", {"xero": "connected"}), status_code=status.HTTP_302_FOUND)


def queue_ignition_sync(user: dict) -> tuple[dict | None, bool]:
    try:
        sync_run, started = request_ignition_sync_run(user)
        if started:
            threading.Thread(target=run_ignition_sync_job, args=(dict(user), str(sync_run["id"])), daemon=True).start()
        return sync_run, started
    except Exception:
        logger.exception("Unable to queue Ignition sync")
        return None, False


@app.get("/auth/ignition/start")
def auth_ignition_start(redirect_to: str = "/", user: dict = Depends(require_panel_user)):
    redirect_to = normalise_oauth_redirect(redirect_to)
    verifier = create_pkce_verifier()
    state_token = start_oauth_state(redirect_to=redirect_to, provider="ignition", code_verifier=verifier)
    try:
        authorize_url = ignition_authorize_url(state_token, verifier)
    except IgnitionConfigurationError as exc:
        return xero_login_error_response(str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR, provider="Ignition")
    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@app.get("/auth/ignition/callback")
async def auth_ignition_callback(request: Request, code: str, state: str):
    try:
        user = current_user_from_request(request)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to Jenius before connecting Ignition.")
        state_row = consume_oauth_state(state)
        if state_row.get("provider") not in ("ignition",):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Ignition OAuth state.")
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
            threading.Thread(target=run_sync_job, args=(dict(user), str(sync_run["id"])), daemon=True).start()
        return sync_run, started
    except Exception as exc:
        logger.exception("Unable to queue initial Xero sync after login")
        record_sync_start_failure(user, exc)
        return None, False


@app.get("/auth/xero/callback")
async def auth_xero_callback(request: Request, code: str, state: str):
    try:
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
    return FileResponse(BASE_DIR / "static" / "console.html")


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
    add_note(invoice_id, user, body)
    await sync_invoice_note_to_xero(invoice_id, user, body)
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
    active_sync_run = active_sync_run_for_user(user)
    return {
        **panel_payload(user),
        "activeSyncRun": serialize_sync_run(active_sync_run) if active_sync_run else None,
    }


@app.get("/api/insights")
async def api_insights(user: dict = Depends(require_panel_user)):
    return await insights_payload(user)


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
            threading.Thread(target=run_sync_job, args=(dict(user), str(sync_run["id"]), sync_options), daemon=True).start()
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
    payload = {
        "status": sync_run["status"],
        "syncRun": serialize_sync_run(sync_run),
        "workingDataReady": sync_run_has_working_data(sync_run),
    }
    if sync_run["status"] == "completed":
        try:
            payload["panel"] = panel_payload(user)
        except Exception as exc:
            logger.exception("Unable to build completed sync panel payload")
            payload["panelError"] = {
                "message": "Sync completed, but the refreshed panel payload could not be built.",
                "error": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
            }
    return payload


@app.get("/api/developer/logs")
def api_developer_logs(limit: int = Query(120, ge=1, le=300), user: dict = Depends(require_panel_user)):
    return {"logs": list_developer_logs(user, limit)}


@app.post("/api/xero/disconnect")
def api_xero_disconnect(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **disconnect_xero(user)}


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
    threading.Thread(
        target=lambda: asyncio.run(
            run_invoice_operation_job(dict(user), str(operation_run["id"]), "late_payment_charges", invoice_ids, options)
        ),
        daemon=True,
    ).start()
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
    threading.Thread(
        target=lambda: asyncio.run(
            run_invoice_operation_job(dict(user), str(operation_run["id"]), "bad_debt_write_offs", invoice_ids)
        ),
        daemon=True,
    ).start()
    return {"status": "queued", "operationRun": serialize_operation_run(operation_run)}


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


@app.post("/api/jashflow/loans/{loan_id}/payments")
async def api_add_jashflow_payment(loan_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "jashflow": add_jashflow_payment(user, loan_id, payload)}


@app.post("/api/jashflow/settings")
async def api_save_jashflow_settings(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "jashflow": save_jashflow_settings(user, payload)}


@app.post("/api/jashflow/interest-posts")
async def api_post_jashflow_interest(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **await post_jashflow_interest_invoice(user, payload)}


@app.get("/api/ignition")
def api_ignition(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "ignition": ignition_payload(user)}


@app.post("/api/ignition/sync")
def api_ignition_sync(user: dict = Depends(require_panel_user)):
    sync_run, started = request_ignition_sync_run(user)
    if started:
        threading.Thread(target=run_ignition_sync_job, args=(dict(user), str(sync_run["id"])), daemon=True).start()
    return {"status": sync_run["status"], "started": started, "ignitionSyncRun": serialize_ignition_sync_run(sync_run)}


@app.get("/api/ignition/sync/{sync_run_id}")
def api_ignition_sync_status(sync_run_id: str, user: dict = Depends(require_panel_user)):
    sync_run = get_ignition_sync_run(user, sync_run_id)
    payload = {"status": sync_run["status"], "ignitionSyncRun": serialize_ignition_sync_run(sync_run)}
    if sync_run["status"] == "completed":
        payload["ignition"] = ignition_payload(user)
    return payload


@app.get("/api/me-report")
def api_me_report(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": me_report_payload(user)}


@app.post("/api/me-report/clients")
async def api_create_me_report_client(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "meReport": create_me_report_client(user, payload)}


@app.post("/api/me-report/clients/{client_id}/connect-xero")
def api_connect_me_report_client_xero(client_id: str, user: dict = Depends(require_panel_user)):
    return {"status": "ok", "meReport": connect_me_report_client_to_current_xero(user, client_id)}


@app.post("/api/me-report/clients/{client_id}/sync")
def api_me_report_sync(client_id: str, user: dict = Depends(require_panel_user)):
    sync_run, started = request_me_report_sync_run(user, client_id)
    if started:
        threading.Thread(target=run_me_report_sync_job, args=(dict(user), str(sync_run["id"])), daemon=True).start()
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


@app.post("/api/me-report/clients/{client_id}/reports")
async def api_generate_me_report(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", **generate_me_report(user, client_id, payload)}


@app.get("/api/me-report/reports/{report_id}/download", response_class=HTMLResponse)
def api_download_me_report(report_id: str, user: dict = Depends(require_panel_user)):
    return HTMLResponse(me_report_report_html(user, report_id))


@app.get("/api/bank-statements")
def api_bank_statements(user: dict = Depends(require_panel_user)):
    return {"status": "ok", "bankStatements": bank_statement_payload(user)}


@app.post("/api/bank-statements/clients")
async def api_add_bank_statement_client(request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "bankStatements": add_bank_statement_client(user, payload)}


@app.post("/api/bank-statements/clients/{client_id}/accounts")
async def api_create_bank_statement_account(client_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    return {"status": "ok", "bankStatements": create_bank_statement_account(user, client_id, payload)}


@app.post("/api/bank-statements/accounts/{account_id}/uploads")
async def api_upload_bank_statement(
    account_id: str,
    files: list[UploadFile] = File(...),
    user: dict = Depends(require_panel_user),
):
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Upload at least one PDF bank statement.")
    result = None
    for upload in files:
        content = await upload.read()
        result = await upload_bank_statement_pdf(
            user,
            account_id,
            upload.filename or "bank-statement.pdf",
            upload.content_type or "application/pdf",
            content,
        )
    return {"status": "ok", "bankStatements": result or bank_statement_payload(user)}


@app.get("/api/customers/{customer_id}/xero-transactions")
async def api_customer_xero_transactions(customer_id: str, user: dict = Depends(require_panel_user)):
    return await customer_xero_transactions(customer_id, user)


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
    add_note(invoice_id, user, body)
    xero_note = await sync_invoice_note_to_xero(invoice_id, user, body)
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
