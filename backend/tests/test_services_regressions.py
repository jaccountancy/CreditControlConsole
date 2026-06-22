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

    def test_payroll_overview_prefers_journal_with_stronger_payroll_liability_lines(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pensions Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-1",
                            "Totals": {"PayeAmount": "3000.00", "NicAmount": "1151.67"},
                        }
                    ]
                }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-low",
                        "JournalDate": "2026-05-31",
                        "Reference": "Manual adjustment",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "4151.67", "Description": "Tax"},
                        ],
                    },
                    {
                        "JournalID": "jrnl-payroll",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "11676.94", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "1198.10", "Description": "Pension"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["journalPayableDiagnostics"].get("engine"), "disabled")

    def test_payroll_overview_uses_most_recent_payroll_journal(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-most-recent-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pensions Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-most-recent-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-most-recent-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-payroll-older",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "20000.00", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "3000.00", "Description": "Pension"},
                        ],
                    },
                    {
                        "JournalID": "jrnl-payroll-latest",
                        "JournalDate": "2026-06-01",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "1200.00", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "225.00", "Description": "Pension"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["journalPayableDiagnostics"].get("engine"), "disabled")

    def test_payroll_overview_prefers_journal_with_source_id_matching_selected_payrun(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-match-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pensions Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-match-1"):
                return {"PayRuns": [{"PayRunID": "submitted-match-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-other",
                        "SourceID": "another-payrun-id",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "2500.00", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "450.00", "Description": "Pension"},
                        ],
                    },
                    {
                        "JournalID": "jrnl-target",
                        "SourceID": "submitted-match-1",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "5000.00", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "800.00", "Description": "Pension"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["journalPayableDiagnostics"].get("engine"), "disabled")

    def test_payroll_overview_uses_trial_balance_delta_when_journal_lines_do_not_match(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-miss-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-miss-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-miss-1",
                            "Totals": {"PayeAmount": "3000.00", "NicAmount": "1151.67"},
                        }
                    ]
                }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                report_date = str((params or {}).get("date") or "")
                if report_date == "2026-05-31":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "PAYE Payable"}, {"Value": "10000.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "Pensions Payable"}, {"Value": "2200.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                if report_date == "2026-04-30":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "PAYE Payable"}, {"Value": "5000.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "Pensions Payable"}, {"Value": "1200.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-unmatched",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountCode": "477", "Credit": "52641.89", "Description": "Salaries"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 5000.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 1000.0)
        self.assertEqual(payload["summary"]["figureSources"]["p32Tax"], "nominal_trial_balance_delta")
        self.assertEqual(payload["summary"]["figureSources"]["pensionPayable"], "nominal_trial_balance_delta")

    def test_payroll_overview_uses_openai_inference_when_journal_and_trial_balance_are_not_usable(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-openai-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-openai-1"):
                return {"PayRuns": [{"PayRunID": "submitted-openai-1", "Totals": {"PayeAmount": "0.00", "NicAmount": "0.00"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {
                    "Reports": [
                        {
                            "Rows": [
                                {
                                    "RowType": "Section",
                                    "Rows": [
                                        {"RowType": "Row", "Cells": [{"Value": "Sales"}, {"Value": "100.00"}]},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-unmatched-openai",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountCode": "477", "Credit": "52641.89", "Description": "Salaries"},
                        ],
                    },
                ],
                "",
            )

        async def _fake_openai(*_args, **_kwargs):
            return (
                services.Decimal("6123.45"),
                services.Decimal("1188.22"),
                {"engine": "openai", "confidence": 0.92, "used": False, "notes": "Matched payroll context"},
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals), \
             patch.object(services, "_payroll_overview_openai_liability_inference", side_effect=_fake_openai):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 0.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["figureSources"]["p32Tax"], "none")
        self.assertEqual(payload["summary"]["figureSources"]["pensionPayable"], "none")

    def test_payroll_overview_uses_signed_journal_lines_when_account_metadata_missing(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-signed-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-signed-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-signed-1",
                            "Totals": {"PayeAmount": "3000.00", "NicAmount": "1151.67"},
                        }
                    ]
                }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-payroll-signed",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountCode": "825", "Description": "Tax", "NetAmount": "-11676.94"},
                            {"AccountCode": "858", "Description": "Pension", "NetAmount": "-1198.10"},
                            {"AccountCode": "477", "Description": "Earnings", "NetAmount": "52641.89"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["journalPayableDiagnostics"].get("engine"), "disabled")

    def test_payroll_overview_sums_credit_lines_for_tax_and_pension_liability_in_payroll_journal(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-net-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pensions Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-net-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-net-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-payroll-net",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "11676.94", "Description": "Tax"},
                            {"AccountID": "acc-825", "Credit": "9150.77", "Description": "National Insurance Contribution"},
                            {"AccountID": "acc-825", "Credit": "1165.00", "Description": "Deductions"},
                            {"AccountID": "acc-825", "Debit": "3773.26", "Description": "Employment Allowance"},
                            {"AccountID": "acc-825", "Debit": "762.32", "Description": "Statutory Recovery - Maternity Pay"},
                            {"AccountID": "acc-858", "Credit": "1086.12", "Description": "Benefits"},
                            {"AccountID": "acc-858", "Credit": "1198.10", "Description": "Deductions"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_matches_liability_lines_when_account_code_formats_differ(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-codefmt-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825 - PAYE Payable",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858 - Pension Payable",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-codefmt-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-codefmt-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-code-format",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountCode": "825", "Description": "Tax", "Credit": "11676.94"},
                            {"AccountCode": "858", "Description": "Pension", "Credit": "1198.10"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_matches_snake_case_journal_line_fields(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-snake-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-snake-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-snake-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-snake-case",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"account_id": "acc-825", "line_description": "Tax", "credit_amount": "11676.94"},
                            {"account_code": "858", "line_description": "Pension", "credit_amount": "1198.10"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_matches_account_ids_when_journal_id_format_differs(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-idfmt-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "A5574A89-1234-4444-9999-AAAAAAAAAAAA",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "B5574A89-1234-4444-9999-BBBBBBBBBBBB",
                            "Code": "858",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-idfmt-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-idfmt-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-id-format",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "{a5574a89-1234-4444-9999-aaaaaaaaaaaa}", "Description": "Tax", "Credit": "11676.94"},
                            {"AccountID": "{b5574a89-1234-4444-9999-bbbbbbbbbbbb}", "Description": "Pension", "Credit": "1198.10"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_does_not_call_payslip_detail_fallback(self):
        payslip_calls = {"count": 0}

        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {"PayRunID": "submitted-slow-1", "PayRunStatus": "POSTED", "PaymentDate": "2026-06-22"},
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-slow-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-slow-1",
                            "Payslips": [{"PayslipID": "ps-1"}, {"PayslipID": "ps-2"}, {"PayslipID": "ps-3"}],
                        }
                    ]
                }
            if url.startswith("https://api.xero.com/payroll.xro/2.0/PaySlips/"):
                payslip_calls["count"] += 1
                raise Exception("Xero permissions need updating. Reconnect Xero to approve reports and journals scopes.")
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payslip_calls["count"], 0)

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
        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 0.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_reads_described_tax_and_pension_line_amounts(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-desc-1",
                            "PayRunStatus": "POSTED",
                            "PaymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-desc-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-desc-1",
                            "PayItems": {
                                "TaxItems": [
                                    {"Description": "PAYE", "Amount": "725.00"},
                                    {"Description": "Employee NIC", "Amount": "275.00"},
                                ],
                                "DeductionItems": [
                                    {"Description": "Workplace Pension Employer", "Amount": "180.25"},
                                ],
                            },
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1000.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 180.25)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-desc-1"), {})
        self.assertEqual(submitted.get("estimatedP32Tax"), 1000.0)
        self.assertEqual(submitted.get("estimatedPensionPayable"), 180.25)

    def test_payroll_overview_sums_superannuation_style_pension_keys(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-super-1",
                            "PayRunStatus": "POSTED",
                            "PaymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-super-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-super-1",
                            "Payslips": [
                                {"EmployerSuperannuation": "95.50"},
                                {"KiwiSaverEmployerContribution": "44.50"},
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

        self.assertEqual(payload["summary"]["pensionPayableBalance"], 140.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-super-1"), {})
        self.assertEqual(submitted.get("estimatedPensionPayable"), 140.0)

    def test_payroll_overview_does_not_fall_back_to_payslip_detail_for_pension(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-ps-1",
                            "PayRunStatus": "POSTED",
                            "PaymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-ps-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-ps-1",
                            "Payslips": [
                                {"PayslipID": "ps-1"},
                                {"PayslipID": "ps-2"},
                            ],
                        }
                    ]
                }
            if url == services.XERO_PAYROLL_PAYSLIP_DETAILS_URL.format(payslip_id="ps-1"):
                return {"Payslips": [{"PayslipID": "ps-1", "EmployerPensionContribution": "90.00"}]}
            if url == services.XERO_PAYROLL_PAYSLIP_DETAILS_URL.format(payslip_id="ps-2"):
                return {"Payslips": [{"PayslipID": "ps-2", "EmployerPensionContribution": "60.00"}]}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-ps-1"), {})
        self.assertEqual(submitted.get("estimatedPensionPayable"), 0.0)

    def test_payroll_overview_does_not_sum_employee_and_employer_from_payslip_detail_calls(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-ps-2",
                            "PayRunStatus": "POSTED",
                            "PaymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-ps-2"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-ps-2",
                            "Payslips": [
                                {"PayslipID": "ps-3"},
                            ],
                        }
                    ]
                }
            if url == services.XERO_PAYROLL_PAYSLIP_DETAILS_URL.format(payslip_id="ps-3"):
                return {
                    "Payslips": [
                        {
                            "PayslipID": "ps-3",
                            "EmployerPensionContribution": "90.00",
                            "EmployeePensionContribution": "55.00",
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-ps-2"), {})
        self.assertEqual(submitted.get("estimatedPensionPayable"), 0.0)

    def test_payroll_overview_prefers_nominal_trial_balance_delta_over_payroll_api(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-nominal-delta-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-nominal-delta-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-nominal-delta-1",
                            "Totals": {
                                "PayeAmount": "7002.10",
                                "PensionPayable": "2096.72",
                            },
                        }
                    ]
                }
            if url == services.XERO_PAYROLL_PAYSLIPS_BY_PAYRUN_URL:
                return {
                    "PaySlips": [
                        {"Tax": "7002.10", "EmployerPensionContribution": "2096.72"},
                    ]
                }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                report_date = str((params or {}).get("date") or "")
                if report_date == "2026-05-31":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "825 PAYE Payable"}, {"Value": "17457.13"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "858 Pension Payable"}, {"Value": "1972.69"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                if report_date == "2026-04-30":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "825 PAYE Payable"}, {"Value": "0.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "858 Pension Payable"}, {"Value": "0.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 17457.13)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 1972.69)
        self.assertEqual(payload["summary"]["figureSources"]["p32Tax"], "nominal_trial_balance_delta")
        self.assertEqual(payload["summary"]["figureSources"]["pensionPayable"], "nominal_trial_balance_delta")
        self.assertEqual(payload["summary"]["trialBalanceDeltaP32Tax"], 17457.13)
        self.assertEqual(payload["summary"]["trialBalanceDeltaPensionPayable"], 1972.69)


if __name__ == "__main__":
    unittest.main()
