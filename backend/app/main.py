from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse

from .config import get_settings
from .dashboard import load_dashboard
from .database import ensure_schema, upsert_invoices
from .schemas import DashboardPayload, SyncResult
from .xero import XeroConfigurationError, fetch_accounts_receivable_invoices

app = FastAPI(title="Credit Control Backend", version="0.1.0")


def authorize_request(authorization: str = Header(default="")) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.api_token}"

    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
        )


@app.on_event("startup")
def startup() -> None:
    ensure_schema()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "environment": get_settings().app_env,
    }


@app.get("/api/dashboard", response_model=DashboardPayload, dependencies=[Depends(authorize_request)])
def dashboard() -> DashboardPayload:
    return load_dashboard()


@app.post("/api/xero/sync", response_model=SyncResult, dependencies=[Depends(authorize_request)])
async def sync_xero() -> SyncResult:
    invoices = await fetch_accounts_receivable_invoices()
    updated = upsert_invoices(invoices)
    return SyncResult(
        fetched=len(invoices),
        inserted_or_updated=updated,
        synced_at=datetime.now(timezone.utc),
    )


@app.exception_handler(XeroConfigurationError)
async def xero_configuration_error_handler(_, exc: XeroConfigurationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": str(exc)},
    )
