import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))


class ServiceStatusApiTests(unittest.TestCase):
    def test_service_status_endpoint_returns_only_ports_and_service_states(self):
        import app

        statuses = {
            "litellm": {"listening": True, "port": 4100},
            "claude": {"listening": True, "port": 4101},
            "admin": {"listening": True, "port": 8091},
        }

        def fake_service_status(_port, *, command_pattern, **_kwargs):
            if command_pattern == "litellm":
                return statuses["litellm"]
            if command_pattern == "claude_desktop_gateway:app":
                return statuses["claude"]
            return statuses["admin"]

        with patch.object(app, "parse_env_file", return_value={}), \
             patch.object(app, "get_ports", return_value=(4100, 8091)), \
             patch.object(app, "service_status", side_effect=fake_service_status):
            payload = app.api_service_status()

        self.assertEqual(payload["ports"], {"litellm": 4100, "claude": 4101, "admin_panel": 8091})
        self.assertEqual(set(payload["service_status"]), {"litellm", "claude", "admin_panel"})
        self.assertEqual(payload["service_status"]["claude"]["listening"], True)
        self.assertNotIn("model_routes", payload)
        self.assertNotIn("api_profiles", payload)

    def test_unknown_model_family_has_a_stable_uncategorized_fallback(self):
        import app

        self.assertIn("未分类", app.DEFAULT_MODEL_FAMILIES)
        self.assertEqual(app.guess_model_family("vendor-private-model"), "未分类")


if __name__ == "__main__":
    unittest.main()
