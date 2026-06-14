from __future__ import annotations

import os
import unittest
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
    from fastapi import HTTPException
    from app import hmrc_648 as hmrc648
    _HMRC_TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    HTTPException = Exception  # type: ignore[assignment]
    hmrc648 = None  # type: ignore[assignment]
    _HMRC_TEST_IMPORT_ERROR = str(exc)


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)
        self.content = b"{}"

    def json(self):
        return self._payload


@unittest.skipIf(hmrc648 is None, f"HMRC 64-8 tests skipped: {_HMRC_TEST_IMPORT_ERROR}")
class Hmrc648Tests(unittest.TestCase):
    def test_service_flags_requires_any_service(self):
        with self.assertRaises(HTTPException):
            hmrc648._service_flags_from_payload({})

    def test_service_flags_accepts_vat_mtd_only(self):
        flags = hmrc648._service_flags_from_payload({"includeVatMtd": True})
        self.assertTrue(flags["includeVatMtd"])
        self.assertFalse(flags["includeSa"])
        self.assertFalse(flags["includeCt"])

    def test_connector_split_rejects_mixed_legacy_and_mtd(self):
        with self.assertRaises(HTTPException):
            hmrc648._connector_for_flags(
                {
                    "includeSa": True,
                    "includePaye": False,
                    "includeCt": False,
                    "includeVatMtd": True,
                    "includeSaMtd": False,
                    "includeCis": False,
                }
            )

    def test_validate_sa_fields_requires_utr_and_postcode(self):
        with self.assertRaises(HTTPException):
            hmrc648._validate_service_fields(
                {"saUtr": "12345"},
                {"includeSa": True, "includePaye": False, "includeCt": False, "includeVatMtd": False, "includeSaMtd": False, "includeCis": False},
                {"postcode": "BAD", "saNino": "", "taxOfficeNumber": "", "taxOfficeReference": "", "accountsOfficeReference": ""},
            )

    def test_submit_xml_gateway_falls_back_when_unconfigured(self):
        with patch.dict(os.environ, {"HMRC_XML_ENDPOINT_URL": ""}, clear=False):
            result = hmrc648._submit_xml_gateway({"id": "abc123"}, {})
        self.assertIn("LOCAL-", result["hmrcSubmissionReference"])
        self.assertTrue(result["submittedAt"])
        self.assertTrue(result["expectedCodeBy"])

    def test_submit_xml_gateway_reads_auth_request_id_from_response(self):
        fake_response = _DummyResponse(200, {})
        fake_response.text = "<GovTalkMessage><AuthRequestID>500000000000000005</AuthRequestID></GovTalkMessage>"
        with patch.dict(
            os.environ,
            {
                "HMRC_XML_ENDPOINT_URL": "https://example-hmrc-gateway.test/submit",
                "HMRC_XML_SENDER_ID": "sender-id",
                "HMRC_XML_AUTH_VALUE": "secret",
                "HMRC_XML_IR_AGENT_REFERENCE": "A123456",
                "HMRC_XML_VENDOR_ID": "1234",
                "HMRC_XML_PRODUCT_NAME": "Credit Control Console",
            },
            clear=False,
        ):
            with patch.object(hmrc648.httpx, "post", return_value=fake_response):
                result = hmrc648._submit_xml_gateway(
                    {
                        "id": "abc123",
                        "client_name": "Test Client Ltd",
                        "client_id": "AB/CD",
                        "include_ct": True,
                        "ct_utr": "5181741759",
                        "postcode": "BD17 7TW",
                    },
                    {},
                )
        self.assertEqual(result["hmrcSubmissionReference"], "500000000000000005")
        self.assertIn("accepted", result["notesAppend"].lower())

    def test_submit_mtd_gateway_falls_back_when_unconfigured(self):
        with patch.dict(os.environ, {"HMRC_MTD_SUBMIT_URL": ""}, clear=False):
            result = hmrc648._submit_mtd_gateway({"id": "user-1"}, {"id": "abc123"}, {})
        self.assertIn("LOCAL-MTD-", result["hmrcSubmissionReference"])

    def test_create_request_query_placeholder_count_matches_params(self):
        class _FakeCursor:
            def __init__(self):
                self.query = ""
                self.params = ()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, params=None):
                self.query = str(query)
                self.params = params or ()

            def fetchone(self):
                return {
                    "id": "req-1",
                    "client_id": "C1",
                    "client_name": "Client 1",
                    "include_sa": True,
                    "include_paye": False,
                    "include_ct": False,
                    "include_vat_mtd": False,
                    "include_sa_mtd": False,
                    "include_cis": False,
                    "status": "draft",
                    "submission_channel": "online",
                }

        class _FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor
                self.committed = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self._cursor

            def commit(self):
                self.committed = True

        fake_cursor = _FakeCursor()
        fake_connection = _FakeConnection(fake_cursor)
        with patch.object(hmrc648, "get_connection", return_value=fake_connection):
            with patch.object(hmrc648, "_record_audit_event", return_value=None):
                hmrc648.create_hmrc_64_8_request(
                    {"id": "user-1"},
                    {"clientName": "Client 1", "includeVatMtd": True},
                )

        self.assertIn("INSERT INTO hmrc_64_8_requests", fake_cursor.query)
        self.assertEqual(fake_cursor.query.count("%s"), len(fake_cursor.params))
        self.assertTrue(fake_connection.committed)

    def test_capture_code_validates_prefix(self):
        with self.assertRaises(HTTPException):
            with patch.object(
                hmrc648,
                "_get_user_request",
                return_value={"include_sa": True, "include_paye": False, "include_ct": False, "include_vat_mtd": False, "include_sa_mtd": False, "include_cis": False},
            ):
                hmrc648.capture_hmrc_64_8_code({"id": "user-1"}, "request-1", {"authorityCode": "PE12345678"})

    def test_update_request_query_placeholder_count_matches_params(self):
        class _FakeCursor:
            def __init__(self):
                self.query = ""
                self.params = ()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, params=None):
                self.query = str(query)
                self.params = params or ()

            def fetchone(self):
                return {
                    "id": "req-1",
                    "client_id": "C1",
                    "client_name": "Client 1",
                    "include_sa": False,
                    "include_paye": False,
                    "include_ct": False,
                    "include_vat_mtd": True,
                    "include_sa_mtd": False,
                    "include_cis": False,
                    "status": "draft",
                    "submission_channel": "online",
                }

        class _FakeConnection:
            def __init__(self, cursor):
                self._cursor = cursor
                self.committed = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self._cursor

            def commit(self):
                self.committed = True

        existing = {
            "id": "req-1",
            "client_id": "C1",
            "client_name": "Client 1",
            "client_manager": "",
            "client_contact_name": "",
            "client_contact_email": "",
            "client_contact_phone": "",
            "postal_address": "",
            "sa_utr": "",
            "sa_nino": "",
            "ct_utr": "",
            "postcode": "",
            "paye_reference": "",
            "tax_office_number": "",
            "tax_office_reference": "",
            "accounts_office_reference": "",
            "company_number": "",
            "include_sa": False,
            "include_paye": False,
            "include_ct": False,
            "include_vat_mtd": True,
            "include_sa_mtd": False,
            "include_cis": False,
            "status": "draft",
            "submission_channel": "online",
            "hmrc_submission_reference": "",
            "submitted_at": None,
            "expected_code_by": None,
            "reminder_count": 0,
            "last_reminder_at": None,
            "authority_code": "",
            "authority_code_received_at": None,
            "authority_activated_at": None,
            "notes": "",
            "evidence_links": [],
        }
        fake_cursor = _FakeCursor()
        fake_connection = _FakeConnection(fake_cursor)
        with patch.object(hmrc648, "_get_user_request", return_value=existing):
            with patch.object(hmrc648, "get_connection", return_value=fake_connection):
                with patch.object(hmrc648, "_record_audit_event", return_value=None):
                    hmrc648.update_hmrc_64_8_request(
                        {"id": "user-1"},
                        "req-1",
                        {"clientName": "Client 1", "includeVatMtd": True},
                    )
        self.assertIn("UPDATE hmrc_64_8_requests", fake_cursor.query)
        self.assertEqual(fake_cursor.query.count("%s"), len(fake_cursor.params))


if __name__ == "__main__":
    unittest.main()
