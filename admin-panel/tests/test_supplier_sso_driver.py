import sys
import tempfile
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

from supplier_sso import (  # noqa: E402
    LingSuanCredentialCapture,
    build_system_browser_command,
    browser_runtime_status,
    build_supplier_cookie_header,
    extract_bearer_token,
    find_system_browser,
    is_google_signin_rejected_url,
    is_lingsuan_auth_me_url,
)


class SupplierSsoDriverTests(unittest.TestCase):
    def test_accepts_only_exact_https_lingsuan_auth_me_url(self):
        self.assertTrue(is_lingsuan_auth_me_url("https://lingsuan.top/api/v1/auth/me"))
        self.assertTrue(is_lingsuan_auth_me_url("https://lingsuan.top/api/v1/auth/me?timezone=Asia%2FShanghai"))
        for url in (
            "http://lingsuan.top/api/v1/auth/me",
            "https://evil.example/api/v1/auth/me",
            "https://lingsuan.top.evil.example/api/v1/auth/me",
            "https://accounts.google.com/api/v1/auth/me",
            "https://lingsuan.top/api/v1/auth/login",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_lingsuan_auth_me_url(url))

    def test_extracts_case_insensitive_bearer_token_without_accepting_other_schemes(self):
        self.assertEqual(extract_bearer_token({"Authorization": "Bearer supplier-token"}), "supplier-token")
        self.assertEqual(extract_bearer_token({"authorization": "bearer second-token"}), "second-token")
        self.assertEqual(extract_bearer_token({"authorization": "Basic abc"}), "")
        self.assertEqual(extract_bearer_token({}), "")

    def test_cookie_header_keeps_only_supplier_domain(self):
        cookies = [
            {"name": "session", "value": "supplier-session", "domain": ".lingsuan.top"},
            {"name": "csrf", "value": "supplier-csrf", "domain": "lingsuan.top"},
            {"name": "google", "value": "google-secret", "domain": ".google.com"},
            {"name": "other", "value": "other-secret", "domain": "example.com"},
        ]

        header = build_supplier_cookie_header(cookies, "lingsuan.top")

        self.assertEqual(header, "session=supplier-session; csrf=supplier-csrf")
        self.assertNotIn("google-secret", header)
        self.assertNotIn("other-secret", header)

    def test_capture_requires_successful_same_origin_auth_response(self):
        capture = LingSuanCredentialCapture()

        capture.observe_response(
            "https://accounts.google.com/api/v1/auth/me",
            200,
            {"authorization": "Bearer google-token"},
        )
        capture.observe_response(
            "https://lingsuan.top/api/v1/auth/me",
            401,
            {"authorization": "Bearer expired-token"},
        )
        self.assertFalse(capture.authenticated)

        capture.observe_response(
            "https://lingsuan.top/api/v1/auth/me?timezone=Asia%2FShanghai",
            200,
            {"authorization": "Bearer supplier-token"},
        )

        self.assertTrue(capture.authenticated)
        self.assertEqual(capture.credentials([{"name": "session", "value": "abc", "domain": ".lingsuan.top"}]), {
            "auth_token": "supplier-token",
            "session_cookie": "session=abc",
        })
        self.assertNotIn("supplier-token", repr(capture))

    def test_browser_runtime_status_is_safe_when_playwright_is_missing(self):
        status = browser_runtime_status()

        self.assertIn("available", status)
        self.assertIn("package_installed", status)
        self.assertIn("install_command", status)
        self.assertNotIn("exception", status)

    def test_finds_supported_system_browser_in_candidate_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            edge = root / "msedge.exe"
            chrome = root / "chrome.exe"
            edge.write_bytes(b"edge")
            chrome.write_bytes(b"chrome")

            browser = find_system_browser([
                ("Microsoft Edge", edge),
                ("Google Chrome", chrome),
            ])

        self.assertEqual(browser["name"], "Microsoft Edge")
        self.assertEqual(Path(browser["executable_path"]), edge)

    def test_system_browser_login_command_is_visible_and_not_automated(self):
        command = build_system_browser_command(
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"E:\relaydeck\supplier-profile"),
            "https://lingsuan.top/dashboard",
        )

        self.assertEqual(command[0], r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        self.assertIn(r"--user-data-dir=E:\relaydeck\supplier-profile", command)
        self.assertIn("--new-window", command)
        self.assertEqual(command[-1], "https://lingsuan.top/dashboard")
        self.assertFalse(any("remote-debugging" in item for item in command))
        self.assertFalse(any("automation" in item for item in command))

    def test_recognizes_google_unsupported_browser_rejection_page(self):
        self.assertTrue(is_google_signin_rejected_url(
            "https://accounts.google.com/v3/signin/rejected?app_domain=https%3A%2F%2Flingsuan.top"
        ))
        self.assertFalse(is_google_signin_rejected_url("https://accounts.google.com/o/oauth2/auth"))
        self.assertFalse(is_google_signin_rejected_url("https://lingsuan.top/dashboard"))


if __name__ == "__main__":
    unittest.main()
