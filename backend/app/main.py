import json
import logging
import threading
from html import escape
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
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
    add_note,
    add_promise,
    customer_detail,
    dashboard_payload,
    disconnect_xero,
    get_xero_connection_for_user,
    invoice_detail,
    list_customers,
    list_developer_logs,
    panel_payload,
    get_sync_run,
    record_sync_start_failure,
    request_sync_run,
    run_sync,
    run_sync_job,
    serialize_sync_run,
    update_control_status,
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


def template_context(request: Request, **extra):
    return {"request": request, "user": current_user_from_request(request), **extra}


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return "application/json" in accept or "application/json" in content_type


def xero_login_error_response(message: str, status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR) -> HTMLResponse:
    safe_message = escape(message)
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Xero connection failed</title>
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
                <h1>Xero connection failed</h1>
                <p>{safe_message}</p>
                <a href="/login">Back to login</a>
            </main>
        </body>
        </html>
        """,
        status_code=status_code,
    )


def xero_login_success_response() -> HTMLResponse:
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Xero connected</title>
            <style>
                body {
                    margin: 0;
                    min-height: 100vh;
                    display: grid;
                    place-items: center;
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                    background: #f5f8ff;
                    color: #1e2f4d;
                }
                main {
                    width: min(560px, calc(100vw - 40px));
                    padding: 32px;
                    border-radius: 20px;
                    background: #fff;
                    box-shadow: 0 18px 60px rgba(41, 79, 148, 0.14);
                }
                h1 { margin: 0 0 12px; font-size: 28px; }
                p { margin: 0 0 22px; color: #65738e; line-height: 1.55; }
                a {
                    display: inline-flex;
                    padding: 12px 18px;
                    border-radius: 999px;
                    color: #fff;
                    background: #1d67f2;
                    text-decoration: none;
                    font-weight: 700;
                }
            </style>
        </head>
        <body>
            <main>
                <h1>Xero connected</h1>
                <p>Your Xero organisation is connected. Return to the Credit Control Console and run sync.</p>
                <a href="/">Open dashboard</a>
            </main>
        </body>
        </html>
        """,
        status_code=status.HTTP_200_OK,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": get_settings().app_env}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", template_context(request))


@app.get("/auth/xero/start")
def auth_xero_start(redirect_to: str = "/auth/xero/connected"):
    state_token = start_oauth_state(redirect_to=redirect_to)
    return RedirectResponse(xero_authorize_url(state_token), status_code=status.HTTP_302_FOUND)


@app.get("/auth/xero/connected", response_class=HTMLResponse)
def auth_xero_connected():
    return xero_login_success_response()


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
        response = RedirectResponse(state_row["redirect_to"] or "/", status_code=status.HTTP_302_FOUND)
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
def invoice_add_note(invoice_id: str, user: dict = Depends(require_user), body: str = Form(...)):
    add_note(invoice_id, user, body)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/invoices/{invoice_id}/promises")
def invoice_add_promise(
    invoice_id: str,
    user: dict = Depends(require_user),
    promised_amount: str = Form(...),
    promised_date: str = Form(...),
    note: str = Form(""),
):
    add_promise(invoice_id, user, promised_amount, promised_date, note)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/invoices/{invoice_id}/status")
def invoice_set_status(
    invoice_id: str,
    user: dict = Depends(require_user),
    status_value: str = Form(...),
    note: str = Form(""),
):
    update_control_status(invoice_id, user, status_value, note)
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
    return panel_payload(user)


@app.post("/api/panel/sync")
async def api_panel_sync(user: dict = Depends(require_panel_user)):
    try:
        sync_run, started = request_sync_run(user)
        if started:
            threading.Thread(target=run_sync_job, args=(dict(user), str(sync_run["id"])), daemon=True).start()
        return {
            "status": "queued" if started else "running",
            "started": started,
            "syncRun": serialize_sync_run(sync_run),
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
    }
    if sync_run["status"] == "completed":
        payload["panel"] = panel_payload(user)
    return payload


@app.get("/api/developer/logs")
def api_developer_logs(limit: int = Query(120, ge=1, le=300), user: dict = Depends(require_panel_user)):
    return {"logs": list_developer_logs(user, limit)}


@app.post("/api/xero/disconnect")
def api_xero_disconnect(user: dict = Depends(require_panel_user)):
    return {"status": "ok", **disconnect_xero(user)}


@app.post("/api/invoices/{invoice_id}/notes")
async def api_invoice_add_note(invoice_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    body = str(payload.get("body", "")).strip()
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Note body is required.")
    add_note(invoice_id, user, body)
    return {
        "status": "ok",
        "invoice": invoice_detail(invoice_id),
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
    return {
        "status": "ok",
        "invoice": invoice_detail(invoice_id),
    }


@app.post("/api/invoices/{invoice_id}/status")
async def api_invoice_set_status(invoice_id: str, request: Request, user: dict = Depends(require_panel_user)):
    payload = await request.json()
    status_value = str(payload.get("statusValue", "")).strip()
    note = str(payload.get("note", "")).strip()
    if not status_value:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status is required.")
    update_control_status(invoice_id, user, status_value, note)
    return {
        "status": "ok",
        "invoice": invoice_detail(invoice_id),
    }


@app.exception_handler(XeroConfigurationError)
async def xero_configuration_error_handler(_, exc: XeroConfigurationError) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": str(exc)})
