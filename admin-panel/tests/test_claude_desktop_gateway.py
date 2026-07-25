import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from claude_desktop_gateway import build_model_discovery_response  # noqa: E402


class ClaudeDesktopGatewayTests(unittest.TestCase):
    def test_model_discovery_uses_anthropic_gateway_shape_for_all_models(self):
        response = build_model_discovery_response(
            [
                {"id": "claude-haiku-relaydeck-gpt-5-6-sol"},
                {"id": "claude-haiku-relaydeck-deepseek-v4-flash"},
            ],
            tier="haiku",
        )

        self.assertEqual(
            response,
            {
                "data": [
                    {
                        "type": "model",
                        "id": "claude-haiku-relaydeck-gpt-5-6-sol",
                        "display_name": "claude-haiku-relaydeck-gpt-5-6-sol",
                        "anthropic_family_tier": "haiku",
                    },
                    {
                        "type": "model",
                        "id": "claude-haiku-relaydeck-deepseek-v4-flash",
                        "display_name": "claude-haiku-relaydeck-deepseek-v4-flash",
                        "anthropic_family_tier": "haiku",
                    },
                ],
                "has_more": False,
                "first_id": None,
                "last_id": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
