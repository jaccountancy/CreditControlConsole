from datetime import date, datetime

from pydantic import BaseModel


class TopRiskAccount(BaseModel):
    name: str
    amount_due: float
    due_date: date | None


class DashboardPayload(BaseModel):
    as_of: datetime | None
    invoice_count: int
    total_receivables: float
    total_overdue: float
    overdue_1_30: float
    overdue_31_60: float
    overdue_61_90: float
    overdue_90_plus: float
    accounts_needing_action: int
    top_risk_accounts: list[TopRiskAccount]


class SyncResult(BaseModel):
    fetched: int
    inserted_or_updated: int
    synced_at: datetime
