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

    _IGNITION_TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    services = None  # type: ignore[assignment]
    _IGNITION_TEST_IMPORT_ERROR = str(exc)


@unittest.skipIf(services is None, f"Ignition renewals tests skipped: {_IGNITION_TEST_IMPORT_ERROR}")
class IgnitionRenewalsTests(unittest.TestCase):
    def test_proposal_end_date_falls_back_to_contract_months_when_no_explicit_end_date(self):
        proposal = {
            "accepted_at": "2026-06-01T09:00:00Z",
            "minimum_contract_length": 12,
        }
        self.assertEqual(services._ignition_proposal_end_date(proposal), date(2027, 6, 1))

    def test_proposal_end_date_month_rollover_is_clamped(self):
        proposal = {
            "accepted_at": "2026-01-31T12:00:00Z",
            "minimum_contract_length": 1,
        }
        self.assertEqual(services._ignition_proposal_end_date(proposal), date(2026, 2, 28))

    def test_upcoming_renewal_proposals_includes_fallback_end_date(self):
        records = [
            {
                "external_id": "proposal-1",
                "payload": {
                    "name": "Monthly package",
                    "client_name": "Acme Ltd",
                    "state": "accepted",
                    "accepted_at": "2026-05-01T10:00:00Z",
                    "minimum_contract_length": 1,
                    "services": [],
                },
            }
        ]
        items = services._ignition_upcoming_renewal_proposals(
            records=records,
            window_start=date(2026, 5, 25),
            window_end=date(2026, 6, 30),
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["proposal_external_id"], "proposal-1")
        self.assertEqual(items[0]["renewal_date"], date(2026, 6, 1))


if __name__ == "__main__":
    unittest.main()
