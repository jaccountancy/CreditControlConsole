from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timedelta
from uuid import uuid4
from unittest.mock import patch
from xml.etree import ElementTree as ET

os.environ.setdefault("PORT", "8000")
os.environ.setdefault("BASE_URL", "https://example.com")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@example.com:5432/credit_control")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("WIDGET_TOKEN", "test-widget-token")
os.environ.setdefault("XERO_CLIENT_ID", "test-xero-client-id")
os.environ.setdefault("XERO_CLIENT_SECRET", "test-xero-client-secret")
os.environ.setdefault("XERO_REDIRECT_URI", "https://example.com/xero/callback")

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
    def __init__(self, response: _DummyResponse | list[_DummyResponse]):
        if isinstance(response, list):
            self._responses = list(response)
        else:
            self._responses = [response]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url: str):
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


@unittest.skipIf(ch is None, f"Companies House tests skipped: {_CH_TEST_IMPORT_ERROR}")
class CompaniesHouseTests(unittest.TestCase):
    def test_list_auth_code_register_uses_qualified_columns(self):
        class _FakeCursor:
            def __init__(self):
                self.queries = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, params=None):
                self.queries.append(str(query))

            def fetchall(self):
                return []

            def fetchone(self):
                return {"total": 0}

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
        with patch.object(ch, "get_connection", return_value=fake_connection):
            payload = ch.list_auth_code_register(limit=300)

        self.assertEqual(payload.get("totalCount"), 0)
        self.assertEqual(payload.get("rows"), [])
        self.assertTrue(fake_connection.committed)
        first_query = fake_cursor.queries[0]
        self.assertIn("SELECT r.id", first_query)
        self.assertIn("FROM ch_auth_code_register r", first_query)
        self.assertIn("ON c.company_number = r.company_number", first_query)

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
            "company_number": "12-34",
            "next_due_date": date.today(),
            "share_capital": {},
        }
        errors = ch._validate_cs01_payload(row, date.today())
        self.assertTrue(any("company number" in err.lower() for err in errors))

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

    def test_validate_cs01_payload_allows_future_review_date_within_due_window(self):
        future_review_date = date.today() + timedelta(days=30)
        row = {
            "company_number": "12345678",
            "next_made_up_to_date": future_review_date,
            "next_due_date": date.today() + timedelta(days=60),
            "share_capital": {},
        }
        errors = ch._validate_cs01_payload(row, future_review_date)
        self.assertFalse(any("cannot be in the future" in err for err in errors))

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

    def test_build_cs01_payload_includes_director_personal_code(self):
        row = {
            "company_number": "12345678",
            "next_due_date": date.today(),
            "share_capital": {
                "confirmationStatement": {
                    "identityVerification": {
                        "required": True,
                        "directorPersonalCodeSupplied": True,
                        "directorPersonalCode": "AB12CD34",
                        "verificationStatementGiven": True,
                        "relevantOfficer": "A Director",
                    }
                }
            },
        }
        payload = ch._build_cs01_payload(row)
        self.assertEqual(
            payload.get("identityVerification", {}).get("directorPersonalCode"),
            "AB12CD34",
        )

    def test_deep_merge_dicts_preserves_nested_confirmation_statement_values(self):
        base = {
            "confirmationStatement": {
                "identityVerification": {
                    "directorPersonalCode": "ZXCV1234",
                    "verificationStatementGiven": True,
                },
                "registeredEmailAddress": "ops@example.com",
            }
        }
        patch = {
            "confirmationStatement": {
                "numberOfShareholders": 2,
            }
        }
        merged = ch._deep_merge_dicts(base, patch)
        self.assertEqual(
            merged.get("confirmationStatement", {}).get("identityVerification", {}).get("directorPersonalCode"),
            "ZXCV1234",
        )
        self.assertEqual(merged.get("confirmationStatement", {}).get("numberOfShareholders"), 2)
        self.assertEqual(
            merged.get("confirmationStatement", {}).get("registeredEmailAddress"),
            "ops@example.com",
        )

    def test_extract_director_personal_code_from_nested_payloads(self):
        payload = {
            "items": [
                {"identity_verification": {"personal_code": "ab-12 cd34"}}
            ]
        }
        code = ch._extract_director_personal_code(payload)
        self.assertEqual(code, "AB12CD34")

    def test_extract_shareholder_signals_ingests_company_and_filing_history_variants(self):
        payload = {
            "accounts": {
                "last_accounts": {
                    "number_of_members": "12",
                }
            }
        }
        signals = ch._extract_shareholder_signals(payload, [])
        self.assertEqual(signals.get("confirmationStatement", {}).get("numberOfShareholders"), 12)
        self.assertEqual(
            signals.get("ingestion", {}).get("shareholderCountSource"),
            "accounts.last_accounts.number_of_members",
        )

        filing_history = [
            {
                "descriptionValues": {
                    "members": "15 shareholders",
                }
            }
        ]
        fallback_signals = ch._extract_shareholder_signals({}, filing_history)
        self.assertEqual(fallback_signals.get("confirmationStatement", {}).get("numberOfShareholders"), 15)
        self.assertEqual(
            fallback_signals.get("ingestion", {}).get("shareholderCountSource"),
            "filing_history.descriptionValues.members",
        )

    def test_extract_shareholder_signals_pulls_share_capital_rows_and_statement_of_capital(self):
        payload = {
            "share_capital": [
                {
                    "share_class": "Ordinary",
                    "number_of_shares_issued": "100",
                    "aggregate_nominal_value": "100",
                },
                {
                    "share_class": "Preference",
                    "number_allotted": "50",
                    "nominal_value": "1",
                },
            ]
        }
        signals = ch._extract_shareholder_signals(payload, [])
        self.assertEqual(
            signals.get("statementOfCapital", {}).get("totalNumberOfSharesIssued"),
            "150",
        )
        self.assertEqual(
            signals.get("statementOfCapital", {}).get("totalAggregateNominalValue"),
            "150",
        )
        self.assertEqual(
            signals.get("shareholdings", []),
            [
                {"shareClass": "Ordinary", "numberHeld": "100", "shareholders": []},
                {"shareClass": "Preference", "numberHeld": "50", "shareholders": []},
            ],
        )

    def test_validate_cs01_payload_rejection_matrix(self):
        today = date.today()
        base_row = {
            "company_number": "12345678",
            "next_made_up_to_date": today,
            "next_due_date": today,
            "last_filed_date": today - timedelta(days=365),
            "contact_email": "ops@example.com",
            "sic_codes": ["62012"],
            "pscs": [{"name": "Example PSC", "ceasedOn": ""}],
            "share_capital": {
                "shareholdings": [
                    {
                        "shareClass": "Ordinary",
                        "numberHeld": "10",
                        "shareholders": [{"name": "Jay Wilson"}],
                    }
                ],
                "confirmationStatement": {
                    "stateConfirmation": True,
                    "acceptLawfulPurposeStatement": True,
                },
            },
        }
        base_payload = {
            "reviewPeriodStart": (today - timedelta(days=364)).isoformat(),
            "reviewPeriodEnd": today.isoformat(),
            "registeredEmailAddress": "ops@example.com",
            "acceptLawfulPurposeStatement": True,
            "stateConfirmation": True,
            "tradingOnMarket": False,
            "dtr5Applies": False,
            "pscExemptAsTradingOnRegulatedMarket": False,
            "pscExemptAsSharesAdmittedOnMarket": False,
            "pscExemptAsTradingOnUKRegulatedMarket": False,
            "sicCodes": ["62012"],
            "statementOfCapital": {
                "totalNumberOfSharesIssued": "10",
                "totalAggregateNominalValue": "10",
            },
        }
        scenarios = [
            (
                "invalid_company_number",
                {"company_number": "12-34"},
                {},
                "Company number must be 8 alphanumeric characters.",
            ),
            (
                "review_date_mismatch",
                {"next_made_up_to_date": today - timedelta(days=1)},
                {},
                "Review date must match the recorded made up to date.",
            ),
            (
                "due_date_breach",
                {"next_due_date": today - timedelta(days=1)},
                {},
                "Review date cannot be after the recorded due date.",
            ),
            (
                "review_period_inverted",
                {},
                {
                    "reviewPeriodStart": today.isoformat(),
                    "reviewPeriodEnd": (today - timedelta(days=1)).isoformat(),
                },
                "Review period start cannot be after review period end.",
            ),
            (
                "review_period_end_mismatch",
                {},
                {"reviewPeriodEnd": (today - timedelta(days=1)).isoformat()},
                "Review period end must match the submission review date.",
            ),
            (
                "missing_lawful_statement",
                {},
                {"acceptLawfulPurposeStatement": False},
                "Lawful purpose statement must be accepted for CS01.",
            ),
            (
                "missing_state_confirmation",
                {},
                {"stateConfirmation": False},
                "State confirmation must be set to true for CS01.",
            ),
            (
                "invalid_registered_email",
                {},
                {"registeredEmailAddress": "invalid-email"},
                "Registered email address is required and must be a valid email for CS01.",
            ),
            (
                "dtr5_without_trading",
                {},
                {"dtr5Applies": True, "tradingOnMarket": False},
                "DTR5Applies cannot be true when TradingOnMarket is false.",
            ),
            (
                "exempt_shares_without_dtr5",
                {},
                {"pscExemptAsSharesAdmittedOnMarket": True, "dtr5Applies": False},
                "PSCExemptAsSharesAdmittedOnMarket requires DTR5Applies to be true.",
            ),
            (
                "active_psc_and_exemption_mix",
                {},
                {"pscExemptAsTradingOnRegulatedMarket": True, "tradingOnMarket": True},
                "Active PSC records and PSC exemption flags cannot both be supplied.",
            ),
            (
                "no_psc_and_no_exemption",
                {"pscs": []},
                {},
                "No active PSCs were found and no PSC exemption was selected for CS01.",
            ),
            (
                "invalid_sic_code",
                {},
                {"sicCodes": ["ABC12"]},
                "SIC code 1 must be a 5-digit UK SIC code.",
            ),
            (
                "negative_statement_of_capital",
                {},
                {"statementOfCapital": {"totalNumberOfSharesIssued": "-1", "totalAggregateNominalValue": "10"}},
                "StatementOfCapital totalNumberOfSharesIssued cannot be negative.",
            ),
        ]

        for label, row_patch, payload_patch, expected_error in scenarios:
            with self.subTest(label=label):
                row = {**base_row, **row_patch}
                payload = {**base_payload, **payload_patch}
                errors = ch._validate_cs01_payload(row, today, cs_payload=payload)
                self.assertTrue(any(expected_error in err for err in errors), f"{label} -> {errors}")

    def test_validate_cs01_payload_catches_shareholding_transfer_and_holder_shape_errors(self):
        today = date.today()
        row = {
            "company_number": "12345678",
            "next_made_up_to_date": today,
            "next_due_date": today,
            "contact_email": "ops@example.com",
            "sic_codes": ["62012"],
            "pscs": [{"name": "Example PSC", "ceasedOn": ""}],
            "share_capital": {
                "shareholdings": [
                    {
                        "shareClass": "Ordinary",
                        "numberHeld": "0",
                        "shareholders": [{}, "invalid-holder"],
                        "transfers": [
                            {"dateOfTransfer": "invalid-date", "numberSharesTransferred": "foo"},
                            "invalid-transfer",
                        ],
                    }
                ]
            },
        }
        payload = {
            "reviewPeriodStart": (today - timedelta(days=364)).isoformat(),
            "reviewPeriodEnd": today.isoformat(),
            "registeredEmailAddress": "ops@example.com",
            "acceptLawfulPurposeStatement": True,
            "stateConfirmation": True,
            "tradingOnMarket": False,
            "dtr5Applies": False,
            "pscExemptAsTradingOnRegulatedMarket": False,
            "pscExemptAsSharesAdmittedOnMarket": False,
            "pscExemptAsTradingOnUKRegulatedMarket": False,
            "sicCodes": ["62012"],
        }
        errors = ch._validate_cs01_payload(row, today, cs_payload=payload)
        self.assertTrue(any("NumberHeld must be greater than zero" in err for err in errors), errors)
        self.assertTrue(any("shareholder 1 must include a name" in err for err in errors), errors)
        self.assertTrue(any("shareholder 2 must be an object" in err for err in errors), errors)
        self.assertTrue(any("transfer 1 requires a valid transfer date" in err for err in errors), errors)
        self.assertTrue(any("transfer 1 requires a numeric number of shares" in err for err in errors), errors)
        self.assertTrue(any("transfer 2 must be an object" in err for err in errors), errors)

    def test_validate_cs01_payload_rejects_payload_shareholding_without_share_class(self):
        today = date.today()
        row = {
            "company_number": "12345678",
            "next_made_up_to_date": today,
            "next_due_date": today,
            "contact_email": "ops@example.com",
            "sic_codes": ["62012"],
            "pscs": [{"name": "Example PSC", "ceasedOn": ""}],
            "share_capital": {},
        }
        payload = {
            "reviewPeriodStart": (today - timedelta(days=364)).isoformat(),
            "reviewPeriodEnd": today.isoformat(),
            "registeredEmailAddress": "ops@example.com",
            "acceptLawfulPurposeStatement": True,
            "stateConfirmation": True,
            "tradingOnMarket": False,
            "dtr5Applies": False,
            "pscExemptAsTradingOnRegulatedMarket": False,
            "pscExemptAsSharesAdmittedOnMarket": False,
            "pscExemptAsTradingOnUKRegulatedMarket": False,
            "sicCodes": ["62012"],
            "shareholdings": [
                {
                    "numberHeld": "10",
                    "shareholders": [{"name": "Jay Wilson"}],
                }
            ],
        }
        errors = ch._validate_cs01_payload(row, today, cs_payload=payload)
        self.assertTrue(any("Shareholding row 1 is missing ShareClass." in err for err in errors), errors)

    def test_validate_cs01_payload_rejects_shareholding_list_size_caps(self):
        today = date.today()
        row = {
            "company_number": "12345678",
            "next_made_up_to_date": today,
            "next_due_date": today,
            "contact_email": "ops@example.com",
            "sic_codes": ["62012"],
            "pscs": [{"name": "Example PSC", "ceasedOn": ""}],
            "share_capital": {},
        }
        payload = {
            "reviewPeriodStart": (today - timedelta(days=364)).isoformat(),
            "reviewPeriodEnd": today.isoformat(),
            "registeredEmailAddress": "ops@example.com",
            "acceptLawfulPurposeStatement": True,
            "stateConfirmation": True,
            "tradingOnMarket": False,
            "dtr5Applies": False,
            "pscExemptAsTradingOnRegulatedMarket": False,
            "pscExemptAsSharesAdmittedOnMarket": False,
            "pscExemptAsTradingOnUKRegulatedMarket": False,
            "sicCodes": ["62012"],
            "shareholdings": [
                {
                    "shareClass": "Ordinary",
                    "numberHeld": "10",
                    "shareholders": [{"name": f"Holder {index}"} for index in range(11)],
                    "transfers": [{"dateOfTransfer": "2026-05-01", "numberSharesTransferred": "1"} for _ in range(201)],
                }
            ],
        }
        errors = ch._validate_cs01_payload(row, today, cs_payload=payload)
        self.assertTrue(any("cannot include more than 10 shareholders" in err for err in errors), errors)
        self.assertTrue(any("cannot include more than 200 transfers" in err for err in errors), errors)

    def test_live_like_cs01_payload_variants_render_submission_xml(self):
        review_date = date(2026, 6, 1)
        variants = [
            {
                "registeredEmailAddress": "ops@example.com",
                "acceptLawfulPurposeStatement": True,
                "stateConfirmation": True,
            },
            {
                "registeredEmailAddress": "ops@example.com",
                "acceptLawfulPurposeStatement": True,
                "stateConfirmation": True,
                "sicCodes": ["62012", "63120", "69201"],
                "statementOfCapital": {
                    "totalNumberOfSharesIssued": "100",
                    "totalAggregateNominalValue": "100.00",
                },
            },
            {
                "registeredEmailAddress": "ops@example.com",
                "acceptLawfulPurposeStatement": True,
                "stateConfirmation": True,
                "shareholdings": [
                    {
                        "shareClass": "Ordinary",
                        "numberHeld": "100",
                        "shareholders": [
                            {"name": "Wilson, Jay"},
                            {"fullName": "Jordan Blake"},
                        ],
                        "transfers": [
                            {"dateOfTransfer": "2026-05-20", "numberSharesTransferred": "10"},
                        ],
                    }
                ],
            },
            {
                "registeredEmailAddress": "ops@example.com",
                "acceptLawfulPurposeStatement": True,
                "stateConfirmation": True,
                "tradingOnMarket": True,
                "pscExemptAsTradingOnRegulatedMarket": True,
                "pscExemptAsTradingOnUKRegulatedMarket": True,
            },
        ]

        for idx, payload in enumerate(variants, start=1):
            with self.subTest(variant=idx):
                xml = ch._build_ch_submission_xml(
                    presenter_id="00046248000",
                    presenter_auth="PLCTL2F87WL",
                    environment="production",
                    company_number="12345678",
                    company_name="Example Ltd",
                    company_auth_code="A1B2C3",
                    review_date=review_date,
                    registered_email="ops@example.com",
                    package_reference=f"pkg-{idx}",
                    transaction_id=f"tx-{idx}",
                    submission_number=f"{idx:06d}",
                    cs_payload=payload,
                )
                self.assertIn(b"ConfirmationStatement", xml)
                self.assertIn(b"ReviewDate", xml)

    def test_prefill_no_changes_payload_removes_change_sections(self):
        review_date = date(2026, 6, 1)
        row = {
            "id": "company-1",
            "last_filed_date": date(2025, 6, 1),
            "share_capital": {
                "statementOfCapital": {
                    "totalNumberOfSharesIssued": "100",
                    "totalAggregateNominalValue": "100.00",
                },
                "shareholdings": [
                    {
                        "shareClass": "Ordinary",
                        "numberHeld": "100",
                        "shareholders": [{"name": "Jay Wilson"}],
                    }
                ],
            },
            "sic_codes": ["62012"],
        }
        current_payload = {
            "sicCodes": ["62012"],
            "statementOfCapital": {"totalNumberOfSharesIssued": "100"},
            "shareholdings": [{"shareClass": "Ordinary", "numberHeld": "100"}],
            "acceptLawfulPurposeStatement": True,
            "stateConfirmation": True,
        }
        with patch.object(ch, "_load_latest_submission_cs01_payload", return_value=current_payload):
            payload = ch._prefill_no_changes_cs01_payload(row, current_payload, review_date)

        self.assertNotIn("sicCodes", payload)
        self.assertNotIn("statementOfCapital", payload)
        self.assertNotIn("shareholdings", payload)
        self.assertEqual(payload.get("reviewPeriodEnd"), review_date.isoformat())

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

    def test_build_status_xml_hashes_presenter_auth_with_md5(self):
        xml = ch._build_ch_status_xml(
            presenter_id="00046248000",
            presenter_auth="PLCTL2F87WL",
            environment="production",
            transaction_id="tx-1",
            submission_number="ZZZZZZ",
        )
        root = ET.fromstring(xml)
        method = root.find(".//{*}Method")
        value = root.find(".//{*}Value")
        self.assertIsNotNone(method)
        self.assertIsNotNone(value)
        self.assertEqual(method.text, "MD5")
        self.assertEqual(value.text, ch._ch_md5_auth_value("PLCTL2F87WL"))

    def test_build_submission_xml_hashes_presenter_auth_with_md5(self):
        xml = ch._build_ch_submission_xml(
            presenter_id="00046248000",
            presenter_auth="PLCTL2F87WL",
            environment="production",
            company_number="12345678",
            company_name="Example Ltd",
            company_auth_code="A1B2C3",
            review_date=date(2026, 6, 1),
            registered_email="ops@example.com",
            package_reference="pkg-1",
            transaction_id="tx-2",
            submission_number="123456",
            cs_payload={},
        )
        root = ET.fromstring(xml)
        method = root.find(".//{*}Method")
        value = root.find(".//{*}Value")
        self.assertIsNotNone(method)
        self.assertIsNotNone(value)
        self.assertEqual(method.text, "MD5")
        self.assertEqual(value.text, ch._ch_md5_auth_value("PLCTL2F87WL"))

    def test_authorisation_failure_reason_includes_uk_causes(self):
        reason = ch._enhance_authorisation_failure_reason(
            "ConfirmationStatement - 502 - Authorisation Failure",
            environment="sandbox",
            presenter_id="PRESENTER1234",
            presenter_auth="ABCDEF12",
            company_auth_code="A1B2C3",
            company_number="13279119",
        )
        self.assertIn("Authorisation check: environment=sandbox", reason)
        self.assertIn("Likely UK causes:", reason)
        self.assertIn("not a GOV.UK One Login / personal code", reason)

    def test_bulk_submit_rejects_invalid_xero_unit_amount_setting(self):
        company_id = "77b42a3f-2a17-4e95-bfa4-c4fca152585d"
        with patch.object(
            ch,
            "_ensure_settings_row",
            return_value={
                "xero_invoice_unit_amount": "not-a-number",
            },
        ):
            with self.assertRaises(HTTPException) as ctx:
                ch.bulk_submit_confirmation_statements({"id": "u1"}, {"companyIds": [company_id]})
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("xeroInvoiceUnitAmount", str(ctx.exception.detail))

    def test_bulk_invoice_rejects_invalid_xero_unit_amount_setting(self):
        company_id = "77b42a3f-2a17-4e95-bfa4-c4fca152585d"
        with patch.object(
            ch,
            "_ensure_settings_row",
            return_value={
                "xero_invoice_account_code": "200",
                "xero_invoice_item_code": "",
                "xero_invoice_description": "Companies House confirmation statement filing",
                "xero_invoice_tax_type": "NONE",
                "xero_invoice_unit_amount": "not-a-number",
            },
        ):
            with self.assertRaises(HTTPException) as ctx:
                import asyncio

                asyncio.run(ch.bulk_raise_submission_invoices({"id": "u1"}, {"companyIds": [company_id]}))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("xeroInvoiceUnitAmount", str(ctx.exception.detail))

    def test_connection_test_invalid_credentials(self):
        with patch.object(ch, "_ensure_settings_row", return_value={"environment": "sandbox"}), \
             patch.object(ch, "decrypt_api_key", return_value="bad-key"), \
             patch.object(ch, "_connection_test_probe_company_number", return_value="00000000"), \
             patch.object(
                 ch,
                 "_companies_house_http_client",
                 return_value=_DummyClient(
                     [
                         _DummyResponse(401, {}),
                         _DummyResponse(401, {}),
                     ]
                 ),
             ):
            with self.assertRaises(HTTPException) as ctx:
                ch.test_companies_house_connection()
        self.assertEqual(ctx.exception.status_code, 400)

    def test_connection_test_detects_environment_mismatch(self):
        with patch.object(ch, "_ensure_settings_row", return_value={"environment": "production"}), \
             patch.object(ch, "decrypt_api_key", return_value="env-specific-key"), \
             patch.object(ch, "_connection_test_probe_company_number", return_value="00000000"), \
             patch.object(
                 ch,
                 "_companies_house_http_client",
                 return_value=_DummyClient(
                     [
                         _DummyResponse(401, {}),
                         _DummyResponse(404, {}),
                     ]
                 ),
             ):
            with self.assertRaises(HTTPException) as ctx:
                ch.test_companies_house_connection()
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("rejected in production", str(ctx.exception.detail))
        self.assertIn("accepted in sandbox", str(ctx.exception.detail))

    def test_connection_test_treats_404_probe_as_success(self):
        with patch.object(ch, "_ensure_settings_row", return_value={"environment": "sandbox"}), \
             patch.object(ch, "decrypt_api_key", return_value="good-key"), \
             patch.object(ch, "configured_presenter_id", return_value=""), \
             patch.object(ch, "configured_presenter_auth", return_value=""), \
             patch.object(ch, "_connection_test_probe_company_number", return_value="00000000"), \
             patch.object(
                 ch,
                 "_companies_house_http_client",
                 return_value=_DummyClient(_DummyResponse(404, {})),
             ):
            payload = ch.test_companies_house_connection()
        self.assertTrue(payload.get("connected"))
        self.assertEqual(payload.get("statusCode"), 404)
        self.assertTrue(str(payload.get("endpoint") or "").endswith("/company/00000000"))

    def test_connection_test_treats_400_probe_as_success(self):
        with patch.object(ch, "_ensure_settings_row", return_value={"environment": "sandbox"}), \
             patch.object(ch, "decrypt_api_key", return_value="good-key"), \
             patch.object(ch, "configured_presenter_id", return_value=""), \
             patch.object(ch, "configured_presenter_auth", return_value=""), \
             patch.object(ch, "_connection_test_probe_company_number", return_value="00000000"), \
             patch.object(
                 ch,
                 "_companies_house_http_client",
                 return_value=_DummyClient(_DummyResponse(400, {"error": "invalid company number"})),
             ):
            payload = ch.test_companies_house_connection()
        self.assertTrue(payload.get("connected"))
        self.assertEqual(payload.get("statusCode"), 400)

    def test_connection_test_uses_unsaved_overrides(self):
        with patch.object(ch, "_ensure_settings_row", return_value={"environment": "sandbox", "presenter_id": ""}), \
             patch.object(ch, "configured_presenter_id", return_value=""), \
             patch.object(ch, "configured_presenter_auth", return_value=""), \
             patch.object(ch, "_connection_test_probe_company_number", return_value="00000000"), \
             patch.object(ch, "_companies_house_http_client", return_value=_DummyClient(_DummyResponse(404, {}))) as mock_client, \
             patch.object(ch, "configured_api_key", return_value="hardcoded-key"):
            payload = ch.test_companies_house_connection(
                {
                    "environment": "production",
                    "apiKey": "override-key",
                }
            )

        self.assertTrue(payload.get("connected"))
        self.assertEqual(payload.get("environment"), "production")
        self.assertIn("api.company-information.service.gov.uk", str(payload.get("endpoint") or ""))
        self.assertEqual(mock_client.call_args.args[0], "hardcoded-key")

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

    def test_normalise_workflow_review_stringifies_uuid_user_id(self):
        user_id = uuid4()
        review = ch._normalise_workflow_review(
            {
                "sections": {key: True for key in ch.CH_WORKFLOW_REVIEW_SECTIONS},
                "notes": "All complete",
            },
            user_id=user_id,
        )
        self.assertEqual(review.get("updatedByUserId"), str(user_id))


if __name__ == "__main__":
    unittest.main()
