from __future__ import annotations

import os
import unittest
from datetime import date

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

    _IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    services = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


@unittest.skipIf(services is None, f"JUKSIB tests skipped: {_IMPORT_ERROR}")
class JukSibHelperTests(unittest.TestCase):
    def test_normalise_name_removes_ltd_and_punctuation(self):
        self.assertEqual(
            services._juksib_normalise_name("ABC Holdings, Ltd. (UK)"),
            "abc holdings",
        )

    def test_similarity_prefers_close_company_names(self):
        close_score = services._juksib_similarity("Audit & Accountancy Fees", "Audit and Accountancy Fees")
        far_score = services._juksib_similarity("Audit & Accountancy Fees", "Motor Expenses")
        self.assertGreater(close_score, far_score)
        self.assertGreaterEqual(close_score, services.Decimal("0.80"))

    def test_purchase_account_type_detection(self):
        self.assertTrue(services._juksib_is_purchase_account({"Type": "EXPENSE"}))
        self.assertTrue(services._juksib_is_purchase_account({"Type": "DIRECTCOSTS"}))
        self.assertFalse(services._juksib_is_purchase_account({"Type": "REVENUE"}))

    def test_account_name_score_prefers_audit_and_accountancy_fees(self):
        best = services._juksib_account_name_score("Audit and Accountancy Fees")
        nearby = services._juksib_account_name_score("Professional Fees")
        unrelated = services._juksib_account_name_score("Motor Expenses")
        self.assertGreater(best, nearby)
        self.assertGreater(nearby, unrelated)

    def test_source_invoice_rows_filters_status_and_date_range(self):
        raw_invoices = [
            {
                "InvoiceID": "inv-ok",
                "InvoiceNumber": "INV-001",
                "Status": "AUTHORISED",
                "Date": "2026-05-10",
                "DueDate": "2026-05-20",
                "SubTotal": "100.00",
                "TotalTax": "20.00",
                "Total": "120.00",
                "AmountDue": "120.00",
                "CurrencyCode": "GBP",
                "Contact": {"ContactID": "c-1", "Name": "Alpha Ltd"},
                "LineItems": [{"Description": "Monthly services"}],
            },
            {
                "InvoiceID": "inv-void",
                "InvoiceNumber": "INV-VOID",
                "Status": "VOIDED",
                "Date": "2026-05-11",
                "SubTotal": "100.00",
                "TotalTax": "20.00",
                "Total": "120.00",
                "AmountDue": "120.00",
                "CurrencyCode": "GBP",
                "Contact": {"ContactID": "c-2", "Name": "Beta Ltd"},
            },
            {
                "InvoiceID": "inv-old",
                "InvoiceNumber": "INV-OLD",
                "Status": "AUTHORISED",
                "Date": "2026-03-30",
                "SubTotal": "100.00",
                "TotalTax": "20.00",
                "Total": "120.00",
                "AmountDue": "120.00",
                "CurrencyCode": "GBP",
                "Contact": {"ContactID": "c-3", "Name": "Gamma Ltd"},
            },
        ]
        rows = services._juksib_source_invoice_rows(
            raw_invoices,
            date_from=date(2026, 4, 1),
            date_to=date(2026, 6, 1),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["juk_xero_invoice_id"], "inv-ok")
        self.assertEqual(rows[0]["juk_invoice_number"], "INV-001")

    def test_extract_created_bill_id_accepts_valid_invoice_id(self):
        bill_id, error = services._juksib_extract_created_bill_id(
            {
                "Invoices": [
                    {
                        "InvoiceID": "63a2aefa-df47-459c-a582-8f3914dda148",
                        "HasErrors": False,
                    }
                ]
            }
        )
        self.assertEqual(bill_id, "63a2aefa-df47-459c-a582-8f3914dda148")
        self.assertEqual(error, "")

    def test_extract_created_bill_id_rejects_xero_validation_error_payload(self):
        bill_id, error = services._juksib_extract_created_bill_id(
            {
                "HasErrors": True,
                "Invoices": [
                    {
                        "InvoiceID": "should-not-be-trusted",
                        "HasErrors": True,
                        "ValidationErrors": [{"Message": "Account code is invalid."}],
                    }
                ],
            }
        )
        self.assertEqual(bill_id, "")
        self.assertIn("Xero rejected bill creation", error)


if __name__ == "__main__":
    unittest.main()
