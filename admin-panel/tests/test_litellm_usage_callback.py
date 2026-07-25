import importlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADMIN_PANEL_DIR = ROOT / "admin-panel"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ADMIN_PANEL_DIR))
CALLBACK_PATH = ROOT / "relaydeck_litellm_callback.py"
CONFIG_CALLBACK_PATH = ROOT / "config" / "relaydeck_callback.py"

import app  # noqa: E402
from usage_quota_store import UsageQuotaStore  # noqa: E402


class LiteLLMConfigAttributionTests(unittest.TestCase):
    def test_generated_config_registers_callback_and_route_metadata(self):
        providers = [
            {
                "model_name": "gpt-5.4",
                "upstream_model": "openai/gpt-5.4",
                "api_base": "https://lingsuan.top/v1",
                "api_key_env": "LINGSUAN_KEY",
                "custom_llm_provider": "openai",
                "relay_label": "lingsuan-codex",
                "supplier_id": "supplier-lingsuan",
                "api_profile_id": "api-lingsuan",
                "model_route_id": "route-gpt-5-4",
                "binding_id": "binding-gpt-5-4-lingsuan",
                "platform_key": "lingsuan.top",
                "cost_provider_key": "billing-supplier-lingsuan",
                "priority": 100,
                "enabled": True,
            }
        ]

        config = app.build_litellm_config_from_providers(providers, {}, {"drop_params": True})
        params = config["model_list"][0]["litellm_params"]
        model_info = config["model_list"][0]["model_info"]

        self.assertIn("relaydeck_callback.proxy_handler_instance", config["litellm_settings"]["callbacks"])
        self.assertNotIn("relaydeck_callback.proxy_handler_instance", config["litellm_settings"].get("success_callback", []))
        self.assertNotIn("relaydeck_callback.proxy_handler_instance", config["litellm_settings"].get("failure_callback", []))
        self.assertTrue(CONFIG_CALLBACK_PATH.exists())
        self.assertEqual(model_info["api_profile_id"], "api-lingsuan")
        self.assertEqual(model_info["relaydeck"]["cost_provider_key"], "billing-supplier-lingsuan")
        self.assertEqual(params["metadata"]["relaydeck"]["route_binding_id"], "binding-gpt-5-4-lingsuan")
        self.assertEqual(params["metadata"]["relaydeck"]["cost_provider_key"], "billing-supplier-lingsuan")


@unittest.skipUnless(CALLBACK_PATH.exists(), "LiteLLM callback not implemented")
class LiteLLMUsageCallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.callback = importlib.import_module("relaydeck_litellm_callback")

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = UsageQuotaStore(Path(self.temp_dir.name) / "usage.db")
        self.store.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def callback_kwargs():
        return {
            "model": "gpt-5.4",
            "litellm_call_id": "call-123",
            "response_cost": 0.0004,
            "litellm_params": {
                "metadata": {
                    "model_group": "gpt-5.4",
                    "relaydeck": {
                        "platform_key": "lingsuan.top",
                        "cost_provider_key": "billing-supplier-lingsuan",
                        "api_profile_id": "api-lingsuan",
                        "model_route_id": "route-gpt-5-4",
                        "route_binding_id": "binding-gpt-5-4-lingsuan",
                        "public_model_name": "gpt-5.4",
                        "upstream_model": "openai/gpt-5.4",
                        "relay_label": "lingsuan-codex",
                        "api_base_hash": "hash-123",
                        "custom_llm_provider": "openai",
                    },
                }
            },
        }

    def test_normalizer_extracts_safe_route_usage_without_content(self):
        response = {
            "id": "response-123",
            "model": "openai/gpt-5.4",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            "choices": [{"message": {"content": "must not be persisted"}}],
        }

        event = self.callback.normalize_litellm_usage_event(
            self.callback_kwargs(),
            response,
            datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 21, 0, 0, 1, tzinfo=timezone.utc),
            status="success",
        )

        self.assertEqual(event["request_id"], "response-123")
        self.assertEqual(event["api_profile_id"], "api-lingsuan")
        self.assertEqual(event["route_binding_id"], "binding-gpt-5-4-lingsuan")
        self.assertEqual(event["total_tokens"], 120)
        self.assertEqual(event["latency_ms"], 1000)
        self.assertNotIn("choices", event)
        self.assertNotIn("content", repr(event))

    def test_normalizer_accepts_litellm_model_info_metadata_shape(self):
        kwargs = self.callback_kwargs()
        relaydeck = kwargs["litellm_params"]["metadata"].pop("relaydeck")
        kwargs["litellm_params"]["model_info"] = {"relaydeck": relaydeck}

        event = self.callback.normalize_litellm_usage_event(
            kwargs,
            {"id": "response-nested", "usage": {"total_tokens": 1}},
            datetime(2026, 7, 21, 1, 0),
            datetime(2026, 7, 21, 1, 0, 1),
            status="success",
        )

        self.assertEqual(event["api_profile_id"], "api-lingsuan")
        self.assertEqual(event["provider_key"], "billing-supplier-lingsuan")
        self.assertRegex(event["created_at"], r"[+-]\d\d:\d\d$")

    def test_enrichment_binds_price_version_and_topup_cash_cost(self):
        price = self.store.insert_pricing_version(
            {
                "platform_key": "lingsuan.top",
                "api_profile_id": "api-lingsuan",
                "upstream_model": "openai/gpt-5.4",
                "base_input_per_1m": 10,
                "base_output_per_1m": 40,
                "multiplier": 1,
                "input_per_1m": 10,
                "output_per_1m": 40,
                "currency": "USD",
                "effective_from": "2026-07-01T00:00:00Z",
                "fingerprint": "price-v1",
            }
        )
        self.store.activate_pricing_version(price["id"])
        self.store.insert_recharge_record(
            {
                "provider_key": "billing-supplier-lingsuan",
                "paid_amount_cny": 30,
                "credited_amount": 90,
                "credited_currency": "USD",
            }
        )
        event = {
            "source": "litellm_gateway",
            "request_id": "request-1",
            "created_at": "2026-07-21T00:00:00Z",
            "platform_key": "lingsuan.top",
            "provider_key": "billing-supplier-lingsuan",
            "api_profile_id": "api-lingsuan",
            "upstream_model": "openai/gpt-5.4",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "status": "success",
        }

        enriched = self.callback.enrich_usage_event(event, self.store)

        self.assertEqual(enriched["pricing_version_id"], price["id"])
        self.assertAlmostEqual(enriched["estimated_cost"], 0.0003)
        self.assertAlmostEqual(enriched["cash_cost_cny"], 0.0001)
        self.assertEqual(enriched["cost_attribution_status"], "attributed-topup")


class LiteLLMCallbackContractTests(unittest.TestCase):
    def test_callback_module_exists(self):
        self.assertTrue(CALLBACK_PATH.exists())


if __name__ == "__main__":
    unittest.main()
