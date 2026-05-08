import json
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import (
    approve_device_code,
    clear_session_cookie,
    consume_oauth_state,
    create_device_login,
    current_user_from_request,
    require_api_user,
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
    get_xero_connection_for_user,
    invoice_detail,
    list_customers,
    panel_payload,
    run_sync,
    update_control_status,
)
from .xero import XeroConfigurationError, exchange_code_for_tokens, fetch_connections, fetch_user_profile, store_login

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Credit Control Backend", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def startup() -> None:
    ensure_schema()


def template_context(request: Request, **extra):
    return {"request": request, "user": current_user_from_request(request), **extra}


def wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return "application/json" in accept or "application/json" in content_type


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "environment": get_settings().app_env}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", template_context(request))


@app.get("/auth/xero/start")
def auth_xero_start(redirect_to: str = "/"):
    state_token = start_oauth_state(redirect_to=redirect_to)
    return RedirectResponse(xero_authorize_url(state_token), status_code=status.HTTP_302_FOUND)


@app.get("/auth/xero/callback")
async def auth_xero_callback(request: Request, code: str, state: str):
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
def dashboard_page(request: Request, user: dict = Depends(require_user)):
    xero_connected = False
    try:
        get_xero_connection_for_user(user["id"])
        xero_connected = True
    except HTTPException:
        xero_connected = False

    return templates.TemplateResponse(
        "dashboard.html",
        template_context(
            request,
            dashboard=dashboard_payload(),
            xero_connected=xero_connected,
        ),
    )


@app.post("/sync/run")
async def trigger_sync(user: dict = Depends(require_user)):
    await run_sync(user)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, user: dict = Depends(require_user)):
    return templates.TemplateResponse(
        "customers.html",
        template_context(request, customers=list_customers()),
    )


@app.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_page(customer_id: str, request: Request, user: dict = Depends(require_user)):
    detail = customer_detail(customer_id)
    return templates.TemplateResponse(
        "customer_detail.html",
        template_context(request, **detail),
    )


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_page(invoice_id: str, request: Request, user: dict = Depends(require_user)):
    detail = invoice_detail(invoice_id)
    return templates.TemplateResponse(
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
    return templates.TemplateResponse("device.html", template_context(request, verification_code=code, approved=False))


@app.post("/device/approve")
def device_approve(verification_code: str = Form(...), user: dict = Depends(require_user)):
    approve_device_code(verification_code, user["id"])
    return RedirectResponse("/device/complete?approved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/device/complete", response_class=HTMLResponse)
def device_complete(request: Request, approved: int = 0):
    return templates.TemplateResponse("device.html", template_context(request, verification_code=None, approved=bool(approved)))


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
def api_panel(user: dict = Depends(require_user)):
    return panel_payload(user)


@app.post("/api/panel/sync")
async def api_panel_sync(user: dict = Depends(require_user)):
    sync_run = await run_sync(user)
    return {
        "status": "ok",
        "summary": sync_run["summary"],
        "panel": panel_payload(user),
    }


@app.post("/api/invoices/{invoice_id}/notes")
async def api_invoice_add_note(invoice_id: str, request: Request, user: dict = Depends(require_user)):
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
async def api_invoice_add_promise(invoice_id: str, request: Request, user: dict = Depends(require_user)):
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
async def api_invoice_set_status(invoice_id: str, request: Request, user: dict = Depends(require_user)):
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
