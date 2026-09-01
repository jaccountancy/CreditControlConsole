from __future__ import annotations

import os
import sys
import asyncio
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

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


class _Cursor:
    def __init__(self, row: dict | None = None, rows: list[dict] | None = None):
        self._row = row or {}
        self._rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, _query, _params):
        return None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, row: dict | None = None, rows: list[dict] | None = None):
        self._row = row or {}
        self._rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _Cursor(row=self._row, rows=self._rows)

    def commit(self):
        return None


class _RecordingCursor:
    def __init__(self):
        self.executed: list[tuple[str, tuple | None]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.executed.append((str(query or ""), params))

    def fetchone(self):
        return {}

    def fetchall(self):
        return []


class _RecordingConnection:
    def __init__(self):
        self._cursor = _RecordingCursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        return None


@unittest.skipIf(services is None, f"Regression tests skipped: {_TEST_IMPORT_ERROR}")
class ServicesRegressionTests(unittest.TestCase):
    def test_month_start_supports_no_argument(self):
        fixed_now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now):
            self.assertEqual(services._month_start(), date(2026, 6, 1))

    def test_pi_month_bounds_default_month(self):
        fixed_now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now):
            start, end = services._pi_month_bounds(None)
        self.assertEqual(start, date(2026, 6, 1))
        self.assertEqual(end, date(2026, 6, 30))

    def test_pi_period_gate_rejects_locked_month(self):
        gate_payload = {
            "openMonthStart": date(2026, 1, 1),
            "openMonthEnd": date(2026, 1, 31),
            "openMonth": "2026-01",
            "openStatus": "open",
        }
        with patch.object(services, "_pi_apply_period_gate", return_value=gate_payload):
            with self.assertRaises(services.HTTPException) as exc:
                services._pi_assert_requested_open_month(
                    cursor=object(),
                    user_id="user-1",
                    tenant_id="tenant-1",
                    requested_start=date(2026, 2, 1),
                    requested_end=date(2026, 2, 28),
                )
        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual((exc.exception.detail or {}).get("code"), services.PI_CLEARING_PERIOD_LOCK_ERROR_CODE)

    def test_pi_period_gate_accepts_open_month(self):
        gate_payload = {
            "openMonthStart": date(2026, 1, 1),
            "openMonthEnd": date(2026, 1, 31),
            "openMonth": "2026-01",
            "openStatus": "open",
        }
        with patch.object(services, "_pi_apply_period_gate", return_value=gate_payload):
            result = services._pi_assert_requested_open_month(
                cursor=object(),
                user_id="user-1",
                tenant_id="tenant-1",
                requested_start=date(2026, 1, 1),
                requested_end=date(2026, 1, 31),
            )
        self.assertEqual(result.get("openMonth"), "2026-01")

    def test_pi_workflow_period_locked_prevents_source_loaders(self):
        async def _run():
            user = {"id": "user-1"}
            locked_error = services.HTTPException(
                status_code=409,
                detail={"code": services.PI_CLEARING_PERIOD_LOCK_ERROR_CODE, "message": "Locked"},
            )
            with patch.object(services, "_pi_batch_bounds", return_value=(date(2026, 2, 1), date(2026, 2, 28))), \
                 patch.object(services, "_pi_clearing_account_code_for_user", return_value="PI Clearing Account"), \
                 patch.object(services, "get_master_xero_connection_for_user", return_value={"tenant_id": "tenant-1"}), \
                 patch.object(services, "get_connection", return_value=_Connection()), \
                 patch.object(services, "_pi_assert_requested_open_month", side_effect=locked_error), \
                 patch.object(services, "_pi_load_xero_payments") as mocked_xero_loader, \
                 patch.object(services, "_pi_load_ignition_payments") as mocked_ignition_loader:
                with self.assertRaises(services.HTTPException) as exc:
                    await services.run_pi_clearing_workflow(user, {"batchStart": "2026-02-01", "batchEnd": "2026-02-28"})
                self.assertEqual(exc.exception.status_code, 409)
                self.assertEqual((exc.exception.detail or {}).get("code"), services.PI_CLEARING_PERIOD_LOCK_ERROR_CODE)
                mocked_xero_loader.assert_not_called()
                mocked_ignition_loader.assert_not_called()

        asyncio.run(_run())

    def test_pi_business_action_key_is_deterministic(self):
        key_one = services._pi_build_business_action_key(
            user_id="user-1",
            tenant_id="tenant-1",
            run_id="run-1",
            run_row_id="row-1",
            action_type="CREATE_RECOVERY_INVOICE",
            payout_id="payout-1",
            collection_ids=["c2", "c1"],
            xero_contact_id="contact-1",
            amount=Decimal("95.04"),
            note_date=date(2026, 1, 30),
        )
        key_two = services._pi_build_business_action_key(
            user_id="user-1",
            tenant_id="tenant-1",
            run_id="run-1",
            run_row_id="row-1",
            action_type="CREATE_RECOVERY_INVOICE",
            payout_id="payout-1",
            collection_ids=["c1", "c2"],
            xero_contact_id="contact-1",
            amount=Decimal("95.04"),
            note_date=date(2026, 1, 30),
        )
        self.assertEqual(key_one, key_two)
        self.assertEqual(len(key_one), 64)

    def test_pi_posting_policy_allows_only_recoverable_high_confidence(self):
        row = {
            "difference_total": 95.04,
            "raw_payload": {
                "ignitionReversalTotal": 95.04,
                "step1MissingDebitTotal": 0,
                "riskScore": 97,
                "reasonCode": "chargeback",
            },
        }
        with patch.object(
            services,
            "_pi_reason_policy_decision",
            return_value={
                "reasonCode": "chargeback",
                "treatment": "create_recovery_invoice",
                "recoverable": True,
                "autoAllowlist": True,
                "confidenceThreshold": 95,
            },
        ):
            allowed, code, _reason, _policy = services._pi_posting_policy_for_row(row, user_id="user-1", tenant_id="tenant-1")
            self.assertTrue(allowed)
            self.assertEqual(code, services.PI_CLEARING_ALLOWED_POST_ACTION)

        blocked_row = {
            "difference_total": 95.04,
            "raw_payload": {
                "ignitionReversalTotal": 0,
                "step1MissingDebitTotal": 0,
                "riskScore": 97,
            },
        }
        with patch.object(
            services,
            "_pi_reason_policy_decision",
            return_value={
                "reasonCode": "unknown",
                "treatment": "manual_review",
                "recoverable": False,
                "autoAllowlist": False,
                "confidenceThreshold": 95,
            },
        ):
            allowed, code, _reason, _policy = services._pi_posting_policy_for_row(blocked_row, user_id="user-1", tenant_id="tenant-1")
            self.assertFalse(allowed)
            self.assertEqual(code, "RULE_NOT_ALLOWLISTED")

    def test_pi_period_gate_rejects_later_month_for_multiple_non_closed_states(self):
        gate_payloads = [
            {"openMonthStart": date(2026, 1, 1), "openMonthEnd": date(2026, 1, 31), "openMonth": "2026-01", "openStatus": "open"},
            {"openMonthStart": date(2026, 1, 1), "openMonthEnd": date(2026, 1, 31), "openMonth": "2026-01", "openStatus": "exception"},
            {"openMonthStart": date(2026, 1, 1), "openMonthEnd": date(2026, 1, 31), "openMonth": "2026-01", "openStatus": "ready_for_close"},
            {"openMonthStart": date(2026, 1, 1), "openMonthEnd": date(2026, 1, 31), "openMonth": "2026-01", "openStatus": "reopened"},
            {"openMonthStart": date(2026, 1, 1), "openMonthEnd": date(2026, 1, 31), "openMonth": "2026-01", "openStatus": "failed"},
        ]
        for gate_payload in gate_payloads:
            with patch.object(services, "_pi_apply_period_gate", return_value=gate_payload):
                with self.assertRaises(services.HTTPException) as exc:
                    services._pi_assert_requested_open_month(
                        cursor=object(),
                        user_id="user-1",
                        tenant_id="tenant-1",
                        requested_start=date(2026, 2, 1),
                        requested_end=date(2026, 2, 28),
                    )
                self.assertEqual(exc.exception.status_code, 409)
                self.assertEqual((exc.exception.detail or {}).get("code"), services.PI_CLEARING_PERIOD_LOCK_ERROR_CODE)

    def test_pi_daily_evidence_rows_include_no_activity_days(self):
        evidence = services._pi_daily_evidence_rows(
            month_start=date(2026, 1, 1),
            month_end=date(2026, 1, 3),
            xero_rows=[],
            ignition_rows=[],
        )
        self.assertEqual(len(evidence), 3)
        self.assertTrue(all(str(item.get("status")) == "no_activity_confirmed" for item in evidence))

    def test_pi_month_close_export_missing_record_raises_404(self):
        with patch.object(services, "get_connection", return_value=_Connection(row={})):
            with self.assertRaises(services.HTTPException) as exc:
                services.pi_clearing_month_close_export({"id": "user-1"}, "close-1")
        self.assertEqual(exc.exception.status_code, 404)

    def test_pi_close_predicates_recompute_from_persisted_rows_and_detect_source_change(self):
        class _ScriptedCursor:
            def __init__(self):
                self._last_query = ""

            def execute(self, query, _params=None):
                self._last_query = str(query or "")

            def fetchall(self):
                if "FROM pi_clearing_run_rows" in self._last_query:
                    return [
                        {
                            "difference_total": Decimal("95.04"),
                            "raw_payload": {
                                "xeroRows": [
                                    {
                                        "date": "2026-01-10",
                                        "amount": -95.04,
                                        "paymentId": "xero-1",
                                        "clientName": "Client A",
                                        "clientKey": "client-a",
                                    }
                                ],
                                "ignitionRows": [],
                            },
                        }
                    ]
                return []

            def fetchone(self):
                if "COUNT(*) AS unresolved_count" in self._last_query:
                    return {"unresolved_count": 1}
                if "COUNT(*) AS pending_actions" in self._last_query:
                    return {"pending_actions": 0}
                if "FROM payments" in self._last_query:
                    return {"source_updated_at": datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)}
                if "FROM ignition_reporting_records" in self._last_query:
                    return {"source_updated_at": datetime(2026, 1, 11, 12, 0, tzinfo=timezone.utc)}
                return {}

        cursor = _ScriptedCursor()
        run_row = {
            "id": "run-1",
            "month_start": date(2026, 1, 1),
            "month_end": date(2026, 1, 31),
            "updated_at": datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc),
        }
        summary = {"xeroStep1": {"missingCreditCount": 0, "missingCreditPaymentIds": []}}

        with patch.object(
            services,
            "_pi_load_xero_payments",
            return_value=(
                [
                    {"amount": 95.04},
                    {"amount": -95.04},
                ],
                "tenant-1",
                "2026-01-31T00:00:00+00:00",
            ),
        ):
            checks = services._pi_close_predicate_checks(
                cursor,
                user={"id": "user-1"},
                tenant_id="tenant-1",
                run_row=run_row,
                summary=summary,
            )
        by_code = {str(item.get("code")): item for item in (checks.get("checks") or [])}
        self.assertFalse(bool(by_code.get("UNRESOLVED_DIFFERENCES", {}).get("passed")))
        self.assertFalse(bool(by_code.get("SOURCE_UNCHANGED", {}).get("passed")))
        self.assertTrue(bool(by_code.get("XERO_MONTH_END_DIRECT_NIL", {}).get("passed")))

    def test_apply_pi_credit_notes_handles_unique_violation_as_duplicate_skip(self):
        class _RunAndRowsCursor:
            def __init__(self):
                self._last_query = ""

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, _params=None):
                self._last_query = str(query or "")

            def fetchone(self):
                if "FROM pi_clearing_runs" in self._last_query:
                    return {
                        "id": "run-1",
                        "tenant_id": "tenant-1",
                        "month_start": date(2026, 1, 1),
                        "month_end": date(2026, 1, 31),
                        "account_code": "200",
                        "summary": {},
                    }
                return {}

            def fetchall(self):
                if "FROM pi_clearing_run_rows" in self._last_query:
                    return [
                        {
                            "id": "row-1",
                            "difference_total": Decimal("95.04"),
                            "xero_contact_id": "contact-1",
                            "currency_code": "GBP",
                            "client_name": "Client A",
                            "raw_payload": {
                                "ignitionReversalTotal": 95.04,
                                "step1MissingDebitTotal": 0,
                                "riskScore": 99,
                                "reasonCode": "chargeback",
                                "payoutDate": "2026-01-15",
                                "payoutId": "payout-1",
                                "ignitionPaymentIds": ["ign-1"],
                            },
                            "ignition_payment_ids": ["ign-1"],
                        }
                    ]
                return []

        class _ExistingActionCursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, _query, _params=None):
                return None

            def fetchone(self):
                return {}

        class _InsertCursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, _params=None):
                if "INSERT INTO pi_clearing_credit_notes" in str(query or ""):
                    raise services.pg_errors.UniqueViolation("duplicate")
                return None

            def fetchone(self):
                return {}

        class _Conn:
            def __init__(self, cursor_obj):
                self._cursor_obj = cursor_obj

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self._cursor_obj

            def commit(self):
                return None

        async def _run():
            with patch.object(
                services,
                "get_connection",
                side_effect=[
                    _Conn(_RunAndRowsCursor()),
                    _Conn(_ExistingActionCursor()),
                    _Conn(_InsertCursor()),
                    _Conn(_InsertCursor()),
                    _Conn(_InsertCursor()),
                ],
            ), patch.object(services, "get_master_xero_connection_for_user", return_value={"tenant_id": "tenant-1"}), \
                 patch.object(services, "_fetch_xero_chart_of_accounts", return_value=[{"code": "200", "name": "PI Clearing"}]), \
                 patch.object(
                     services,
                     "_pi_posting_policy_for_row",
                     return_value=(True, services.PI_CLEARING_ALLOWED_POST_ACTION, "ok", {"reasonCode": "chargeback", "treatment": "create_recovery_invoice"}),
                 ), \
                 patch.object(
                     services,
                     "_pi_get_action_approval",
                     return_value={
                         "status": services.PI_CLEARING_ACTION_STATUS_APPROVED,
                         "payload_hash": "",
                         "source_snapshot_hash": "",
                     },
                 ), \
                 patch.object(services, "_pi_record_action_approval", return_value=None), \
                 patch.object(services, "posting_settings_for_tenant", return_value={"piClearingWritesEnabled": True}), \
                 patch.object(services, "_pi_assert_requested_open_month", return_value={"openMonth": "2026-01"}), \
                 patch.object(services, "create_sales_invoice", return_value={"Invoices": [{"InvoiceID": "inv-1", "InvoiceNumber": "INV-1"}]}), \
                 patch.object(
                     services,
                     "_pi_find_existing_credit_note_action",
                     return_value={
                         "id": "existing-1",
                         "xero_credit_note_id": "inv-1",
                         "xero_credit_note_number": "INV-1",
                         "status": "created",
                     },
                 ), \
                 patch.object(services, "pi_clearing_payload", return_value={"runs": []}):
                result = await services.apply_pi_clearing_credit_notes(
                    {"id": "user-1"},
                    "run-1",
                    {"confirmed": True, "accountCode": "200"},
                )
                self.assertEqual(result.get("created"), [])
                self.assertEqual(len(result.get("skipped") or []), 1)
                self.assertEqual((result.get("skipped") or [{}])[0].get("policyCode"), "DUPLICATE_ACTION")
                self.assertEqual((result.get("skipped") or [{}])[0].get("existingActionId"), "existing-1")

        asyncio.run(_run())

    def test_queue_connector_event_deferred_when_month_locked(self):
        async def _run():
            user = {"id": "user-1"}
            recorder = _RecordingConnection()
            locked = services.HTTPException(
                status_code=409,
                detail={"code": services.PI_CLEARING_PERIOD_LOCK_ERROR_CODE, "message": "Locked"},
            )
            with patch.object(services, "get_master_xero_connection_for_user", return_value={"tenant_id": "tenant-1"}), \
                 patch.object(services, "get_connection", return_value=recorder), \
                 patch.object(services, "_pi_assert_requested_open_month", side_effect=locked), \
                 patch.object(services, "record_audit_event", return_value=None):
                result = await services.queue_pi_clearing_connector_event(
                    user,
                    {"provider": "ignition", "eventId": "evt-1", "month": "2026-02"},
                )
            self.assertTrue(result.get("accepted"))
            self.assertTrue(result.get("deferred"))
            self.assertEqual(result.get("month"), "2026-02")
            statuses = [
                params[6]
                for query, params in recorder._cursor.executed
                if "INSERT INTO pi_clearing_deferred_events" in query and params
            ]
            self.assertIn("deferred", statuses)

        asyncio.run(_run())

    def test_queue_connector_event_ready_when_month_open(self):
        async def _run():
            user = {"id": "user-1"}
            recorder = _RecordingConnection()
            with patch.object(services, "get_master_xero_connection_for_user", return_value={"tenant_id": "tenant-1"}), \
                 patch.object(services, "get_connection", return_value=recorder), \
                 patch.object(services, "_pi_assert_requested_open_month", return_value={"openMonth": "2026-01"}), \
                 patch.object(services, "record_audit_event", return_value=None):
                result = await services.queue_pi_clearing_connector_event(
                    user,
                    {"provider": "ignition", "eventId": "evt-2", "month": "2026-01"},
                )
            self.assertTrue(result.get("accepted"))
            self.assertFalse(result.get("deferred"))
            self.assertEqual(result.get("month"), "2026-01")
            statuses = [
                params[6]
                for query, params in recorder._cursor.executed
                if "INSERT INTO pi_clearing_deferred_events" in query and params
            ]
            self.assertIn("ready", statuses)

        asyncio.run(_run())

    def test_pi_writes_disabled_blocks_credit_note_posting(self):
        class _RunCursor:
            def __init__(self):
                self._last_query = ""

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, _params=None):
                self._last_query = str(query or "")

            def fetchone(self):
                if "FROM pi_clearing_runs" in self._last_query:
                    return {
                        "id": "run-1",
                        "tenant_id": "tenant-1",
                        "month_start": date(2026, 1, 1),
                        "month_end": date(2026, 1, 31),
                        "account_code": "200",
                        "summary": {},
                    }
                return {}

            def fetchall(self):
                if "FROM pi_clearing_run_rows" in self._last_query:
                    return [
                        {
                            "id": "row-1",
                            "difference_total": Decimal("95.04"),
                            "xero_contact_id": "contact-1",
                            "currency_code": "GBP",
                            "client_name": "Client A",
                            "raw_payload": {
                                "ignitionReversalTotal": 95.04,
                                "step1MissingDebitTotal": 0,
                                "riskScore": 99,
                                "reasonCode": "chargeback",
                            },
                        }
                    ]
                return []

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return _RunCursor()

            def commit(self):
                return None

        async def _run():
            with patch.object(services, "get_connection", return_value=_Conn()), \
                 patch.object(services, "_pi_assert_requested_open_month", return_value={"openMonth": "2026-01"}), \
                 patch.object(services, "posting_settings_for_tenant", return_value={"piClearingWritesEnabled": False}):
                with self.assertRaises(services.HTTPException) as exc:
                    await services.apply_pi_clearing_credit_notes(
                        {"id": "user-1"},
                        "run-1",
                        {"confirmed": True, "accountCode": "200"},
                    )
                self.assertEqual(exc.exception.status_code, 409)
                self.assertEqual((exc.exception.detail or {}).get("code"), "PI_CLEARING_WRITES_DISABLED")

        asyncio.run(_run())

    def test_pi_posting_settings_include_write_switch_and_fee_treatment(self):
        payload = services._serialize_posting_settings(
            {
                "tenant_id": "tenant-1",
                "pi_clearing_account_code": "200",
                "pi_clearing_account_locked": True,
                "pi_clearing_writes_enabled": False,
                "pi_clearing_fee_treatment": "manual_review",
            },
            tenant_id="tenant-1",
        )
        self.assertFalse(payload.get("piClearingWritesEnabled"))
        self.assertEqual(payload.get("piClearingFeeTreatment"), "manual_review")

    def test_pi_reopen_closed_months_source_change_scanner_no_rows(self):
        class _CursorNoRows:
            def execute(self, _query, _params=None):
                return None

            def fetchall(self):
                return []

            def fetchone(self):
                return {}

        with patch.object(services, "record_audit_event") as mocked_audit:
            services._pi_reopen_closed_months_with_source_changes(_CursorNoRows(), "user-1", "tenant-1")
        mocked_audit.assert_not_called()

    def test_retry_pi_credit_notes_requeues_failed_retryable_actions(self):
        async def _run():
            user = {"id": "user-1"}
            with patch.object(
                services,
                "_pi_get_latest_action_approval_for_row",
                side_effect=[
                    {
                        "status": services.PI_CLEARING_ACTION_STATUS_FAILED_RETRYABLE,
                        "business_action_key": "key-1",
                        "payload_hash": "hash-1",
                        "source_snapshot_hash": "source-1",
                        "tenant_id": "tenant-1",
                    },
                    {
                        "status": services.PI_CLEARING_ACTION_STATUS_APPROVED,
                        "business_action_key": "key-2",
                        "payload_hash": "hash-2",
                        "source_snapshot_hash": "source-2",
                        "tenant_id": "tenant-1",
                    },
                ],
            ), patch.object(services, "_pi_record_action_approval", return_value=None), \
                 patch.object(services, "record_audit_event", return_value=None), \
                 patch.object(
                     services,
                     "apply_pi_clearing_credit_notes",
                     return_value={"created": [{"runRowId": "row-1"}], "skipped": [], "runs": []},
                 ) as mocked_apply:
                result = await services.retry_pi_clearing_credit_notes(
                    user,
                    "run-1",
                    {"rowIds": ["row-1", "row-2"], "accountCode": "200"},
                )
            self.assertEqual(len(result.get("prepared") or []), 1)
            self.assertEqual((result.get("prepared") or [{}])[0].get("runRowId"), "row-1")
            self.assertEqual(len(result.get("created") or []), 1)
            self.assertEqual(len(result.get("skipped") or []), 1)
            mocked_apply.assert_called_once()
            apply_payload = mocked_apply.call_args.args[2]
            self.assertEqual(apply_payload.get("confirmed"), True)
            self.assertEqual(apply_payload.get("rowIds"), ["row-1"])
            self.assertEqual(apply_payload.get("accountCode"), "200")

        asyncio.run(_run())

    def test_mark_pi_credit_notes_failed_manual_updates_action_status(self):
        async def _run():
            user = {"id": "user-1"}
            with patch.object(
                services,
                "_pi_get_latest_action_approval_for_row",
                return_value={
                    "business_action_key": "key-1",
                    "payload_hash": "hash-1",
                    "source_snapshot_hash": "source-1",
                    "tenant_id": "tenant-1",
                },
            ), patch.object(services, "_pi_record_action_approval", return_value=None) as mocked_record, \
                 patch.object(services, "record_audit_event", return_value=None), \
                 patch.object(services, "pi_clearing_payload", return_value={"runs": []}):
                result = await services.mark_pi_clearing_credit_notes_failed_manual(
                    user,
                    "run-1",
                    {"rowIds": ["row-1"], "notes": "Manual review required"},
                )
            self.assertEqual(len(result.get("updated") or []), 1)
            self.assertEqual((result.get("updated") or [{}])[0].get("status"), services.PI_CLEARING_ACTION_STATUS_FAILED_MANUAL)
            mocked_record.assert_called_once()

        asyncio.run(_run())

    def test_call_stats_client_logs_payload_uses_month_start_without_type_error(self):
        fixed_now = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
        benchmark_row = {
            "total_calls": 0,
            "active_clients": 0,
            "inbound_calls": 0,
            "outbound_calls": 0,
        }
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "_call_stats_fetch_rows", return_value=[]), \
             patch.object(services, "_call_stats_practice_summary", return_value={"inboundCalls": 0, "outboundCalls": 0}), \
             patch.object(services, "get_connection", return_value=_Connection(row=benchmark_row)):
            payload = services.call_stats_client_logs_payload({"id": "user-1"}, "client-1", {})
        self.assertEqual(payload["clientId"], "client-1")
        self.assertEqual(payload["summary"]["callsThisMonth"], 0)
        self.assertEqual(payload["summary"]["callsLastMonth"], 0)

    def test_me_report_director_loan_account_data_uses_account_code_key(self):
        accounts = [
            {
                "accountCode": "DLA001",
                "accountName": "Director Loan Account",
                "accountType": "LIABILITY",
                "debitYTD": 0,
                "creditYTD": 0,
            }
        ]
        result = services._me_report_director_loan_account_data(accounts, {})
        self.assertEqual(result["accountCount"], 1)
        self.assertEqual(result["accounts"][0]["code"], "DLA001")

    def test_me_report_director_loan_account_code_for_client(self):
        mapping_rows = [
            {
                "account_code": "4550",
                "account_name": "Director Loan",
                "category": "Director Loan",
                "suggested_treatment": "",
                "confidence": 0.95,
            }
        ]
        with patch.object(services, "get_connection", return_value=_Connection(rows=mapping_rows)):
            code = services._me_report_director_loan_account_code_for_client("client-1")
        self.assertEqual(code, "4550")

    def test_payroll_overview_uses_latest_submitted_payrun_details_for_p32_estimate(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {"PayRunID": "draft-1", "PayRunStatus": "DRAFT", "PaymentDate": "2026-06-30"},
                        {"PayRunID": "submitted-1", "PayRunStatus": "POSTED", "PaymentDate": "2026-06-22"},
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-1",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "5000.00",
                        }
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-1",
                            "Totals": {"PayeAmount": "1000.00", "NicAmount": "250.00"},
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1250.0)
        self.assertEqual(payload["summary"]["outstandingTaxBalance"], 5000.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-1"), {})
        self.assertEqual(submitted.get("estimatedP32Tax"), 1250.0)

    def test_payroll_overview_prefers_journal_with_stronger_payroll_liability_lines(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pensions Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-1",
                            "Totals": {"PayeAmount": "3000.00", "NicAmount": "1151.67"},
                        }
                    ]
                }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-low",
                        "JournalDate": "2026-05-31",
                        "Reference": "Manual adjustment",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "4151.67", "Description": "Tax"},
                        ],
                    },
                    {
                        "JournalID": "jrnl-payroll",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "11676.94", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "1198.10", "Description": "Pension"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["journalPayableDiagnostics"].get("engine"), "disabled")

    def test_payroll_overview_uses_most_recent_payroll_journal(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-most-recent-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pensions Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-most-recent-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-most-recent-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-payroll-older",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "20000.00", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "3000.00", "Description": "Pension"},
                        ],
                    },
                    {
                        "JournalID": "jrnl-payroll-latest",
                        "JournalDate": "2026-06-01",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "1200.00", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "225.00", "Description": "Pension"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["journalPayableDiagnostics"].get("engine"), "disabled")

    def test_payroll_overview_prefers_journal_with_source_id_matching_selected_payrun(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-match-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pensions Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-match-1"):
                return {"PayRuns": [{"PayRunID": "submitted-match-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-other",
                        "SourceID": "another-payrun-id",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "2500.00", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "450.00", "Description": "Pension"},
                        ],
                    },
                    {
                        "JournalID": "jrnl-target",
                        "SourceID": "submitted-match-1",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "5000.00", "Description": "Tax"},
                            {"AccountID": "acc-858", "Credit": "800.00", "Description": "Pension"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["journalPayableDiagnostics"].get("engine"), "disabled")

    def test_payroll_overview_uses_trial_balance_delta_when_journal_lines_do_not_match(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-miss-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-miss-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-miss-1",
                            "Totals": {"PayeAmount": "3000.00", "NicAmount": "1151.67"},
                        }
                    ]
                }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                report_date = str((params or {}).get("date") or "")
                if report_date == "2026-05-31":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "PAYE Payable"}, {"Value": "10000.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "Pensions Payable"}, {"Value": "2200.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                if report_date == "2026-04-30":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "PAYE Payable"}, {"Value": "5000.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "Pensions Payable"}, {"Value": "1200.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-unmatched",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountCode": "477", "Credit": "52641.89", "Description": "Salaries"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["figureSources"]["p32Tax"], "payroll_api_payslips")
        self.assertEqual(payload["summary"]["figureSources"]["pensionPayable"], "none")

    def test_payroll_overview_uses_openai_inference_when_journal_and_trial_balance_are_not_usable(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-openai-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-openai-1"):
                return {"PayRuns": [{"PayRunID": "submitted-openai-1", "Totals": {"PayeAmount": "0.00", "NicAmount": "0.00"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {
                    "Reports": [
                        {
                            "Rows": [
                                {
                                    "RowType": "Section",
                                    "Rows": [
                                        {"RowType": "Row", "Cells": [{"Value": "Sales"}, {"Value": "100.00"}]},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-unmatched-openai",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountCode": "477", "Credit": "52641.89", "Description": "Salaries"},
                        ],
                    },
                ],
                "",
            )

        async def _fake_openai(*_args, **_kwargs):
            return (
                services.Decimal("6123.45"),
                services.Decimal("1188.22"),
                {"engine": "openai", "confidence": 0.92, "used": False, "notes": "Matched payroll context"},
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals), \
             patch.object(services, "_payroll_overview_openai_liability_inference", side_effect=_fake_openai):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 0.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["figureSources"]["p32Tax"], "none")
        self.assertEqual(payload["summary"]["figureSources"]["pensionPayable"], "none")

    def test_payroll_overview_uses_signed_journal_lines_when_account_metadata_missing(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-signed-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-signed-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-signed-1",
                            "Totals": {"PayeAmount": "3000.00", "NicAmount": "1151.67"},
                        }
                    ]
                }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-payroll-signed",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountCode": "825", "Description": "Tax", "NetAmount": "-11676.94"},
                            {"AccountCode": "858", "Description": "Pension", "NetAmount": "-1198.10"},
                            {"AccountCode": "477", "Description": "Earnings", "NetAmount": "52641.89"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        self.assertEqual(payload["summary"]["journalPayableDiagnostics"].get("engine"), "disabled")

    def test_payroll_overview_sums_credit_lines_for_tax_and_pension_liability_in_payroll_journal(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-net-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pensions Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-net-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-net-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-payroll-net",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "acc-825", "Credit": "11676.94", "Description": "Tax"},
                            {"AccountID": "acc-825", "Credit": "9150.77", "Description": "National Insurance Contribution"},
                            {"AccountID": "acc-825", "Credit": "1165.00", "Description": "Deductions"},
                            {"AccountID": "acc-825", "Debit": "3773.26", "Description": "Employment Allowance"},
                            {"AccountID": "acc-825", "Debit": "762.32", "Description": "Statutory Recovery - Maternity Pay"},
                            {"AccountID": "acc-858", "Credit": "1086.12", "Description": "Benefits"},
                            {"AccountID": "acc-858", "Credit": "1198.10", "Description": "Deductions"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_matches_liability_lines_when_account_code_formats_differ(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-codefmt-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825 - PAYE Payable",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858 - Pension Payable",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-codefmt-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-codefmt-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-code-format",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountCode": "825", "Description": "Tax", "Credit": "11676.94"},
                            {"AccountCode": "858", "Description": "Pension", "Credit": "1198.10"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_matches_snake_case_journal_line_fields(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-snake-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-snake-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-snake-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-snake-case",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"account_id": "acc-825", "line_description": "Tax", "credit_amount": "11676.94"},
                            {"account_code": "858", "line_description": "Pension", "credit_amount": "1198.10"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_matches_account_ids_when_journal_id_format_differs(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-journal-idfmt-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "A5574A89-1234-4444-9999-AAAAAAAAAAAA",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "B5574A89-1234-4444-9999-BBBBBBBBBBBB",
                            "Code": "858",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-journal-idfmt-1"):
                return {"PayRuns": [{"PayRunID": "submitted-journal-idfmt-1", "Totals": {"PayeAmount": "4151.67"}}]}
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        async def _fake_fetch_journals(_connection_row):
            return (
                [
                    {
                        "JournalID": "jrnl-id-format",
                        "JournalDate": "2026-05-31",
                        "Reference": "Payroll journal",
                        "JournalLines": [
                            {"AccountID": "{a5574a89-1234-4444-9999-aaaaaaaaaaaa}", "Description": "Tax", "Credit": "11676.94"},
                            {"AccountID": "{b5574a89-1234-4444-9999-bbbbbbbbbbbb}", "Description": "Pension", "Credit": "1198.10"},
                        ],
                    },
                ],
                "",
            )

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get), \
             patch.object(services, "_code_breaker_fetch_xero_journals", side_effect=_fake_fetch_journals):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 4151.67)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_does_not_call_payslip_detail_fallback(self):
        payslip_calls = {"count": 0}

        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {"PayRunID": "submitted-slow-1", "PayRunStatus": "POSTED", "PaymentDate": "2026-06-22"},
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-slow-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-slow-1",
                            "Payslips": [{"PayslipID": "ps-1"}, {"PayslipID": "ps-2"}, {"PayslipID": "ps-3"}],
                        }
                    ]
                }
            if url.startswith("https://api.xero.com/payroll.xro/2.0/PaySlips/"):
                payslip_calls["count"] += 1
                raise Exception("Xero permissions need updating. Reconnect Xero to approve reports and journals scopes.")
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payslip_calls["count"], 0)

    def test_payroll_overview_sums_pension_from_submitted_payrun_payslips(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {"PayRunID": "submitted-2", "PayRunStatus": "POSTED", "PaymentDate": "2026-06-22"},
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-2"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-2",
                            "Payslips": [
                                {"EmployerPensionContribution": "120.50"},
                                {"EmployerPensionContribution": "79.50"},
                            ],
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["pensionPayableBalance"], 200.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-2"), {})
        self.assertEqual(submitted.get("estimatedPensionPayable"), 200.0)

    def test_payroll_overview_supports_lower_camel_payrun_keys(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "payRunId": "submitted-lc-1",
                            "payRunStatus": "POSTED",
                            "payRunPeriodEndDate": "2026-06-22",
                            "wages": "3999.55",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-lc-1"):
                return {
                    "PayRuns": [
                        {
                            "payRunId": "submitted-lc-1",
                            "Totals": {"PayeAmount": "1100.00", "NicAmount": "300.00"},
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1400.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-lc-1"), {})
        self.assertEqual(submitted.get("estimatedP32Tax"), 1400.0)
        self.assertEqual(submitted.get("status"), "POSTED")
        self.assertEqual(submitted.get("wages"), 3999.55)

    def test_payroll_overview_supports_lower_camel_plural_payruns_key(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "payRuns": [
                        {
                            "payRunId": "submitted-lc-plural-1",
                            "payRunStatus": "POSTED",
                            "paymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-lc-plural-1"):
                return {
                    "payRuns": [
                        {
                            "payRunId": "submitted-lc-plural-1",
                            "totals": {"payeAmount": "900.00", "nicAmount": "200.00"},
                            "payslips": [
                                {"employerPensionContribution": "60.00"},
                                {"employerPensionContribution": "40.00"},
                            ],
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1100.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 100.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-lc-plural-1"), {})
        self.assertEqual(submitted.get("estimatedP32Tax"), 1100.0)
        self.assertEqual(submitted.get("estimatedPensionPayable"), 100.0)

    def test_payroll_overview_supports_lower_camel_account_balance_keys(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {"PayRuns": []}
            if url == services.ACCOUNTS_URL:
                return {
                    "accounts": [
                        {
                            "accountId": "tax-1",
                            "code": "825",
                            "name": "PAYE Payable",
                            "type": "CURRLIAB",
                            "class": "LIABILITY",
                            "currentBalance": "1234.56",
                        },
                        {
                            "accountId": "pen-1",
                            "code": "826",
                            "name": "Pension Payable",
                            "type": "CURRLIAB",
                            "class": "LIABILITY",
                            "currentBalance": "210.10",
                        },
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["outstandingTaxBalance"], 1234.56)
        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 0.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)

    def test_payroll_overview_reads_described_tax_and_pension_line_amounts(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-desc-1",
                            "PayRunStatus": "POSTED",
                            "PaymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-desc-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-desc-1",
                            "PayItems": {
                                "TaxItems": [
                                    {"Description": "PAYE", "Amount": "725.00"},
                                    {"Description": "Employee NIC", "Amount": "275.00"},
                                ],
                                "DeductionItems": [
                                    {"Description": "Workplace Pension Employer", "Amount": "180.25"},
                                ],
                            },
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 1000.0)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 180.25)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-desc-1"), {})
        self.assertEqual(submitted.get("estimatedP32Tax"), 1000.0)
        self.assertEqual(submitted.get("estimatedPensionPayable"), 180.25)

    def test_payroll_overview_sums_superannuation_style_pension_keys(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-super-1",
                            "PayRunStatus": "POSTED",
                            "PaymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-super-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-super-1",
                            "Payslips": [
                                {"EmployerSuperannuation": "95.50"},
                                {"KiwiSaverEmployerContribution": "44.50"},
                            ],
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["pensionPayableBalance"], 140.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-super-1"), {})
        self.assertEqual(submitted.get("estimatedPensionPayable"), 140.0)

    def test_payroll_overview_does_not_fall_back_to_payslip_detail_for_pension(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-ps-1",
                            "PayRunStatus": "POSTED",
                            "PaymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-ps-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-ps-1",
                            "Payslips": [
                                {"PayslipID": "ps-1"},
                                {"PayslipID": "ps-2"},
                            ],
                        }
                    ]
                }
            if url == services.XERO_PAYROLL_PAYSLIP_DETAILS_URL.format(payslip_id="ps-1"):
                return {"Payslips": [{"PayslipID": "ps-1", "EmployerPensionContribution": "90.00"}]}
            if url == services.XERO_PAYROLL_PAYSLIP_DETAILS_URL.format(payslip_id="ps-2"):
                return {"Payslips": [{"PayslipID": "ps-2", "EmployerPensionContribution": "60.00"}]}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-ps-1"), {})
        self.assertEqual(submitted.get("estimatedPensionPayable"), 0.0)

    def test_payroll_overview_does_not_sum_employee_and_employer_from_payslip_detail_calls(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-ps-2",
                            "PayRunStatus": "POSTED",
                            "PaymentDate": "2026-06-22",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {"Accounts": []}
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-ps-2"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-ps-2",
                            "Payslips": [
                                {"PayslipID": "ps-3"},
                            ],
                        }
                    ]
                }
            if url == services.XERO_PAYROLL_PAYSLIP_DETAILS_URL.format(payslip_id="ps-3"):
                return {
                    "Payslips": [
                        {
                            "PayslipID": "ps-3",
                            "EmployerPensionContribution": "90.00",
                            "EmployeePensionContribution": "55.00",
                        }
                    ]
                }
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["pensionPayableBalance"], 0.0)
        submitted = next((row for row in payload["payRuns"] if row.get("payRunId") == "submitted-ps-2"), {})
        self.assertEqual(submitted.get("estimatedPensionPayable"), 0.0)

    def test_payroll_overview_prefers_nominal_trial_balance_delta_over_payroll_api(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-nominal-delta-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-nominal-delta-1"):
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-nominal-delta-1",
                            "Totals": {
                                "PayeAmount": "7002.10",
                                "PensionPayable": "2096.72",
                            },
                        }
                    ]
                }
            if url == services.XERO_PAYROLL_PAYSLIPS_BY_PAYRUN_URL:
                return {
                    "PaySlips": [
                        {"Tax": "7002.10", "EmployerPensionContribution": "2096.72"},
                    ]
                }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                report_date = str((params or {}).get("date") or "")
                if report_date == "2026-05-31":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "825 PAYE Payable"}, {"Value": "17457.13"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "858 Pension Payable"}, {"Value": "1972.69"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                if report_date == "2026-04-30":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "825 PAYE Payable"}, {"Value": "0.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "858 Pension Payable"}, {"Value": "0.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 7002.1)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 2096.72)
        self.assertEqual(payload["summary"]["figureSources"]["p32Tax"], "payroll_api_payslips")
        self.assertEqual(payload["summary"]["figureSources"]["pensionPayable"], "payroll_api_payslips")
        self.assertEqual(payload["summary"]["trialBalanceDeltaP32Tax"], 17457.13)
        self.assertEqual(payload["summary"]["trialBalanceDeltaPensionPayable"], 1972.69)

    def test_payroll_overview_prefers_nominal_account_transactions_over_trial_balance_delta(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-nominal-tx-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-nominal-tx-1"):
                return {"PayRuns": [{"PayRunID": "submitted-nominal-tx-1", "Totals": {"PayeAmount": "7002.10", "PensionPayable": "2096.72"}}]}
            if url == services.XERO_PAYROLL_PAYSLIPS_BY_PAYRUN_URL:
                return {"PaySlips": [{"Tax": "7002.10", "EmployerPensionContribution": "2096.72"}]}
            if url == services.XERO_REPORTS_ACCOUNT_TRANSACTIONS_URL:
                account_id = str((params or {}).get("accountID") or "")
                if account_id == "acc-825":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "PAYE Payable"}]},
                                            {
                                                "RowType": "Header",
                                                "Cells": [
                                                    {"Value": "Date"},
                                                    {"Value": "Source"},
                                                    {"Value": "Description"},
                                                    {"Value": "Reference"},
                                                    {"Value": "Debit"},
                                                    {"Value": "Credit"},
                                                ],
                                            },
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Tax"}, {"Value": "Payroll Expense Journal - PD-79"}, {"Value": ""}, {"Value": "11,676.94"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Deductions"}, {"Value": "Payroll Expense Journal - PD-79"}, {"Value": ""}, {"Value": "1,165.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Employment Allowance"}, {"Value": "Payroll Expense Journal - PD-79"}, {"Value": "3,773.26"}, {"Value": ""}]},
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "National Insurance Contribution"}, {"Value": "Payroll Expense Journal - PD-79"}, {"Value": ""}, {"Value": "9,150.77"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Statutory Recovery - Maternity Pay"}, {"Value": "Payroll Expense Journal - PD-79"}, {"Value": "762.32"}, {"Value": ""}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                if account_id == "acc-858":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "Pensions Payable"}]},
                                            {
                                                "RowType": "Header",
                                                "Cells": [
                                                    {"Value": "Date"},
                                                    {"Value": "Source"},
                                                    {"Value": "Description"},
                                                    {"Value": "Reference"},
                                                    {"Value": "Debit"},
                                                    {"Value": "Credit"},
                                                ],
                                            },
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Benefits"}, {"Value": "Payroll Expense Journal - PD-79"}, {"Value": ""}, {"Value": "1,086.12"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Deductions"}, {"Value": "Payroll Expense Journal - PD-79"}, {"Value": ""}, {"Value": "1,198.10"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "8 May 2026"}, {"Value": "Spend Money"}, {"Value": "NEST - pension contributions"}, {"Value": ""}, {"Value": "2,012.99"}, {"Value": ""}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                report_date = str((params or {}).get("date") or "")
                if report_date == "2026-05-31":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "825 PAYE Payable"}, {"Value": "25000.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "858 Pension Payable"}, {"Value": "4000.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                if report_date == "2026-04-30":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "825 PAYE Payable"}, {"Value": "0.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "858 Pension Payable"}, {"Value": "0.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 17457.13)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 2284.22)
        self.assertEqual(payload["summary"]["figureSources"]["p32Tax"], "nominal_account_transactions")
        self.assertEqual(payload["summary"]["figureSources"]["pensionPayable"], "nominal_account_transactions")

    def test_payroll_overview_matches_pension_nominal_by_payrun_date_when_reference_differs(self):
        async def _fake_xero_api_get(_connection_row, url, params=None, on_response=None):
            if callable(on_response):
                on_response({"status_code": 200, "elapsed_ms": 5, "rate_limit_headers": {}})
            if url == services.XERO_PAYROLL_EMPLOYEES_URL:
                return {"Employees": []}
            if url == services.XERO_PAYROLL_PAYRUNS_URL:
                return {
                    "PayRuns": [
                        {
                            "PayRunID": "submitted-nominal-date-1",
                            "PayRunStatus": "POSTED",
                            "PayRunPeriodStartDate": "2026-05-01",
                            "PayRunPeriodEndDate": "2026-05-31",
                            "PaymentDate": "2026-05-31",
                        },
                    ]
                }
            if url == services.ACCOUNTS_URL:
                return {
                    "Accounts": [
                        {
                            "AccountID": "acc-825",
                            "Code": "825",
                            "Name": "PAYE Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                        {
                            "AccountID": "acc-858",
                            "Code": "858",
                            "Name": "Pension Payable",
                            "Type": "CURRLIAB",
                            "Class": "LIABILITY",
                            "CurrentBalance": "0.00",
                        },
                    ]
                }
            if url == services.XERO_PAYROLL_PAYRUN_DETAILS_URL.format(payrun_id="submitted-nominal-date-1"):
                return {"PayRuns": [{"PayRunID": "submitted-nominal-date-1", "Totals": {"PayeAmount": "0.00", "PensionPayable": "0.00"}}]}
            if url == services.XERO_PAYROLL_PAYSLIPS_BY_PAYRUN_URL:
                return {"PaySlips": []}
            if url == services.XERO_REPORTS_ACCOUNT_TRANSACTIONS_URL:
                account_id = str((params or {}).get("accountID") or "")
                if account_id == "acc-825":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "PAYE Payable"}]},
                                            {
                                                "RowType": "Header",
                                                "Cells": [
                                                    {"Value": "Date"},
                                                    {"Value": "Source"},
                                                    {"Value": "Description"},
                                                    {"Value": "Reference"},
                                                    {"Value": "Debit"},
                                                    {"Value": "Credit"},
                                                ],
                                            },
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Tax"}, {"Value": "Payroll Expense Journal - PD-79"}, {"Value": ""}, {"Value": "7002.10"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                if account_id == "acc-858":
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "Pensions Payable"}]},
                                            {
                                                "RowType": "Header",
                                                "Cells": [
                                                    {"Value": "Date"},
                                                    {"Value": "Source"},
                                                    {"Value": "Description"},
                                                    {"Value": "Reference"},
                                                    {"Value": "Debit"},
                                                    {"Value": "Credit"},
                                                ],
                                            },
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Benefits"}, {"Value": "Payroll Expense Journal - PD-80"}, {"Value": ""}, {"Value": "898.62"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "31 May 2026"}, {"Value": "Payroll Expense"}, {"Value": "Deductions"}, {"Value": "Payroll Expense Journal - PD-80"}, {"Value": ""}, {"Value": "1198.10"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
            if url == services.XERO_REPORTS_TRIAL_BALANCE_URL:
                report_date = str((params or {}).get("date") or "")
                if report_date in {"2026-05-31", "2026-04-30"}:
                    return {
                        "Reports": [
                            {
                                "Rows": [
                                    {
                                        "RowType": "Section",
                                        "Rows": [
                                            {"RowType": "Row", "Cells": [{"Value": "825 PAYE Payable"}, {"Value": "0.00"}]},
                                            {"RowType": "Row", "Cells": [{"Value": "858 Pension Payable"}, {"Value": "0.00"}]},
                                        ],
                                    }
                                ]
                            }
                        ]
                    }
                return {}
            raise AssertionError(f"Unexpected URL: {url} params={params}")

        fixed_now = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
        with patch.object(services, "utcnow", return_value=fixed_now), \
             patch.object(services, "xero_connection_for_user_tenant", return_value={"tenant_id": "tenant-1"}), \
             patch.object(services, "xero_api_get", side_effect=_fake_xero_api_get):
            payload = asyncio.run(services.payroll_tenant_overview_payload({"id": "user-1"}, "tenant-1"))

        self.assertEqual(payload["summary"]["estimatedP32TaxBalance"], 7002.1)
        self.assertEqual(payload["summary"]["pensionPayableBalance"], 2096.72)
        self.assertEqual(payload["summary"]["figureSources"]["p32Tax"], "nominal_account_transactions")
        self.assertEqual(payload["summary"]["figureSources"]["pensionPayable"], "nominal_account_transactions")
        pension_rows = payload["summary"]["nominalTransactionDiagnostics"].get("pensionAccountTransactions") or []
        self.assertTrue(pension_rows)
        self.assertEqual(pension_rows[0].get("applicableLineCount"), 2)

    def test_delete_pi_clearing_run_allows_locked_month_and_cleans_local_records(self):
        class _SelectCursor:
            def __init__(self):
                self._fetched = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, _query, _params=None):
                return None

            def fetchone(self):
                if self._fetched:
                    return {}
                self._fetched = True
                return {
                    "id": "run-2",
                    "user_id": "user-1",
                    "tenant_id": "tenant-1",
                    "month_start": date(2026, 2, 1),
                    "month_end": date(2026, 2, 28),
                }

        class _DeleteCursor:
            def __init__(self):
                self.executed = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, params=None):
                self.executed.append((str(query or ""), params))

            def fetchone(self):
                return {}

        class _Conn:
            def __init__(self, cursor_obj):
                self._cursor_obj = cursor_obj

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self._cursor_obj

            def commit(self):
                return None

        select_cursor = _SelectCursor()
        delete_cursor = _DeleteCursor()
        conn_sequence = [_Conn(select_cursor), _Conn(delete_cursor)]

        def _next_conn():
            return conn_sequence.pop(0)

        with patch.object(services, "get_connection", side_effect=_next_conn), \
             patch.object(services, "pi_clearing_payload", return_value={"runs": []}), \
             patch.object(services, "_pi_assert_requested_open_month", side_effect=AssertionError("gate should not run on delete")):
            result = asyncio.run(services.delete_pi_clearing_run({"id": "user-1"}, "run-2"))

        self.assertEqual(result.get("deletedRunId"), "run-2")
        self.assertEqual(result.get("runs"), [])
        all_queries = "\n".join(item[0] for item in delete_cursor.executed)
        self.assertIn("DELETE FROM pi_clearing_action_approvals", all_queries)
        self.assertIn("DELETE FROM pi_clearing_credit_notes", all_queries)
        self.assertIn("DELETE FROM pi_clearing_run_rows", all_queries)
        self.assertIn("DELETE FROM pi_clearing_runs", all_queries)

    def test_jays_stats2_table_migration_adds_columns_before_partial_unique_index(self):
        class _Cursor:
            def __init__(self):
                self.executed: list[str] = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, query, _params=None):
                self.executed.append(str(query or ""))

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        class _Conn:
            def __init__(self):
                self._cursor = _Cursor()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return self._cursor

            def commit(self):
                return None

        recorder = _Conn()
        with patch.object(services, "get_connection", return_value=recorder):
            services._ensure_jays_stats2_tables()

        queries = recorder._cursor.executed
        is_voided_alter_index = next(
            index for index, query in enumerate(queries)
            if "ADD COLUMN IF NOT EXISTS is_voided" in query
        )
        dedupe_update_index = next(
            index for index, query in enumerate(queries)
            if "WITH ranked_duplicates AS" in query
        )
        unique_index_create = next(
            index for index, query in enumerate(queries)
            if "CREATE UNIQUE INDEX IF NOT EXISTS idx_jays_stats2_tx_dedupe_active" in query
        )
        self.assertLess(is_voided_alter_index, unique_index_create)
        self.assertLess(dedupe_update_index, unique_index_create)

    def test_jays_stats2_pdf_extraction_prefers_openai(self):
        async def _run():
            messages: list[str] = []
            with patch.object(
                services,
                "_extract_bank_statement_pdf",
                return_value={
                    "transactions": [
                        {
                            "date": "2026-08-31",
                            "description": "OPENAI",
                            "amount": -5.47,
                            "balance": 296.81,
                        }
                    ]
                },
            ), patch.object(services, "_jays_stats_parse_pdf_transactions_local") as local_parser:
                rows = await services._jays_stats2_extract_pdf_rows(b"pdf", "statement.pdf", messages)
                local_parser.assert_not_called()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].get("source"), "pdf-openai")
            self.assertTrue(any("used OpenAI extraction" in item for item in messages))

        asyncio.run(_run())

    def test_jays_stats2_pdf_extraction_falls_back_to_local_when_openai_unavailable(self):
        async def _run():
            messages: list[str] = []
            with patch.object(
                services,
                "_extract_bank_statement_pdf",
                side_effect=services.HTTPException(status_code=400, detail="OpenAI extraction is not configured."),
            ), patch.object(
                services,
                "_jays_stats_parse_pdf_transactions_local",
                return_value=[{
                    "date": "2026-08-31",
                    "description": "Debit OPENAI",
                    "amount": -5.47,
                    "runningBalance": 296.81,
                    "source": "pdf-local",
                }],
            ):
                rows = await services._jays_stats2_extract_pdf_rows(b"pdf", "statement.pdf", messages)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].get("source"), "pdf-local")
            self.assertTrue(any("OpenAI extraction unavailable" in item for item in messages))
            self.assertTrue(any("used local PDF parser fallback" in item for item in messages))

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
