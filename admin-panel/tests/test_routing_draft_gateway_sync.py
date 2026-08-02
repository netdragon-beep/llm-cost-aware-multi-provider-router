import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

import app  # noqa: E402


class RoutingDraftGatewaySyncTests(unittest.TestCase):
    def state(self, *, gateway_enabled: bool, upstream_model: str = "gpt-5"):
        return {
            "suppliers": [{"id": "supplier-1", "name": "Supplier", "enabled": True}],
            "api_profiles": [
                {
                    "id": "api-1",
                    "supplier_id": "supplier-1",
                    "label": "Primary API",
                    "api_base": "https://example.test/v1",
                    "custom_llm_provider": "openai",
                    "enabled": True,
                }
            ],
            "model_routes": [
                {
                    "id": "route-1",
                    "public_model_name": "gpt-5",
                    "gateway_enabled": gateway_enabled,
                    "enabled": True,
                }
            ],
            "route_bindings": [
                {
                    "id": "binding-1",
                    "model_route_id": "route-1",
                    "api_profile_id": "api-1",
                    "upstream_model": upstream_model,
                    "priority": 10,
                    "enabled": True,
                }
            ],
            "model_families": [],
            "router_settings": {},
            "litellm_settings": {},
            "routing_view_mode": "by_model",
            "supplier_quotas": {},
        }

    def save_draft(self, state):
        return app.api_save_routing_draft(app.RoutingDraftPayload(**state))

    def test_load_management_state_preserves_gateway_runtime_configs_from_disk(self):
        state = self.state(gateway_enabled=True)
        runtime_configs = {
            "openai": {"model_list": [{"model_name": "gpt-5"}]},
            "claude": {"model_list": [{"model_name": "gpt-5"}]},
        }
        state["gateway_runtime_configs"] = runtime_configs

        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "relaydeck-state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            with patch.object(app, "STATE_PATH", state_path):
                loaded_state = app.load_management_state()

        self.assertEqual(loaded_state["gateway_runtime_configs"], runtime_configs)

    def test_published_route_binding_change_synchronizes_gateway_runtime(self):
        previous_state = self.state(gateway_enabled=True, upstream_model="gpt-5")
        draft_state = self.state(gateway_enabled=True, upstream_model="gpt-5.1")

        with (
            patch.object(app, "load_management_state", return_value=previous_state),
            patch.object(app, "save_management_state") as save_state,
            patch.object(app, "save_gateway_configs") as save_configs,
            patch.object(app, "restart_litellm") as restart,
        ):
            result = self.save_draft(draft_state)

        self.assertEqual(save_state.call_count, 2)
        save_configs.assert_called_once()
        restart.assert_called_once()
        configs = save_configs.call_args.args[0]
        self.assertEqual(configs["openai"]["model_list"][0]["litellm_params"]["model"], "gpt-5.1")
        self.assertEqual(configs["claude"]["model_list"][0]["litellm_params"]["model"], "gpt-5.1")
        self.assertEqual(save_state.call_args_list[-1].kwargs["gateway_runtime_configs"], configs)
        self.assertTrue(result["gateway_reloaded"])

    def test_unpublished_route_binding_change_only_saves_draft(self):
        previous_state = self.state(gateway_enabled=False, upstream_model="gpt-5")
        draft_state = self.state(gateway_enabled=False, upstream_model="gpt-5.1")
        draft_state["litellm_settings"] = {
            "relaydeck_claude_discovery": {
                "claude_desktop_discovery_enabled": True,
                "claude_desktop_default_tier": "sonnet",
            }
        }

        with (
            patch.object(app, "load_management_state", return_value=copy.deepcopy(previous_state)),
            patch.object(app, "save_management_state") as save_state,
            patch.object(app, "save_gateway_configs") as save_configs,
            patch.object(app, "restart_litellm") as restart,
        ):
            result = self.save_draft(draft_state)

        save_state.assert_called_once()
        save_configs.assert_not_called()
        restart.assert_not_called()
        self.assertFalse(result["gateway_reloaded"])

    def test_same_draft_retries_gateway_sync_after_a_write_failure(self):
        previous_state = self.state(gateway_enabled=True, upstream_model="gpt-5")
        draft_state = self.state(gateway_enabled=True, upstream_model="gpt-5.1")

        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "relaydeck-state.json"
            state_path.write_text(json.dumps(previous_state), encoding="utf-8")

            with (
                patch.object(app, "STATE_PATH", state_path),
                patch.object(
                    app,
                    "save_gateway_configs",
                    side_effect=[OSError("write failed"), (b"legacy-openai\n", b"legacy-claude\n")],
                ) as save_configs,
                patch.object(app, "restart_litellm") as restart,
            ):
                first_result = self.save_draft(draft_state)
                second_result = self.save_draft(draft_state)

        self.assertFalse(first_result["gateway_reloaded"])
        self.assertTrue(second_result["gateway_reloaded"])
        self.assertEqual(save_configs.call_count, 2)
        self.assertEqual(
            [call.args[0]["openai"]["model_list"][0]["litellm_params"]["model"] for call in save_configs.call_args_list],
            ["gpt-5.1", "gpt-5.1"],
        )
        restart.assert_called_once()

    def test_gateway_restart_failure_reloads_previous_gateway_configs(self):
        previous_state = self.state(gateway_enabled=True, upstream_model="gpt-5")
        draft_state = self.state(gateway_enabled=True, upstream_model="gpt-5.1")

        with (
            patch.object(app, "load_management_state", return_value=previous_state),
            patch.object(app, "save_management_state"),
            patch.object(app, "save_gateway_configs", return_value=(b"legacy-openai\n", b"legacy-claude\n")) as save_configs,
            patch.object(app, "_restore_gateway_configs", create=True) as restore_configs,
            patch.object(app, "restart_litellm", side_effect=[RuntimeError("restart failed"), None]) as restart,
        ):
            result = self.save_draft(draft_state)

        self.assertFalse(result["gateway_reloaded"])
        self.assertEqual(result["gateway_reload_error"], "restart failed")
        save_configs.assert_called_once()
        restore_configs.assert_called_once_with(b"legacy-openai\n", b"legacy-claude\n")
        self.assertEqual(restart.call_count, 2)

    def test_gateway_restart_failure_reports_recovery_restart_failure(self):
        previous_state = self.state(gateway_enabled=True, upstream_model="gpt-5")
        draft_state = self.state(gateway_enabled=True, upstream_model="gpt-5.1")

        with (
            patch.object(app, "load_management_state", return_value=previous_state),
            patch.object(app, "save_management_state"),
            patch.object(app, "save_gateway_configs", return_value=(b"legacy-openai\n", b"legacy-claude\n")),
            patch.object(app, "_restore_gateway_configs", create=True),
            patch.object(app.logger, "exception"),
            patch.object(
                app,
                "restart_litellm",
                side_effect=[RuntimeError("apply restart failed"), RuntimeError("recovery restart failed")],
            ) as restart,
        ):
            result = self.save_draft(draft_state)

        self.assertEqual(
            result["gateway_reload_error"],
            "apply restart failed; recovery failed: recovery restart failed",
        )
        self.assertEqual(restart.call_count, 2)

    def test_save_gateway_configs_restores_both_files_when_second_write_fails(self):
        with self.subTest("second config write"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                openai_path = temp_path / "litellm.yaml"
                claude_path = temp_path / "litellm-claude.yaml"
                legacy_openai = b"# preserve this comment\nunknown_openai: true\n"
                legacy_claude = b"# preserve this comment\nunknown_claude: true\n"
                openai_path.write_bytes(legacy_openai)
                claude_path.write_bytes(legacy_claude)

                def write_openai_then_fail(path, config):
                    if path == claude_path:
                        raise OSError("claude write failed")
                    path.write_text(app.yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

                with (
                    patch.object(app, "CONFIG_PATH", openai_path),
                    patch.object(app, "CLAUDE_CONFIG_PATH", claude_path),
                    patch.object(app, "_write_gateway_config", side_effect=write_openai_then_fail, create=True),
                ):
                    with self.assertRaisesRegex(OSError, "claude write failed"):
                        app.save_gateway_configs({"openai": {"model_list": ["new-openai"]}, "claude": {"model_list": ["new-claude"]}})

                self.assertEqual(openai_path.read_bytes(), legacy_openai)
                self.assertEqual(claude_path.read_bytes(), legacy_claude)

    def test_routing_draft_save_uses_one_lock_for_the_full_operation(self):
        previous_state = self.state(gateway_enabled=True, upstream_model="gpt-5")
        draft_state = self.state(gateway_enabled=True, upstream_model="gpt-5.1")
        save_lock = MagicMock()

        with (
            patch.object(app, "ROUTING_DRAFT_SAVE_LOCK", save_lock, create=True),
            patch.object(app, "load_management_state", return_value=previous_state),
            patch.object(app, "save_management_state"),
            patch.object(app, "save_gateway_configs"),
            patch.object(app, "restart_litellm"),
        ):
            self.save_draft(draft_state)

        save_lock.__enter__.assert_called_once_with()
        save_lock.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
