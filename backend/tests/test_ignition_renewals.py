from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

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

    def test_rule_uplift_recommendation_uses_median_history(self):
        context = {
            "recent_changes": [
                {"percent": 0.02},
                {"percent": 0.03},
                {"percent": 0.04},
            ]
        }
        percent, reason = services._ignition_rule_uplift_recommendation(context)
        self.assertEqual(percent, services.Decimal("0.0300"))
        self.assertIn("historical", reason.lower())

    def test_recommendation_context_builds_hash_and_history(self):
        item = {
            "proposal_external_id": "proposal-new",
            "client_name": "Acme Ltd",
            "plan_name": "Standard",
            "service_name": "Standard Plan Subscription",
            "current_monthly_fee": services.Decimal("100.00"),
        }
        proposal_records = [
            {
                "external_id": "proposal-old-1",
                "payload": {
                    "name": "Standard Plan",
                    "client_name": "Acme Ltd",
                    "state": "accepted",
                    "accepted_at": "2024-06-01T09:00:00Z",
                    "services": [{"name": "Standard Plan Subscription", "pricing": {"minimum_period_value": "90"}, "billing": {"period": "month"}}],
                },
            },
            {
                "external_id": "proposal-old-2",
                "payload": {
                    "name": "Standard Plan",
                    "client_name": "Acme Ltd",
                    "state": "accepted",
                    "accepted_at": "2025-06-01T09:00:00Z",
                    "services": [{"name": "Standard Plan Subscription", "pricing": {"minimum_period_value": "100"}, "billing": {"period": "month"}}],
                },
            },
        ]
        context = services._ignition_recommendation_context(item, proposal_records, [])
        self.assertTrue(context["history_hash"])
        self.assertGreaterEqual(len(context["recent_changes"]), 1)

    def test_risk_assessment_tenure_uses_at_least_one_year_for_prior_calendar_year(self):
        class FakeDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 1, 10)

        with patch.object(services, "date", FakeDate):
            _, summary = services._risk_assessment_tenure_summary("2025-12-30")
        self.assertIn("1 year", summary)

    def test_risk_assessment_tenure_keeps_months_for_same_calendar_year(self):
        class FakeDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 6, 3)

        with patch.object(services, "date", FakeDate):
            _, summary = services._risk_assessment_tenure_summary("2026-01-15")
        self.assertIn("month", summary.lower())


if __name__ == "__main__":
    unittest.main()
