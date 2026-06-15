from __future__ import annotations

import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
