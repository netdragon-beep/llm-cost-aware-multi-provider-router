import sys
import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "admin-panel"))

from relaydeck_litellm_discovery import (  # noqa: E402
    claude_discovery_metadata,
    normalize_claude_discovery_settings,
)
import app  # noqa: E402


class ClaudeModelDiscoveryTests(unittest.TestCase):
    def test_disabled_discovery_does_not_add_metadata(self):
        self.assertEqual(
            claude_discovery_metadata("gpt-5.6-luna", {}),
            {},
        )

    def test_enabled_discovery_infers_claude_tier(self):
        settings = {
            "claude_desktop_discovery_enabled": True,
            "claude_desktop_default_tier": "sonnet",
        }

        self.assertEqual(
            claude_discovery_metadata("claude-opus-4.8", settings),
            {
                "anthropic_family_tier": "opus",
            },
        )

    def test_enabled_discovery_uses_default_tier_for_other_families(self):
        settings = {
            "claude_desktop_discovery_enabled": True,
            "claude_desktop_default_tier": "sonnet",
        }

        self.assertEqual(
            claude_discovery_metadata("gpt-5.6-luna", settings),
            {
                "anthropic_family_tier": "sonnet",
            },
        )

    def test_invalid_default_tier_normalizes_to_sonnet(self):
        self.assertEqual(
            normalize_claude_discovery_settings(
                {
                    "claude_desktop_discovery_enabled": True,
                    "claude_desktop_default_tier": "invalid",
                }
            ),
            {
                "claude_desktop_discovery_enabled": True,
                "claude_desktop_default_tier": "sonnet",
            },
        )

    def test_generated_config_marks_published_model_for_claude_discovery(self):
        providers = [
            {
                "model_name": "gpt-5.6-luna",
                "upstream_model": "gpt-5.6-luna",
                "api_base": "https://example.test/v1",
                "api_key_env": "EXAMPLE_KEY",
                "custom_llm_provider": "openai",
                "relay_label": "example",
                "gateway_enabled": True,
                "enabled": True,
            }
        ]

        config = app.build_litellm_config_from_providers(
            providers,
            {},
            {
                "drop_params": True,
                "relaydeck_claude_discovery": {
                    "claude_desktop_discovery_enabled": True,
                    "claude_desktop_default_tier": "sonnet",
                },
            },
        )

        model_info = config["model_list"][0]["model_info"]
        self.assertEqual(model_info["anthropic_family_tier"], "sonnet")
        self.assertNotIn("relaydeck_claude_discovery", config["litellm_settings"])

    def test_generated_config_keeps_discovery_metadata_off_by_default(self):
        providers = [
            {
                "model_name": "gpt-5.6-luna",
                "upstream_model": "gpt-5.6-luna",
                "gateway_enabled": True,
                "enabled": True,
            }
        ]

        config = app.build_litellm_config_from_providers(providers, {}, {})

        self.assertNotIn(
            "anthropic_family_tier",
            config["model_list"][0]["model_info"],
        )

    def test_dual_gateway_configs_keep_native_and_claude_groups_separate(self):
        providers = [
            {
                "model_name": "gpt-5.6-sol",
                "upstream_model": "gpt-5.6-sol",
                "api_base": "https://example.test/v1",
                "api_key_env": "EXAMPLE_KEY",
                "custom_llm_provider": "openai",
                "relay_label": "primary",
                "gateway_enabled": True,
                "enabled": True,
            }
        ]

        configs = app.build_litellm_gateway_configs_from_providers(providers, {}, {})

        self.assertEqual(
            [item["model_name"] for item in configs["openai"]["model_list"]],
            ["gpt-5.6-sol"],
        )
        self.assertEqual(
            [item["model_name"] for item in configs["claude"]["model_list"]],
            ["claude-haiku-relaydeck-gpt-5-6-sol"],
        )
        self.assertEqual(
            configs["claude"]["model_list"][0]["litellm_params"],
            configs["openai"]["model_list"][0]["litellm_params"],
        )

    def test_dual_gateway_configs_keep_fallback_order_for_claude_aliases(self):
        providers = [
            {
                "model_name": "gpt-5.6-sol",
                "upstream_model": "gpt-5.6-sol-primary",
                "relay_label": "primary",
                "gateway_enabled": True,
                "enabled": True,
                "priority": 10,
            },
            {
                "model_name": "gpt-5.6-sol",
                "upstream_model": "gpt-5.6-sol-backup",
                "relay_label": "backup",
                "gateway_enabled": True,
                "enabled": True,
                "priority": 20,
            },
        ]

        configs = app.build_litellm_gateway_configs_from_providers(providers, {}, {})

        self.assertEqual(
            configs["openai"]["router_settings"]["fallbacks"],
            [{"gpt-5.6-sol": ["gpt-5.6-sol__2__backup"]}],
        )
        self.assertEqual(
            configs["claude"]["router_settings"]["fallbacks"],
            [{
                "claude-haiku-relaydeck-gpt-5-6-sol": [
                    "claude-haiku-relaydeck-gpt-5-6-sol__2__backup"
                ]
            }],
        )

    def test_claude_code_settings_merge_preserves_existing_values_and_enables_discovery(self):
        with TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(
                json.dumps({"theme": "dark", "env": {"KEEP": "yes"}}),
                encoding="utf-8",
            )

            result = app.configure_claude_code_settings(
                settings_path,
                "http://127.0.0.1:4101",
                "sk-test",
            )

            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["theme"], "dark")
            self.assertEqual(saved["env"]["KEEP"], "yes")
            self.assertEqual(saved["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:4101")
            self.assertEqual(saved["env"]["ANTHROPIC_AUTH_TOKEN"], "sk-test")
            self.assertEqual(saved["env"]["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"], "1")
            self.assertTrue(Path(result["backup_path"]).exists())


if __name__ == "__main__":
    unittest.main()
