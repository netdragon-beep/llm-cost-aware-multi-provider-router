import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from claude_desktop_gateway import (  # noqa: E402
    build_model_discovery_response,
    build_shortcut_model_discovery_response,
    rewrite_shortcut_model,
)


class ClaudeDesktopGatewayTests(unittest.TestCase):
    def test_shortcut_slot_rewrites_to_internal_relaydeck_alias(self):
        self.assertEqual(
            rewrite_shortcut_model(
                "claude-sonnet",
                [{"name": "claude-sonnet", "tier": "sonnet", "target": "gpt-5.6-terra"}],
            ),
            "claude-haiku-relaydeck-gpt-5-6-terra",
        )
        self.assertEqual(rewrite_shortcut_model("other", []), "other")

    def test_published_public_model_rewrites_to_its_internal_relaydeck_alias(self):
        self.assertEqual(
            rewrite_shortcut_model(
                "gpt-5.6-terra",
                [],
                ["claude-fable-5", "gpt-5.6-terra"],
            ),
            "claude-haiku-relaydeck-gpt-5-6-terra",
        )

    def test_builtin_claude_model_uses_unambiguous_configured_family_target(self):
        self.assertEqual(
            rewrite_shortcut_model(
                "claude-opus-5",
                [{"name": "Opus", "tier": "opus", "target": "claude-fable-5-pojia"}],
            ),
            "claude-haiku-relaydeck-claude-fable-5-pojia",
        )
        self.assertEqual(
            rewrite_shortcut_model(
                "claude-opus-5",
                [
                    {"name": "Opus 4", "tier": "opus", "target": "claude-fable-5-pojia"},
                    {"name": "Opus 5", "tier": "opus", "target": "gpt-5.6-terra"},
                ],
            ),
            "claude-opus-5",
        )

    def test_shortcut_model_discovery_returns_only_mapped_slots(self):
        self.assertEqual(
            build_shortcut_model_discovery_response(
                [
                    {"name": "claude-haiku", "tier": "haiku", "target": "gpt-5.6-sol"},
                    {"name": "claude-sonnet", "tier": "sonnet", "target": "gpt-5.6-terra"},
                    {"name": "claude-opus", "tier": "opus", "target": ""},
                ]
            ),
            {
                "data": [
                    {
                        "type": "model",
                        "id": "claude-haiku",
                        "display_name": "claude-haiku -> gpt-5.6-sol",
                        "anthropic_family_tier": "haiku",
                    },
                    {
                        "type": "model",
                        "id": "claude-sonnet",
                        "display_name": "claude-sonnet -> gpt-5.6-terra",
                        "anthropic_family_tier": "sonnet",
                    },
                ],
                "has_more": False,
                "first_id": None,
                "last_id": None,
            },
        )

    def test_custom_claude_alias_rewrites_and_keeps_its_selected_tier(self):
        slots = [
            {"name": "claude-opus-5", "tier": "opus", "target": "gpt-5.6-terra"},
            {"name": "claude-sonnet-5.6", "tier": "sonnet", "target": "claude-fable-5"},
        ]

        self.assertEqual(
            rewrite_shortcut_model("claude-opus-5", slots),
            "claude-haiku-relaydeck-gpt-5-6-terra",
        )
        self.assertEqual(
            build_shortcut_model_discovery_response(slots)["data"],
            [
                {
                    "type": "model",
                    "id": "claude-opus-5",
                    "display_name": "claude-opus-5 -> gpt-5.6-terra",
                    "anthropic_family_tier": "opus",
                },
                {
                    "type": "model",
                    "id": "claude-sonnet-5.6",
                    "display_name": "claude-sonnet-5.6 -> claude-fable-5",
                    "anthropic_family_tier": "sonnet",
                },
            ],
        )

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

    def test_shortcut_discovery_includes_all_published_public_models(self):
        response = build_shortcut_model_discovery_response(
            [{"name": "Opus", "tier": "opus", "target": "claude-fable-5"}],
            ["claude-fable-5", "gpt-5.6-terra"],
        )

        self.assertEqual(
            response["data"],
            [
                {
                    "type": "model",
                    "id": "claude-fable-5",
                    "display_name": "claude-fable-5",
                    "anthropic_family_tier": "fable",
                },
                {
                    "type": "model",
                    "id": "gpt-5.6-terra",
                    "display_name": "gpt-5.6-terra",
                    "anthropic_family_tier": "sonnet",
                },
                {
                    "type": "model",
                    "id": "Opus",
                    "display_name": "Opus -> claude-fable-5",
                    "anthropic_family_tier": "opus",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
