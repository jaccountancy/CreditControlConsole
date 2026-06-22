from __future__ import annotations

import os
import sys
import asyncio
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

    def test_payroll_overview_uses_latest_submitted_payrun_details_for_p32_estimate(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {"PayRunID": "draft-1", "PayRunStatus": "DRAFT", "PaymentDate": "2026-06-30"},
                        {"PayRunID": "submitted-1", "PayRunStatus": "POSTED", "PaymentDate": "2026-06-22"},
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-1",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "5000.00",
                        }
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-1",
                            "Totals": {"PayeAmount": "1000.00", "NicAmount": "250.00"},
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1250.0)
        self.assertEqual(payload["summary"]["outstandingTaxBalance"], 5000.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-1"), {})
        self.assertEqual(submitted.get("estimatedP32Tax"), 1250.0)

    def test_payroll_overview_sums_pension_from_submitted_payrun_payslips(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {"PayRunID": "submitted-2", "PayRunStatus": "POSTED", "PaymentDate": "2026-06-22"},
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-2"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-2",
                            "Payslips": [
                                {"EmployerPensionContribution": "120.50"},
                                {"EmployerPensionContribution": "79.50"},
                            ],
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["pensionPayableBalance"], 200.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-2"), {})
        self.assertEqual(submitted.get("estimatedPensionPayable"), 200.0)

    def test_payroll_overview_supports_lower_camel_payrun_keys(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "payRunId": "submitted-lc-1",
                            "payRunStatus": "POSTED",
                            "payRunPeriodEndDate": "2026-06-22",
                            "wages": "3999.55",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-lc-1"):
                return {
                    "PayRuns": [
                        {
                            "payRunId": "submitted-lc-1",
                            "Totals": {"PayeAmount": "1100.00", "NicAmount": "300.00"},
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1400.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-lc-1"), {})
        self.assertEqual(submitted.get("estimatedP32Tax"), 1400.0)
        self.assertEqual(submitted.get("status"), "POSTED")
        self.assertEqual(submitted.get("wages"), 3999.55)

    def test_payroll_overview_supports_lower_camel_plural_payruns_key(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "payRuns": [
                        {
                            "payRunId": "submitted-lc-plural-1",
                            "payRunStatus": "POSTED",
                            "paymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-lc-plural-1"):
                return {
                    "payRuns": [
                        {
                            "payRunId": "submitted-lc-plural-1",
                            "totals": {"payeAmount": "900.00", "nicAmount": "200.00"},
                            "payslips": [
                                {"employerPensionContribution": "60.00"},
                                {"employerPensionContribution": "40.00"},
                            ],
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1100.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 100.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-lc-plural-1"), {})
        self.assertEqual(submitted.get("estimatedP32Tax"), 1100.0)
        self.assertEqual(submitted.get("estimatedPensionPayable"), 100.0)

    def test_payroll_overview_supports_lower_camel_account_balance_keys(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {"PayRuns": []}
            if url == services.ACCOUNTS_URL:
                return {
                    "accounts": [
                        {
                            "accountId": "tax-1",
                            "code": "825",
                            "name": "PAYE Payable",
                            "type": "CURRLIAB",
                            "class": "LIABILITY",
                            "currentBalance": "1234.56",
                        },
                        {
                            "accountId": "pen-1",
                            "code": "826",
                            "name": "Pension Payable",
                            "type": "CURRLIAB",
                            "class": "LIABILITY",
                            "currentBalance": "210.10",
                        },
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["outstandingTaxBalance"], 1234.56)
        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1234.56)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 210.1)


if __name__ == "__main__":
    unittest.main()
