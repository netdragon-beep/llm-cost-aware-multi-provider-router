import sys
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

from supplier_sso import (  # noqa: E402
    BrowserCredentialCapture,
    browser_sso_adapter_for_supplier,
    is_browser_sso_auth_me_url,
)


class SupplierSsoAdapterTests(unittest.TestCase):
    def test_builtin_lingsuan_and_autocode_adapters_share_the_contract(self):
        lingsuan = browser_sso_adapter_for_supplier("lingsuan.top", "lingsuan-web")
        autocode = browser_sso_adapter_for_supplier("auto-code.net", "autocode-web")

        self.assertEqual(lingsuan["auth_me_path"], "/api/v1/auth/me")
        self.assertEqual(autocode["auth_me_path"], "/api/v1/auth/me")
        self.assertIn("lingsuan.top", lingsuan["cookie_domains"])
        self.assertIn("auto-code.net", autocode["cookie_domains"])

    def test_auth_me_capture_accepts_only_the_supplier_origin(self):
        adapter = browser_sso_adapter_for_supplier("auto-code.net", "autocode-web")
        capture = BrowserCredentialCapture(adapter)

        self.assertTrue(is_browser_sso_auth_me_url("https://auto-code.net/api/v1/auth/me?tz=Asia%2FShanghai", adapter))
        self.assertFalse(is_browser_sso_auth_me_url("https://lingsuan.top/api/v1/auth/me", adapter))
        capture.observe_response(
            "https://auto-code.net/api/v1/auth/me",
            200,
            {"Authorization": "Bearer auto-code-token"},
        )
        self.assertTrue(capture.authenticated)
        credentials = capture.credentials(
            [
                {"domain": "auto-code.net", "name": "session", "value": "session-value"},
                {"domain": "lingsuan.top", "name": "foreign", "value": "must-not-capture"},
            ]
        )
        self.assertEqual(credentials["auth_token"], "auto-code-token")
        self.assertEqual(credentials["session_cookie"], "session=session-value")

    def test_capture_does_not_authenticate_on_failed_auth_me_response(self):
        adapter = browser_sso_adapter_for_supplier("auto-code.net", "autocode-web")
        capture = BrowserCredentialCapture(adapter)
        capture.observe_response(
            "https://auto-code.net/api/v1/auth/me",
            401,
            {"Authorization": "Bearer expired"},
        )
        self.assertFalse(capture.authenticated)


if __name__ == "__main__":
    unittest.main()
