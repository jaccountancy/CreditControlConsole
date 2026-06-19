from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("PORT", "8000")
os.environ.setdefault("BASE_URL", "https://example.com")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@example.com:5432/credit_control")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("WIDGET_TOKEN", "test-widget-token")
os.environ.setdefault("XERO_CLIENT_ID", "test-xero-client-id")
os.environ.setdefault("XERO_CLIENT_SECRET", "test-xero-client-secret")
os.environ.setdefault("XERO_REDIRECT_URI", "https://example.com/xero/callback")

try:
    from app import services

    _TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    services = None  # type: ignore[assignment]
    _TEST_IMPORT_ERROR = str(exc)


class _Cursor:
    def __init__(self, row: dict | None = None, rows: list[dict] | None = None):
        self._row = row or {}
        self._rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _query, _params):
        return None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, row: dict | None = None, rows: list[dict] | None = None):
        self._row = row or {}
        self._rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _Cursor(row=self._row, rows=self._rows)

    def commit(self):
        return None


@unittest.skipIf(services is None, f"Regression tests skipped: {_TEST_IMPORT_ERROR}")
class ServicesRegressionTests(unittest.TestCase):
    def test_month_start_supports_no_argument(self):
        fixed_now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now):
            self.assertEqual(services._month_start(), date(2026, 6, 1))

    def test_pi_month_bounds_default_month(self):
        fixed_now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now):
            start, end = services._pi_month_bounds(None)
        self.assertEqual(start, date(2026, 6, 1))
        self.assertEqual(end, date(2026, 6, 30))

    def test_call_stats_client_logs_payload_uses_month_start_without_type_error(self):
        fixed_now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
        benchmark_row = {
            "total_calls": 0,
            "active_clients": 0,
            "inbound_calls": 0,
            "outbound_calls": 0,
        }
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "_call_stats_fetch_rows", return_value=[]), \
             patch.object(services, "_call_stats_practice_summary", return_value={"inboundCalls": 0, "outboundCalls": 0}), \
             patch.object(services, "get_connection", return_value=_Connection(row=benchmark_row)):
            payload = services.call_stats_client_logs_payload({"id": "user-1"}, "client-1", {})
        self.assertEqual(payload["clientId"], "client-1")
        self.assertEqual(payload["summary"]["callsThisMonth"], 0)
        self.assertEqual(payload["summary"]["callsLastMonth"], 0)

    def test_me_report_director_loan_account_data_uses_account_code_key(self):
        accounts = [
            {
                "accountCode": "DLA001",
                "accountName": "Director Loan Account",
                "accountType": "LIABILITY",
                "debitYTD": 0,
                "creditYTD": 0,
            }
        ]
        result = services._me_report_director_loan_account_data(accounts, {})
        self.assertEqual(result["accountCount"], 1)
        self.assertEqual(result["accounts"][0]["code"], "DLA001")

    def test_me_report_director_loan_account_code_for_client(self):
        mapping_rows = [
            {
                "account_code": "4550",
                "account_name": "Director Loan",
                "category": "Director Loan",
                "suggested_treatment": "",
                "confidence": 0.95,
            }
        ]
        with patch.object(services, "get_connection", return_value=_Connection(rows=mapping_rows)):
            code = services._me_report_director_loan_account_code_for_client("client-1")
        self.assertEqual(code, "4550")


if __name__ == "__main__":
    unittest.main()
