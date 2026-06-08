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


@unittest.skipIf(services is None, f"Code Breaker tests skipped: {_IMPORT_ERROR}")
class CodeBreakerSnapshotSelectionTests(unittest.TestCase):
    def test_select_accounts_filing_prefers_exact_made_up_to(self):
        filing_history = [
            {"type": "AA", "made_up_to": "2024-03-31", "netAssets": "100"},
            {"type": "AA", "made_up_to": "2025-03-31", "netAssets": "200"},
        ]
        row, made_up_to, match = services._code_breaker_select_accounts_filing_for_date(
            filing_history,
            date(2025, 3, 31),
        )
        self.assertEqual(match, "exact_period_match")
        self.assertEqual(made_up_to, date(2025, 3, 31))
        self.assertEqual(services._code_breaker_net_assets_value(row), services.Decimal("200.00"))

    def test_select_accounts_filing_uses_latest_before_target_when_no_exact_match(self):
        filing_history = [
            {"type": "AA", "made_up_to": "2023-03-31", "netAssets": "50"},
            {"type": "AA", "made_up_to": "2024-03-31", "netAssets": "100"},
            {"type": "AA", "made_up_to": "2025-03-31", "netAssets": "200"},
        ]
        row, made_up_to, match = services._code_breaker_select_accounts_filing_for_date(
            filing_history,
            date(2024, 8, 1),
        )
        self.assertEqual(match, "latest_before_target")
        self.assertEqual(made_up_to, date(2024, 3, 31))
        self.assertEqual(services._code_breaker_net_assets_value(row), services.Decimal("100.00"))

    def test_select_accounts_filing_uses_earliest_after_target_when_only_future_available(self):
        filing_history = [
            {"type": "AA", "made_up_to": "2024-12-31", "netAssets": "120"},
            {"type": "AA", "made_up_to": "2025-12-31", "netAssets": "180"},
        ]
        row, made_up_to, match = services._code_breaker_select_accounts_filing_for_date(
            filing_history,
            date(2024, 1, 1),
        )
        self.assertEqual(match, "earliest_after_target")
        self.assertEqual(made_up_to, date(2024, 12, 31))
        self.assertEqual(services._code_breaker_net_assets_value(row), services.Decimal("120.00"))

    def test_net_assets_value_ignores_unrelated_numeric_fields(self):
        filing_row = {
            "type": "AA",
            "made_up_to": "2025-05-31",
            "descriptionValues": {
                "made_up_date": "2025-05-31",
                "company_number": "14846268",
            },
            "meta": {"retryCount": 0},
        }
        self.assertIsNone(services._code_breaker_net_assets_value(filing_row))

    def test_net_assets_value_reads_assets_less_liabilities_labels(self):
        filing_row = {
            "type": "AA",
            "made_up_to": "2025-05-31",
            "sections": [
                {"label": "assets less liabilities", "value": "4567.89"},
                {"label": "other", "value": "100"},
            ],
        }
        self.assertEqual(services._code_breaker_net_assets_value(filing_row), services.Decimal("4567.89"))

    def test_extract_net_assets_from_ixhtml_handles_parenthesised_values(self):
        xhtml = """
        <table>
          <tr>
            <td>Net assets (liabilities)</td>
            <td>(<ix:nonFraction contextRef="FY_END_20250531">369</ix:nonFraction>)</td>
            <td><ix:nonFraction contextRef="FY_END_20240531">15</ix:nonFraction></td>
          </tr>
        </table>
        """
        value, matched = services._code_breaker_net_assets_from_ixhtml(xhtml, date(2025, 5, 31))
        self.assertEqual(value, services.Decimal("-369.00"))
        self.assertEqual(matched, date(2025, 5, 31))

    def test_public_filing_rows_extracts_accounts_xhtml_link(self):
        html = """
        <table>
          <tr>
            <td>16 Jan 2026</td>
            <td>AA</td>
            <td>Micro company accounts made up to 31 May 2025</td>
            <td><a href="/company/14846268/filing-history/abc/document?format=xhtml&amp;download=1">Download iXBRL</a></td>
          </tr>
        </table>
        """
        rows = services._code_breaker_public_filing_rows(html, "14846268")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["madeUpTo"], date(2025, 5, 31))
        self.assertEqual(rows[0]["filedOn"], date(2026, 1, 16))
        self.assertIn("format=xhtml", rows[0]["xhtmlPath"])
