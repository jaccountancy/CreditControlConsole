from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
    from app import xero

    _XERO_TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    xero = None  # type: ignore[assignment]
    _XERO_TEST_IMPORT_ERROR = str(exc)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.is_error = status_code >= 400
        self.text = str(payload)
        self.headers = {}
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.refresh_tokens_used: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None, auth=None):
        self.refresh_tokens_used.append(str((data or {}).get("refresh_token", "")))
        return self._responses.pop(0)


class _FakeCursor:
    def __init__(self, select_rows: list[dict]):
        self._select_rows = list(select_rows)
        self._last_row = None
        self.update_params: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        sql = " ".join(str(query).split())
        if sql.startswith("SELECT * FROM xero_connections WHERE id = %s"):
            if not self._select_rows:
                raise AssertionError("No remaining SELECT rows in test cursor")
            self._last_row = self._select_rows.pop(0)
            return
        if sql.startswith("UPDATE xero_connections SET access_token = %s"):
            self.update_params.append(tuple(params or ()))
            return
        raise AssertionError(f"Unexpected query in test: {sql}")

    def fetchone(self):
        return self._last_row


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        return None


@unittest.skipIf(xero is None, f"Xero refresh tests skipped: {_XERO_TEST_IMPORT_ERROR}")
class XeroRefreshTests(unittest.TestCase):
    def test_connection_refreshed_by_another_request_accepts_rotated_token(self):
        now = datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc)
        rotated = {
            "id": "conn-1",
            "refresh_token": "new-refresh",
            "access_token": "new-access",
            "expires_at": now - timedelta(minutes=1),
        }
        cursor = _FakeCursor(select_rows=[rotated])
        fake_connection = _FakeConnection(cursor)
        with patch.object(xero, "get_connection", return_value=fake_connection):
            found = xero._connection_refreshed_by_another_request("conn-1", "old-refresh")
        self.assertEqual(found["refresh_token"], "new-refresh")

    def test_refresh_connection_retries_with_latest_stored_refresh_token(self):
        now = datetime(2026, 6, 4, 14, 0, tzinfo=timezone.utc)
        original = {
            "id": "conn-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_at": now + timedelta(seconds=30),
        }
        unchanged = dict(original)
        rotated = {**original, "refresh_token": "refresh-2"}
        updated = {**rotated, "access_token": "access-3", "refresh_token": "refresh-3", "expires_at": now + timedelta(hours=1)}

        cursor = _FakeCursor(select_rows=[original, unchanged, rotated, updated])
        fake_connection = _FakeConnection(cursor)

        client = _FakeAsyncClient(
            responses=[
                _FakeResponse(400, {"error": "invalid_grant", "error_description": "Refresh token has been consumed"}),
                _FakeResponse(200, {"access_token": "access-3", "refresh_token": "refresh-3", "expires_in": 3600}),
            ]
        )

        def _client_factory(*args, **kwargs):
            return client

        with patch.object(xero, "get_connection", return_value=fake_connection), patch.object(
            xero, "get_settings", return_value=SimpleNamespace(xero_client_id="id", xero_client_secret="secret")
        ), patch.object(xero, "utcnow", return_value=now), patch.object(
            xero.httpx, "AsyncClient", side_effect=_client_factory
        ), patch.object(
            xero, "XERO_REFRESH_CONCURRENCY_WAIT_SECONDS", 0.0
        ):
            result = asyncio.run(xero.refresh_connection("conn-1"))

        self.assertEqual(result["refresh_token"], "refresh-3")
        self.assertEqual(client.refresh_tokens_used, ["refresh-1", "refresh-2"])
        self.assertEqual(len(cursor.update_params), 1)
        self.assertEqual(cursor.update_params[0][4], "user-1")
        self.assertEqual(cursor.update_params[0][5], "refresh-1")


if __name__ == "__main__":
    unittest.main()
