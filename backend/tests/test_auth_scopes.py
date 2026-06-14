import os
import unittest

os.environ.setdefault("PORT", "8000")
os.environ.setdefault("BASE_URL", "https://example.com")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@example.com:5432/credit_control")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault("WIDGET_TOKEN", "test-widget-token")
os.environ.setdefault("XERO_CLIENT_ID", "test-xero-client-id")
os.environ.setdefault("XERO_CLIENT_SECRET", "test-xero-client-secret")
os.environ.setdefault("XERO_REDIRECT_URI", "https://example.com/xero/callback")

try:
    from app.auth import configured_xero_scopes_include_payroll, xero_scope_string, xero_scope_string_all_available

    _AUTH_TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    xero_scope_string = None  # type: ignore[assignment]
    xero_scope_string_all_available = None  # type: ignore[assignment]
    configured_xero_scopes_include_payroll = None  # type: ignore[assignment]
    _AUTH_TEST_IMPORT_ERROR = str(exc)


@unittest.skipIf(xero_scope_string is None, f"Auth scope tests skipped: {_AUTH_TEST_IMPORT_ERROR}")
class XeroScopeStringTests(unittest.TestCase):
    def test_detects_payroll_scopes_in_config(self):
        self.assertTrue(
            configured_xero_scopes_include_payroll(
                "openid profile email offline_access payroll.employees payroll.payruns"
            )
        )
        self.assertFalse(
            configured_xero_scopes_include_payroll(
                "openid profile email offline_access accounting.invoices accounting.payments"
            )
        )

    def test_filters_malformed_or_unknown_tokens(self):
        configured = "openid profile email offline_access payroll.e\nmployees payroll.payruns,accounting.invoices,scope=accounting.payments Xero"
        scopes = xero_scope_string(configured, include_payroll_scopes=True).split()

        self.assertNotIn("payroll.e", scopes)
        self.assertNotIn("mployees", scopes)
        self.assertNotIn("xero", scopes)
        self.assertIn("payroll.payruns", scopes)
        self.assertIn("accounting.invoices", scopes)
        self.assertIn("accounting.payments", scopes)

    def test_removes_payroll_scopes_when_disabled(self):
        configured = (
            "openid profile email offline_access "
            "payroll.employees payroll.payruns payroll.settings payroll.payslip"
        )
        scopes = xero_scope_string(configured, include_payroll_scopes=False).split()

        self.assertNotIn("payroll.employees", scopes)
        self.assertNotIn("payroll.payruns", scopes)
        self.assertNotIn("payroll.settings", scopes)
        self.assertNotIn("payroll.payslip", scopes)

    def test_preserves_read_only_payroll_scopes_when_enabled(self):
        configured = "openid profile email offline_access payroll.employees.read payroll.payruns.read"
        scopes = xero_scope_string(configured, include_payroll_scopes=True).split()

        self.assertIn("payroll.employees.read", scopes)
        self.assertIn("payroll.payruns.read", scopes)
        self.assertNotIn("payroll.employees", scopes)
        self.assertNotIn("payroll.payruns", scopes)

    def test_adds_default_read_only_payroll_scopes_when_enabled(self):
        configured = "openid profile email offline_access accounting.invoices accounting.payments"
        scopes = xero_scope_string(configured, include_payroll_scopes=True).split()

        self.assertIn("payroll.employees.read", scopes)
        self.assertIn("payroll.payruns.read", scopes)
        self.assertIn("accounting.invoices", scopes)
        self.assertIn("accounting.payments", scopes)

    def test_does_not_force_default_accounting_scopes_when_only_payroll_configured(self):
        configured = "openid profile email offline_access payroll.employees payroll.payruns"
        scopes = xero_scope_string(configured, include_payroll_scopes=True).split()

        self.assertNotIn("accounting.invoices", scopes)
        self.assertNotIn("accounting.reports.balancesheet.read", scopes)
        self.assertNotIn("accounting.reports.banksummary.read", scopes)
        self.assertNotIn("assets.read", scopes)
        self.assertIn("payroll.employees", scopes)
        self.assertIn("payroll.payruns", scopes)

    def test_falls_back_to_default_accounting_scopes_when_config_is_empty(self):
        scopes = xero_scope_string("", include_payroll_scopes=False).split()

        self.assertIn("accounting.invoices", scopes)
        self.assertIn("accounting.reports.balancesheet.read", scopes)
        self.assertIn("accounting.reports.banksummary.read", scopes)
        self.assertIn("assets.read", scopes)

    def test_expands_legacy_transactions_scope(self):
        scopes = xero_scope_string("accounting.transactions").split()

        self.assertIn("accounting.invoices", scopes)
        self.assertIn("accounting.payments", scopes)
        self.assertIn("accounting.banktransactions", scopes)
        self.assertIn("accounting.manualjournals", scopes)

    def test_all_available_scope_string_uses_configured_set_plus_payroll_read(self):
        configured = "openid profile email offline_access accounting.invoices accounting.payments accounting.reports.balancesheet.read"
        scopes = xero_scope_string_all_available(configured).split()

        self.assertIn("openid", scopes)
        self.assertIn("offline_access", scopes)
        self.assertIn("accounting.invoices", scopes)
        self.assertIn("accounting.reports.balancesheet.read", scopes)
        self.assertIn("payroll.employees.read", scopes)
        self.assertIn("payroll.payruns.read", scopes)
        self.assertNotIn("files.read", scopes)
        self.assertNotIn("projects.read", scopes)


if __name__ == "__main__":
    unittest.main()
