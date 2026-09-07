import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "admin-panel"))
import app  # noqa: E402


class ClientGatewayModelsTests(unittest.TestCase):
    @patch.object(app, "parse_env_file", return_value={"LITELLM_MASTER_KEY": "test"})
    @patch.object(app, "get_ports", return_value=(4100, 8091))
    @patch.object(app.httpx, "Client")
    def test_lists_active_openai_and_claude_models_without_secrets(self, client, _ports, _env):
        responses = [
            type("R", (), {"is_success": True, "status_code": 200, "json": lambda self: {"data": [{"id": "gpt-5"}]}})(),
            type("R", (), {"is_success": True, "status_code": 200, "json": lambda self: {"data": [{"id": "claude-haiku-relaydeck-gpt-5"}]}})(),
        ]
        client.return_value.__enter__.return_value.get.side_effect = responses
        result = app.client_gateway_models()
        self.assertEqual(result["openai"]["models"], ["gpt-5"])
        self.assertEqual(result["claude"]["models"], [{"alias": "claude-haiku-relaydeck-gpt-5", "public_model_name": "gpt-5"}])
        self.assertNotIn("test", str(result))
