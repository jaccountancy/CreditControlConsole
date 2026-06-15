from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
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


@unittest.skipIf(services is None, f"Payroll headcount tests skipped: {_TEST_IMPORT_ERROR}")
class PayrollHeadcountTests(unittest.TestCase):
    def test_payrun_id_extractor_prefers_payrun_id_field(self):
        self.assertEqual(
            services._payroll_headcount_payrun_id({"PayRunID": " run-123 "}),
            "run-123",
        )

    def test_payrun_detail_headcount_counts_non_blank_net_pay_entries(self):
        payload = {
            "PayRuns": [
                {
                    "Payslips": [
                        {"NetPay": "1150.40"},
                        {"NetPay": 0},
                        {"NetPay": None},
                        {"GrossEarnings": "900.00"},
                    ]
                }
            ]
        }
        self.assertEqual(services._payroll_headcount_from_payrun_details(payload), 2)

    def test_payrun_detail_headcount_handles_missing_payslips(self):
        self.assertEqual(services._payroll_headcount_from_payrun_details({"PayRuns": [{}]}), 0)
        self.assertEqual(services._payroll_headcount_from_payrun_details({}), 0)

    def test_month_start_iso_handles_legacy_text_values(self):
        self.assertEqual(services._payroll_headcount_month_start_iso("2026-06-01"), "2026-06-01")
        self.assertEqual(services._payroll_headcount_month_start_iso("/Date(1764547200000+0000)/"), "2025-12-01")
        self.assertEqual(services._payroll_headcount_month_start_iso(""), "")

    def test_workspace_upsert_repairs_missing_schema_and_retries_once(self):
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        row = {
            "id": "workspace-1",
            "tenant_id": "tenant-1",
            "tenant_name": "Acme Ltd",
            "workspace_name": "Acme Ltd Headcount Workspace",
            "wizard_completed": True,
            "ignition_plan_name": "",
            "ignition_client_name": "",
            "ignition_proposal_name": "",
            "ignition_matched_at": None,
            "created_at": now,
            "updated_at": now,
        }

        class _Cursor:
            def __init__(self, payload: dict):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, _query, _params):
                return None

            def fetchone(self):
                return self.payload

        class _Connection:
            def __init__(self, payload: dict):
                self.payload = payload
                self.commit_calls = 0
                self.rollback_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return _Cursor(self.payload)

            def commit(self):
                self.commit_calls += 1

            def rollback(self):
                self.rollback_calls += 1

        class _RaiseOnEnter:
            def __init__(self, error):
                self.error = error

            def __enter__(self):
                raise self.error

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FlakyConnectionFactory:
            def __init__(self, payload: dict):
                self.payload = payload
                self.calls = 0
                self.connection = _Connection(payload)

            def __call__(self):
                self.calls += 1
                if self.calls == 1:
                    return _RaiseOnEnter(services.pg_errors.UndefinedTable("missing payroll table"))
                return self.connection

        flaky_factory = _FlakyConnectionFactory(row)

        with patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_name": "Acme Ltd"}), \
             patch.object(services, "ensure_schema") as ensure_schema_mock, \
             patch.object(services, "get_connection", side_effect=flaky_factory):
            payload = services.upsert_payroll_headcount_workspace({"id": "user-1"}, "tenant-1")

        self.assertEqual(flaky_factory.calls, 2)
        ensure_schema_mock.assert_called_once_with()
        self.assertEqual(payload["id"], "workspace-1")
        self.assertEqual(payload["tenantId"], "tenant-1")
        self.assertEqual(payload["workspaceName"], "Acme Ltd Headcount Workspace")


if __name__ == "__main__":
    unittest.main()
