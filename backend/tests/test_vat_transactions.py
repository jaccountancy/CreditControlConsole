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


@unittest.skipIf(services is None, f"VAT transaction tests skipped: {_IMPORT_ERROR}")
class VatTransactionsTests(unittest.TestCase):
    def test_invoice_lines_for_vat_period_ignores_non_dict_line_items(self):
        rows = services._invoice_lines_for_vat_period(
            [
                {
                    "InvoiceID": "inv-1",
                    "InvoiceNumber": "INV-1",
                    "DateString": "2026-03-15",
                    "Type": "ACCPAY",
                    "LineItems": [None, "invalid", {"Description": "Valid line", "LineAmount": "100", "TaxAmount": "20", "TaxType": "INPUT2"}],
                }
            ],
            date(2026, 3, 1),
            date(2026, 5, 31),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["description"], "Valid line")
        self.assertEqual(rows[0]["netAmount"], 100.0)
        self.assertEqual(rows[0]["taxAmount"], 20.0)

