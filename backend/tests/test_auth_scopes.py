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
    from app.auth import xero_scope_string

    _AUTH_TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    xero_scope_string = None  # type: ignore[assignment]
    _AUTH_TEST_IMPORT_ERROR = str(exc)


@unittest.skipIf(xero_scope_string is None, f"Auth scope tests skipped: {_AUTH_TEST_IMPORT_ERROR}")
class XeroScopeStringTests(unittest.TestCase):
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
        configured = "openid profile email offline_access payroll.employees payroll.payruns"
        scopes = xero_scope_string(configured, include_payroll_scopes=False).split()

        self.assertNotIn("payroll.employees", scopes)
        self.assertNotIn("payroll.payruns", scopes)

    def test_expands_legacy_transactions_scope(self):
        scopes = xero_scope_string("accounting.transactions").split()

        self.assertIn("accounting.invoices", scopes)
        self.assertIn("accounting.payments", scopes)
        self.assertIn("accounting.banktransactions", scopes)
        self.assertIn("accounting.manualjournals", scopes)


if __name__ == "__main__":
    unittest.main()
