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


if __name__ == "__main__":
    unittest.main()
