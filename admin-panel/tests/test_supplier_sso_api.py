import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

import app  # noqa: E402
from fastapi import HTTPException  # noqa: E402


LING_SUAN_STATE = {
    "suppliers": [{"id": "supplier-lingsuan", "name": "LingSuan"}],
    "api_profiles": [
        {
            "id": "api-lingsuan",
            "supplier_id": "supplier-lingsuan",
            "api_base": "https://lingsuan.top/v1",
            "enabled": True,
        }
    ],
    "supplier_quotas": {
        "lingsuan.top": {
            "enabled": True,
            "adapter": "lingsuan-web",
            "portal_base": "https://lingsuan.top",
        }
    },
}


class FakeVault:
    def __init__(self):
        self.saved = {}

    def merge(self, supplier_key, credentials):
        self.saved[supplier_key] = dict(credentials)
        return {"configured": True, "configured_fields": sorted(credentials)}

    def status(self, supplier_key):
        return {"configured": False, "configured_fields": [], "supplier_key": supplier_key}


class FakeManager:
    def __init__(self):
        self.started = []
        self.cancelled = []
        self.completed = []
        self.cleared = []

    def start(self, supplier_key, portal_base, *, interactive=True, timeout_seconds=600, adapter=None):
        self.started.append((supplier_key, portal_base, interactive, timeout_seconds, adapter or {}))
        return {"supplier_key": supplier_key, "status": "starting", "requires_interaction": False}

    def status(self, supplier_key):
        return {"supplier_key": supplier_key, "status": "idle", "requires_interaction": False}

    def cancel(self, supplier_key):
        self.cancelled.append(supplier_key)
        return True

    def complete_interactive(self, supplier_key):
        self.completed.append(supplier_key)
        return True

    def clear_session(self, supplier_key):
        self.cleared.append(supplier_key)
        return True


class SupplierSsoApiTests(unittest.TestCase):
    def test_credential_sink_accepts_only_supplier_session_fields(self):
        vault = FakeVault()

        status = app.store_supplier_sso_credentials(
            "lingsuan.top",
            {
                "auth_token": "supplier-token",
                "refresh_token": "supplier-refresh",
                "session_cookie": "session=abc",
                "login_password": "must-not-be-written",
                "google_cookie": "must-not-be-written",
            },
            vault=vault,
        )

        self.assertTrue(status["configured"])
        self.assertEqual(
            vault.saved["lingsuan.top"],
            {
                "auth_token": "supplier-token",
                "refresh_token": "supplier-refresh",
                "session_cookie": "session=abc",
            },
        )

    def test_start_endpoint_uses_supplier_portal_and_interactive_browser(self):
        manager = FakeManager()
        with (
            patch.object(app, "load_management_state", return_value=LING_SUAN_STATE),
            patch.object(app, "_supplier_sso_manager", manager),
        ):
            result = app.api_start_supplier_sso("lingsuan.top")

        self.assertTrue(result["ok"])
        self.assertEqual(manager.started[0][:4], ("lingsuan.top", "https://lingsuan.top", True, 600))
        self.assertEqual(manager.started[0][4]["id"], "lingsuan-web")
        self.assertEqual(manager.started[0][4]["portal_base"], "https://lingsuan.top")

    def test_start_endpoint_does_not_require_automatic_quota_monitoring(self):
        state = {
            **LING_SUAN_STATE,
            "supplier_quotas": {
                "lingsuan.top": {
                    **LING_SUAN_STATE["supplier_quotas"]["lingsuan.top"],
                    "enabled": False,
                }
            },
        }
        manager = FakeManager()
        with (
            patch.object(app, "load_management_state", return_value=state),
            patch.object(app, "_supplier_sso_manager", manager),
        ):
            result = app.api_start_supplier_sso("lingsuan.top")

        self.assertTrue(result["ok"])
        self.assertEqual(manager.started[0][:4], ("lingsuan.top", "https://lingsuan.top", True, 600))

    def test_start_endpoint_uses_the_same_browser_sso_flow_for_autocode(self):
        state = {
            "suppliers": [{"id": "supplier-autocode", "name": "AutoCode"}],
            "api_profiles": [
                {
                    "id": "api-autocode",
                    "supplier_id": "supplier-autocode",
                    "api_base": "https://vip.auto-code.net/v1",
                    "enabled": True,
                }
            ],
            "supplier_quotas": {"auto-code.net": {"enabled": True}},
        }
        manager = FakeManager()
        with (
            patch.object(app, "load_management_state", return_value=state),
            patch.object(app, "_supplier_sso_manager", manager),
        ):
            result = app.api_start_supplier_sso("auto-code.net")

        self.assertTrue(result["ok"])
        self.assertEqual(manager.started[0][:4], ("auto-code.net", "https://vip.auto-code.net", True, 600))
        self.assertEqual(manager.started[0][4]["id"], "autocode-web")
        self.assertEqual(manager.started[0][4]["portal_base"], "https://vip.auto-code.net")

    def test_start_endpoint_uses_user_configured_autocode_portal_base(self):
        state = {
            "suppliers": [{"id": "supplier-autocode", "name": "AutoCode"}],
            "api_profiles": [
                {
                    "id": "api-autocode",
                    "supplier_id": "supplier-autocode",
                    "api_base": "https://vip.auto-code.net/v1",
                    "enabled": True,
                }
            ],
            "supplier_quotas": {
                "auto-code.net": {
                    "enabled": True,
                    "portal_base": "https://vip.auto-code.net",
                }
            },
        }
        manager = FakeManager()
        with (
            patch.object(app, "load_management_state", return_value=state),
            patch.object(app, "_supplier_sso_manager", manager),
        ):
            result = app.api_start_supplier_sso("auto-code.net")

        self.assertTrue(result["ok"])
        self.assertEqual(manager.started[0][1], "https://vip.auto-code.net")

    def test_status_cancel_and_clear_endpoints_delegate_without_credentials(self):
        manager = FakeManager()
        with (
            patch.object(app, "load_management_state", return_value=LING_SUAN_STATE),
            patch.object(app, "_supplier_sso_manager", manager),
        ):
            status = app.api_supplier_sso_status("lingsuan.top")
            cancelled = app.api_cancel_supplier_sso("lingsuan.top")
            cleared = app.api_clear_supplier_sso_session("lingsuan.top")

        self.assertEqual(status["sso_status"]["status"], "idle")
        self.assertTrue(cancelled["cancelled"])
        self.assertTrue(cleared["cleared"])
        self.assertNotIn("credentials", str(status))

    def test_complete_endpoint_signals_interactive_login(self):
        manager = FakeManager()
        with patch.object(app, "_supplier_sso_manager", manager):
            result = app.api_complete_supplier_sso("lingsuan.top")

        self.assertTrue(result["completed"])
        self.assertEqual(manager.completed, ["lingsuan.top"])

    def test_supplier_public_state_includes_safe_sso_and_runtime_status(self):
        manager = FakeManager()
        vault = FakeVault()
        with (
            patch.object(app, "_supplier_sso_manager", manager),
            patch.object(app, "get_credential_vault", return_value=vault),
            patch.object(
                app,
                "browser_runtime_status",
                return_value={"available": False, "package_installed": False, "browser_installed": False},
            ),
        ):
            state = app.supplier_quota_public_state(LING_SUAN_STATE)

        quota = state["lingsuan.top"]
        self.assertEqual(quota["sso_status"]["status"], "idle")
        self.assertEqual(quota["browser_sso"]["id"], "lingsuan-web")
        self.assertFalse(quota["browser_runtime"]["available"])
        self.assertNotIn("credentials", str(quota))


if __name__ == "__main__":
    unittest.main()
