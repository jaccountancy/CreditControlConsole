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
except ModuleNotFoundError as exc:  # pragma: no cover
    services = None  # type: ignore[assignment]
    _TEST_IMPORT_ERROR = str(exc)


@unittest.skipIf(services is None, f"Workflow tests skipped: {_TEST_IMPORT_ERROR}")
class NewEmployeeFormsWorkflowTests(unittest.TestCase):
    def test_parse_csv_bytes_maps_and_normalises_fields(self):
        csv_text = """Company/Employer Name,GWC NORTH LTD\nName,Evie Jane Dolby\nNational Insurance Number,PL 29 60 01 B\nSort Code,11-03-17\nAccount Number,14367669\nUnknown Custom Field,Custom Value\n"""
        extracted, unmapped, raw_rows = services._submitted_employee_forms_parse_csv_bytes(csv_text.encode("utf-8"))

        self.assertEqual(extracted.get("employerName"), "GWC NORTH LTD")
        self.assertEqual(extracted.get("employeeFullName"), "Evie Jane Dolby")
        self.assertEqual(extracted.get("nationalInsuranceNumber"), "PL296001B")
        self.assertEqual(extracted.get("bankSortCode"), "110317")
        self.assertEqual(extracted.get("bankAccountNumber"), "14367669")
        self.assertEqual(unmapped.get("Unknown Custom Field"), "Custom Value")
        self.assertGreaterEqual(len(raw_rows), 5)

    def test_internal_duplicate_detection_blocks_strong_match(self):
        target = {
            "id": "a",
            "employee_full_name": "Evie Dolby",
            "employee_first_name": "Evie",
            "employee_last_name": "Dolby",
            "employee_email": "evie@example.com",
            "extracted_fields": {
                "nationalInsuranceNumber": "PL296001B",
                "dateOfBirth": "11/06/2006",
                "startDate": "19/07/2026",
            },
        }
        existing = [
            {
                "id": "b",
                "employee_full_name": "Evie Dolby",
                "employee_first_name": "Evie",
                "employee_last_name": "Dolby",
                "employee_email": "evie@example.com",
                "xero_status": "pending",
                "extracted_fields": {
                    "nationalInsuranceNumber": "PL 29 60 01 B",
                    "dateOfBirth": "2006-06-11",
                    "startDate": "2026-07-19",
                },
            }
        ]
        result = services._submitted_forms_duplicate_evaluation(target, existing)

        self.assertTrue(result.get("blockPublish"))
        self.assertEqual(len(result.get("strong") or []), 1)

    def test_xero_duplicate_detection_blocks_ni_and_email_match(self):
        target = {
            "employee_full_name": "Evie Dolby",
            "employee_first_name": "Evie",
            "employee_last_name": "Dolby",
            "employee_email": "evie@example.com",
            "extracted_fields": {
                "nationalInsuranceNumber": "PL296001B",
                "dateOfBirth": "2006-06-11",
                "startDate": "2026-07-19",
            },
        }
        xero_employees = [
            {
                "EmployeeID": "emp-1",
                "FirstName": "Evie",
                "LastName": "Dolby",
                "Email": "evie@example.com",
                "NationalInsuranceNumber": "PL 29 60 01 B",
                "DateOfBirth": "2006-06-11",
            }
        ]
        result = services._submitted_forms_xero_duplicate_evaluation(target, xero_employees)

        self.assertTrue(result.get("blockPublish"))
        self.assertEqual(len(result.get("strong") or []), 1)

    def test_summary_includes_workflow_counts(self):
        rows = [
            {"xero_status": "created", "workflow_status": "partially-published"},
            {"xero_status": "needs-review", "workflow_status": "needs-review"},
            {"xero_status": "pending", "workflow_status": "ready-to-publish"},
        ]
        summary = services._submitted_employee_forms_summary(rows)

        workflow = summary.get("workflow") or {}
        self.assertEqual(workflow.get("partially-published"), 1)
        self.assertEqual(workflow.get("needs-review"), 1)
        self.assertEqual(workflow.get("ready-to-publish"), 1)


if __name__ == "__main__":
    unittest.main()
