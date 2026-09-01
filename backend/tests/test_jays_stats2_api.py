from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

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
except ModuleNotFoundError as exc:  # pragma: no cover
    main = None  # type: ignore[assignment]
    _TEST_IMPORT_ERROR = str(exc)


@unittest.skipIf(main is None, f"Jays Stats 2 API tests skipped: {_TEST_IMPORT_ERROR}")
class JaysStats2ApiTests(unittest.TestCase):
    def test_bulk_route_is_not_captured_by_transaction_id_uuid_route(self):
        bulk_route = None
        tx_update_route = None
        for route in main.app.router.routes:
            path = getattr(route, "path", "")
            if path == "/api/jays-stats-2/transactions/bulk":
                bulk_route = route
            if path == "/api/jays-stats-2/transactions/{transaction_id:uuid}":
                tx_update_route = route
        self.assertIsNotNone(bulk_route)
        self.assertIsNotNone(tx_update_route)

        bulk_match = bool(getattr(bulk_route, "path_regex").match("/api/jays-stats-2/transactions/bulk"))
        tx_match = bool(getattr(tx_update_route, "path_regex").match("/api/jays-stats-2/transactions/bulk"))
        self.assertTrue(bulk_match)
        self.assertFalse(tx_match)


if __name__ == "__main__":
    unittest.main()
