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

    def test_xero_net_assets_prefers_requested_column_over_comparative(self):
        lines = [
            {
                "label": "Net Assets",
                "amounts": [services.Decimal("1500.00"), services.Decimal("0.00")],
                "raw": {},
            }
        ]
        self.assertEqual(services._code_breaker_xero_net_assets(lines), services.Decimal("1500.00"))

    def test_xero_net_assets_uses_matching_as_at_header_column(self):
        lines = [
            {
                "label": "Net Assets",
                "amounts": [services.Decimal("0.00"), services.Decimal("1500.00")],
                "raw": {
                    "Cells": [
                        {"Value": "Net Assets"},
                        {"Value": "0.00"},
                        {"Value": "1500.00"},
                    ]
                },
            }
        ]
        header_dates = [date(2024, 5, 31), date(2025, 5, 31)]
        self.assertEqual(
            services._code_breaker_xero_net_assets(
                lines,
                as_at_date=date(2025, 5, 31),
                header_dates=header_dates,
            ),
            services.Decimal("1500.00"),
        )

    def test_journal_candidates_only_include_post_filing_backdated_journals(self):
        journals = [
            {
                "JournalID": "before-submission",
                "JournalDate": "2025-05-30",
                "CreatedDateUTC": "2026-01-10T09:00:00Z",
                "JournalLines": [{"NetAmount": "10.00", "AccountCode": "400"}],
            },
            {
                "JournalID": "wrong-period",
                "JournalDate": "2025-06-01",
                "CreatedDateUTC": "2026-01-20T09:00:00Z",
                "JournalLines": [{"NetAmount": "2.00", "AccountCode": "400"}],
            },
            {
                "JournalID": "included",
                "JournalDate": "2025-05-31",
                "CreatedDateUTC": "2026-01-20T09:00:00Z",
                "JournalLines": [{"NetAmount": "0.40", "AccountCode": "400"}],
            },
        ]
        submission = services._parse_optional_iso_datetime("2026-01-16T00:00:00Z")
        candidates = services._code_breaker_journal_candidates(
            journals,
            as_at_date=date(2025, 5, 31),
            submitted_at=submission,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["journalId"], "included")
        self.assertEqual(candidates[0]["journalDate"], "2025-05-31")

    def test_journal_candidates_require_created_date_when_submission_known(self):
        journals = [
            {
                "JournalID": "missing-created",
                "JournalDate": "2025-05-31",
                "JournalLines": [{"NetAmount": "0.40", "AccountCode": "400"}],
            }
        ]
        submission = services._parse_optional_iso_datetime("2026-01-16T00:00:00Z")
        candidates = services._code_breaker_journal_candidates(
            journals,
            as_at_date=date(2025, 5, 31),
            submitted_at=submission,
        )
        self.assertEqual(candidates, [])
