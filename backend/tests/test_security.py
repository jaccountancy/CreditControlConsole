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
    from app import security
    _SECURITY_TEST_IMPORT_ERROR = ""
except ModuleNotFoundError as exc:  # pragma: no cover - local runtime guard
    security = None  # type: ignore[assignment]
    _SECURITY_TEST_IMPORT_ERROR = str(exc)


@unittest.skipIf(security is None, f"Security tests skipped: {_SECURITY_TEST_IMPORT_ERROR}")
class SecurityTests(unittest.TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        value = "top-secret-value"
        label = "ch:test"
        encrypted = security.encrypt_secret(value, label)
        decrypted = security.decrypt_secret(encrypted, label)
        self.assertEqual(decrypted, value)

    def test_legacy_ciphertext_still_decrypts(self):
        value = "legacy-secret"
        label = "legacy:label"
        legacy_encrypted = security._legacy_encrypt_secret(value, label)
        decrypted = security.decrypt_secret(legacy_encrypted, label)
        self.assertEqual(decrypted, value)


if __name__ == "__main__":
    unittest.main()
