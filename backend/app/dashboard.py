from datetime import date, datetime, timezone

from .database import get_connection
from .schemas import DashboardPayload, TopRiskAccount


def load_dashboard() -> DashboardPayload:
    today = date.today()

    summary_query = """
    SELECT
        MAX(synced_at) AS as_of,
        COUNT(*) AS invoice_count,
        COALESCE(SUM(amount_due), 0) AS total_receivables,
        COALESCE(SUM(CASE WHEN due_date < %(today)s THEN amount_due ELSE 0 END), 0) AS total_overdue,
        COALESCE(SUM(CASE WHEN due_date >= %(today)s - INTERVAL '30 days' AND due_date < %(today)s THEN amount_due ELSE 0 END), 0) AS overdue_1_30,
        COALESCE(SUM(CASE WHEN due_date >= %(today)s - INTERVAL '60 days' AND due_date < %(today)s - INTERVAL '30 days' THEN amount_due ELSE 0 END), 0) AS overdue_31_60,
        COALESCE(SUM(CASE WHEN due_date >= %(today)s - INTERVAL '90 days' AND due_date < %(today)s - INTERVAL '60 days' THEN amount_due ELSE 0 END), 0) AS overdue_61_90,
        COALESCE(SUM(CASE WHEN due_date < %(today)s - INTERVAL '90 days' THEN amount_due ELSE 0 END), 0) AS overdue_90_plus,
        COUNT(DISTINCT CASE WHEN due_date < %(today)s AND amount_due > 0 THEN contact_name ELSE NULL END) AS accounts_needing_action
    FROM xero_invoices
    WHERE amount_due > 0
    """

    risk_query = """
    SELECT contact_name, amount_due, due_date
    FROM xero_invoices
    WHERE amount_due > 0
    ORDER BY amount_due DESC, due_date ASC NULLS LAST
    LIMIT 5
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(summary_query, {"today": today})
            summary = cursor.fetchone()
            cursor.execute(risk_query)
            risk_rows = cursor.fetchall()

    if summary is None:
        return DashboardPayload(
            as_of=datetime.now(timezone.utc),
            invoice_count=0,
            total_receivables=0,
            total_overdue=0,
            overdue_1_30=0,
            overdue_31_60=0,
            overdue_61_90=0,
            overdue_90_plus=0,
            accounts_needing_action=0,
            top_risk_accounts=[],
        )

    return DashboardPayload(
        as_of=summary["as_of"],
        invoice_count=summary["invoice_count"],
        total_receivables=float(summary["total_receivables"]),
        total_overdue=float(summary["total_overdue"]),
        overdue_1_30=float(summary["overdue_1_30"]),
        overdue_31_60=float(summary["overdue_31_60"]),
        overdue_61_90=float(summary["overdue_61_90"]),
        overdue_90_plus=float(summary["overdue_90_plus"]),
        accounts_needing_action=summary["accounts_needing_action"],
        top_risk_accounts=[
            TopRiskAccount(
                name=row["contact_name"],
                amount_due=float(row["amount_due"]),
                due_date=row["due_date"],
            )
            for row in risk_rows
        ],
    )
