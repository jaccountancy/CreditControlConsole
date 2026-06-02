from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import patch
from xml.etree import ElementTree as ET

try:
    import httpx
    from fastapi import HTTPException
    from app import companies_house as ch
    _CH_TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    httpx = None  # type: ignore[assignment]
    HTTPException = Exception  # type: ignore[assignment]
    ch = None  # type: ignore[assignment]
    _CH_TEST_IMPORT_ERROR = str(exc)


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "<root/>"
        self.is_error = status_code >= 400

    def json(self):
        return self._payload


class _DummyClient:
    def __init__(self, response: _DummyResponse):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str):
        return self._response


@unittest.skipIf(ch is None, f"Companies House tests skipped: {_CH_TEST_IMPORT_ERROR}")
class CompaniesHouseTests(unittest.TestCase):
    def test_reconcile_status_code(self):
        self.assertEqual(ch._reconcile_submission_status_code("ACCEPT"), "accepted")
        self.assertEqual(ch._reconcile_submission_status_code("REJECT"), "rejected")
        self.assertEqual(ch._reconcile_submission_status_code("PENDING"), "submitted")

    def test_submission_response_extracts_payment_evidence(self):
        xml = """
        <GovTalkMessage xmlns="http://www.govtalk.gov.uk/CM/envelope">
          <Body>
            <Status>
              <SubmissionNumber>ABC123</SubmissionNumber>
              <StatusCode>ACCEPT</StatusCode>
              <PaymentReference>PAY-123</PaymentReference>
            </Status>
          </Body>
        </GovTalkMessage>
        """
        root = ET.fromstring(xml.encode("utf-8"))
        parsed = ch._parse_ch_submission_response(
            response_text=xml,
            response_root=root,
            requested_submission_number="ABC123",
        )
        self.assertEqual(parsed["status"], "accepted")
        self.assertEqual(parsed["paymentEvidence"].get("paymentReference"), "PAY-123")

    def test_validate_cs01_payload_catches_invalid_number(self):
        row = {
            "company_number": "123",
            "next_due_date": date.today(),
            "share_capital": {},
        }
        errors = ch._validate_cs01_payload(row, date.today())
        self.assertTrue(any("Company number" in err for err in errors))

    def test_validate_cs01_payload_requires_made_up_to_date(self):
        row = {
            "company_number": "12345678",
            "next_due_date": date.today(),
            "next_made_up_to_date": None,
            "share_capital": {},
        }
        errors = ch._validate_cs01_payload(row, date.today())
        self.assertTrue(any("Made up to date is required" in err for err in errors))

    def test_validate_cs01_payload_rejects_psc_and_exemption_mix(self):
        row = {
            "company_number": "12345678",
            "next_due_date": date.today(),
            "pscs": [{"name": "Example PSC", "ceasedOn": ""}],
            "share_capital": {
                "cs01Flags": {
                    "tradingOnMarket": True,
                    "pscExemptAsTradingOnRegulatedMarket": True,
                }
            },
        }
        payload = ch._build_cs01_payload(row)
        errors = ch._validate_cs01_payload(row, date.today(), cs_payload=payload)
        self.assertTrue(any("cannot both be supplied" in err for err in errors))

    def test_build_cs01_payload_autofills_shares_admitted_exemption(self):
        row = {
            "company_number": "12345678",
            "next_due_date": date.today(),
            "pscs": [],
            "share_capital": {
                "cs01Flags": {"dtr5Applies": True},
            },
        }
        payload = ch._build_cs01_payload(row)
        self.assertTrue(payload.get("tradingOnMarket"))
        self.assertTrue(payload.get("pscExemptAsSharesAdmittedOnMarket"))

    def test_payment_confirmation_fallback_evidence_contains_audit_marker(self):
        marker = ch._payment_confirmation_fallback_evidence(
            source="status_poll_acceptance",
            status_code="ACCEPT",
            now=datetime(2026, 6, 2),
        )
        self.assertTrue(marker.get("paymentConfirmationFallback"))
        self.assertEqual(marker.get("paymentConfirmationSource"), "status_poll_acceptance")

    def test_submission_idempotency_key_is_stable(self):
        key1 = ch._submission_idempotency_key("cid-1", date(2026, 6, 1))
        key2 = ch._submission_idempotency_key("cid-1", date(2026, 6, 1))
        key3 = ch._submission_idempotency_key("cid-1", date(2026, 6, 2))
        self.assertEqual(key1, key2)
        self.assertNotEqual(key1, key3)

    def test_connection_test_invalid_credentials(self):
        with patch.object(ch, "_ensure_settings_row", return_value={"environment": "sandbox"}), \
             patch.object(ch, "decrypt_api_key", return_value="bad-key"), \
             patch.object(
                 ch,
                 "_companies_house_http_client",
                 return_value=_DummyClient(_DummyResponse(401, {})),
             ):
            with self.assertRaises(HTTPException) as ctx:
                ch.test_companies_house_connection()
        self.assertEqual(ctx.exception.status_code, 400)

    def test_connection_test_treats_404_probe_as_success(self):
        with patch.object(ch, "_ensure_settings_row", return_value={"environment": "sandbox"}), \
             patch.object(ch, "decrypt_api_key", return_value="good-key"), \
             patch.object(
                 ch,
                 "_companies_house_http_client",
                 return_value=_DummyClient(_DummyResponse(404, {})),
             ):
            payload = ch.test_companies_house_connection()
        self.assertTrue(payload.get("connected"))
        self.assertEqual(payload.get("statusCode"), 404)
        self.assertTrue(str(payload.get("endpoint") or "").endswith("/company/00000000"))

    def test_post_gateway_retries_then_raises(self):
        calls = {"count": 0}

        def _always_fail(*args, **kwargs):
            calls["count"] += 1
            raise httpx.ConnectError("boom")

        with patch.object(ch.httpx, "post", side_effect=_always_fail):
            with self.assertRaises(HTTPException) as ctx:
                ch._post_ch_gateway(b"<xml/>")
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(calls["count"], ch.CH_GATEWAY_MAX_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
