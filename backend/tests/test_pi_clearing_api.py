import os
import sys
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

os.environ.setdefault("PORT", "8000")
os.environ.setdefault("BASE_URL", "https://example.com")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@example.com:5432/credit_control")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("WIDGET_TOKEN", "test-widget-token")
os.environ.setdefault("XERO_CLIENT_ID", "test-xero-client-id")
os.environ.setdefault("XERO_CLIENT_SECRET", "test-xero-client-secret")
os.environ.setdefault("XERO_REDIRECT_URI", "https://example.com/xero/callback")

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

try:
    from app import main

    _TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    main = None  # type: ignore[assignment]
    _TEST_IMPORT_ERROR = str(exc)


@unittest.skipIf(main is None, f"PI clearing API tests skipped: {_TEST_IMPORT_ERROR}")
class PiClearingApiTests(unittest.TestCase):
    def test_month_close_export_response_contract(self):
        user = {"id": "user-1"}
        payload = b'{"ok":true}'
        with patch.object(
            main,
            "pi_clearing_month_close_export",
            return_value=(payload, 'pi-clearing-"unsafe".json'),
        ):
            response = main.api_pi_clearing_account_month_close_export("close-1", user=user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "application/json")
        self.assertEqual(response.body, payload)
        content_disposition = str(response.headers.get("content-disposition") or "")
        self.assertIn("attachment;", content_disposition)
        self.assertIn('filename="pi-clearing-unsafe.json"', content_disposition)

    def test_month_close_export_propagates_service_error(self):
        user = {"id": "user-1"}
        with patch.object(
            main,
            "pi_clearing_month_close_export",
            side_effect=HTTPException(status_code=404, detail="not found"),
        ):
            with self.assertRaises(HTTPException) as exc:
                main.api_pi_clearing_account_month_close_export("missing-close", user=user)
        self.assertEqual(exc.exception.status_code, 404)
        self.assertEqual(exc.exception.detail, "not found")

    def test_reopen_month_route_passes_payload_to_service(self):
        user = {"id": "user-1"}
        payload = {"reopened": True, "month": "2026-01"}

        class _Request:
            headers = {"content-type": "application/json"}

            async def json(self):
                return {"month": "2026-01", "reason": "manual_reopen"}

        with patch.object(main, "_require_pi_capability", return_value=None), \
             patch.object(main, "reopen_pi_clearing_month", return_value=payload) as mocked_service:
            response = asyncio.run(main.api_reopen_pi_clearing_account_month(_Request(), user=user))
        self.assertEqual(response.get("status"), "ok")
        self.assertTrue(response.get("reopened"))
        mocked_service.assert_called_once()

    def test_retry_credit_notes_route_calls_service(self):
        user = {"id": "user-1"}
        payload = {"prepared": [], "created": [], "skipped": [], "runs": []}

        class _Request:
            headers = {"content-type": "application/json"}

            async def json(self):
                return {"rowIds": ["row-1"]}

        with patch.object(main, "_require_pi_capability", return_value=None), \
             patch.object(main, "retry_pi_clearing_credit_notes", return_value=payload) as mocked_service:
            response = asyncio.run(main.api_retry_pi_clearing_account_credit_notes("run-1", _Request(), user=user))
        self.assertEqual(response.get("status"), "ok")
        mocked_service.assert_called_once()

    def test_mark_manual_failed_route_calls_service(self):
        user = {"id": "user-1"}
        payload = {"updated": 2, "runs": []}

        class _Request:
            headers = {"content-type": "application/json"}

            async def json(self):
                return {"rowIds": ["row-1", "row-2"]}

        with patch.object(main, "_require_pi_capability", return_value=None), \
             patch.object(main, "mark_pi_clearing_credit_notes_failed_manual", return_value=payload) as mocked_service:
            response = asyncio.run(
                main.api_mark_manual_failed_pi_clearing_account_credit_notes("run-1", _Request(), user=user)
            )
        self.assertEqual(response.get("status"), "ok")
        self.assertEqual(response.get("updated"), 2)
        mocked_service.assert_called_once()

    def test_connector_events_route_calls_service(self):
        user = {"id": "user-1"}
        payload = {"accepted": True, "deferred": True, "eventId": "evt-1", "month": "2026-02", "provider": "ignition"}

        class _Request:
            headers = {"content-type": "application/json"}

            async def json(self):
                return {"provider": "ignition", "eventId": "evt-1", "month": "2026-02"}

        with patch.object(main, "_require_pi_capability", return_value=None), \
             patch.object(main, "queue_pi_clearing_connector_event", return_value=payload) as mocked_service:
            response = asyncio.run(main.api_pi_clearing_account_connector_events(_Request(), user=user))
        self.assertEqual(response.get("status"), "ok")
        self.assertTrue(response.get("accepted"))
        self.assertTrue(response.get("deferred"))
        mocked_service.assert_called_once()


if __name__ == "__main__":
    unittest.main()
