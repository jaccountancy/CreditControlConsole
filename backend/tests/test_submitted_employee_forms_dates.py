from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

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


@unittest.skipIf(services is None, f"Submitted employee form tests skipped: {_TEST_IMPORT_ERROR}")
class SubmittedEmployeeFormDateTests(unittest.TestCase):
    def test_normalise_date_accepts_common_formats(self):
        self.assertEqual(services._submitted_employee_forms_normalise_date("1989-04-07"), "1989-04-07")
        self.assertEqual(services._submitted_employee_forms_normalise_date("07/04/1989"), "1989-04-07")
        self.assertEqual(services._submitted_employee_forms_normalise_date("7th Apr 1989"), "1989-04-07")
        self.assertEqual(services._submitted_employee_forms_normalise_date("1989-04-07T00:00:00Z"), "1989-04-07")

    def test_create_payload_sets_date_of_birth_from_non_iso_value(self):
        payload = services._submitted_forms_employee_create_payload(
            {
                "employee_first_name": "Celeste",
                "employee_last_name": "Vivian",
                "employee_email": "celeste@example.com",
                "extracted_fields": {"dateOfBirth": "07/04/1989"},
            }
        )
        self.assertEqual(payload["Employees"][0].get("DateOfBirth"), "1989-04-07")

    def test_create_payload_uses_legacy_dob_key(self):
        payload = services._submitted_forms_employee_create_payload(
            {
                "employee_first_name": "Celeste",
                "employee_last_name": "Vivian",
                "extracted_fields": {"dob": "7 Apr 1989"},
            }
        )
        self.assertEqual(payload["Employees"][0].get("DateOfBirth"), "1989-04-07")

    def test_apply_field_overrides_updates_extracted_and_identity_fields(self):
        row = {
            "employee_full_name": "Celeste Vivian",
            "employee_first_name": "Celeste",
            "employee_last_name": "Vivian",
            "employee_email": "old@example.com",
            "employer_name": "Old Employer",
            "extracted_fields": {"dateOfBirth": "07/04/1989"},
        }
        override = {
            "employeeFirstName": "Sarah",
            "employeeLastName": "Chapman",
            "employeeEmail": "Sarah.Chapman@Example.com",
            "employerName": "S Fleming Ltd",
            "dateOfBirth": "1988-11-02T00:00:00Z",
            "extractedFields": {"jobTitle": "Senior Accountant"},
        }
        updated = services._submitted_employee_forms_apply_field_overrides(row, override)
        self.assertEqual(updated.get("employee_first_name"), "Sarah")
        self.assertEqual(updated.get("employee_last_name"), "Chapman")
        self.assertEqual(updated.get("employee_full_name"), "Sarah Chapman")
        self.assertEqual(updated.get("employee_email"), "sarah.chapman@example.com")
        self.assertEqual(updated.get("employer_name"), "S Fleming Ltd")
        extracted = updated.get("extracted_fields") or {}
        self.assertEqual(extracted.get("dateOfBirth"), "1988-11-02")
        self.assertEqual(extracted.get("jobTitle"), "Senior Accountant")

    def test_normalized_employee_name_collapses_spaces_and_case(self):
        self.assertEqual(
            services._submitted_forms_normalized_employee_name("  Sarah   Chapman "),
            "sarah chapman",
        )

    def test_xero_payroll_employee_identity_prefers_first_and_last_name(self):
        employee_id, email, full_name = services._xero_payroll_employee_identity(
            {
                "EmployeeID": "abc-123",
                "Email": "Sarah.Chapman@Example.com",
                "FirstName": "Sarah",
                "LastName": "Chapman",
                "Name": "Ignored Fallback Name",
            }
        )
        self.assertEqual(employee_id, "abc-123")
        self.assertEqual(email, "sarah.chapman@example.com")
        self.assertEqual(full_name, "sarah chapman")

    def test_submitted_forms_xero_employee_name_strict_ignores_name_fallback(self):
        strict_name = services._submitted_forms_xero_employee_name_strict(
            {
                "Name": "Sarah Chapman",
                "FirstName": "",
                "LastName": "",
            }
        )
        self.assertEqual(strict_name, "")

    def test_submitted_forms_xero_employee_name_strict_uses_first_and_last(self):
        strict_name = services._submitted_forms_xero_employee_name_strict(
            {
                "FirstName": "  Sarah ",
                "LastName": " Chapman  ",
                "Name": "Wrong Fallback",
            }
        )
        self.assertEqual(strict_name, "sarah chapman")


if __name__ == "__main__":
    unittest.main()
