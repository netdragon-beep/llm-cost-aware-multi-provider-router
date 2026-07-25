import sys
import tempfile
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

import app  # noqa: E402
from usage_quota_store import UsageQuotaStore  # noqa: E402


@unittest.skipUnless(hasattr(app, "supplier_cost_attribution_snapshot"), "supplier cost summary not implemented")
class SupplierCostAttributionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = UsageQuotaStore(Path(self.temp_dir.name) / "usage.db")
        self.store.initialize()
        self.state = {
            "suppliers": [{"id": "supplier-lingsuan", "name": "LingSuan"}],
            "api_profiles": [
                {
                    "id": "api-lingsuan",
                    "supplier_id": "supplier-lingsuan",
                    "api_base": "https://lingsuan.top/v1",
                    "custom_llm_provider": "openai",
                }
            ],
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_supplier_summary_reports_subscription_cost_and_price_coverage(self):
        provider_key = app.supplier_provider_key("supplier-lingsuan", "openai")
        self.store.insert_recharge_record(
            {
                "provider_key": provider_key,
                "paid_amount_cny": 30,
                "billing_type": "subscription",
                "service_start": "2026-07-01",
                "service_end": "2026-07-30",
                "allocation_mode": "daily-amortized",
            }
        )
        self.store.insert_usage_event(
            {
                "source": "litellm_gateway",
                "request_id": "request-1",
                "created_at": "2026-07-10T08:00:00+08:00",
                "platform_key": "lingsuan.top",
                "provider_key": provider_key,
                "api_profile_id": "api-lingsuan",
                "route_binding_id": "binding-1",
                "pricing_version_id": "price-1",
                "upstream_model": "gpt-5.4",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "estimated_cost": 1,
                "currency": "USD",
                "status": "success",
            }
        )

        snapshot = app.supplier_cost_attribution_snapshot(
            self.state,
            self.store,
            "2026-07-10T00:00:00+08:00",
            "2026-07-10T23:59:59+08:00",
        )
        row = snapshot["lingsuan.top"]

        self.assertEqual(row["gateway_requests"], 1)
        self.assertEqual(row["priced_requests"], 1)
        self.assertEqual(row["pricing_coverage"], 1)
        self.assertAlmostEqual(row["subscription_amortized_cny"], 1)
        self.assertAlmostEqual(row["attributed_cash_cost_cny"], 1)
        self.assertEqual(row["recent_events"][0]["route_binding_id"], "binding-1")
        self.assertFalse({"prompt", "response", "content"} & set(row["recent_events"][0]))

    def test_recharge_payload_accepts_subscription_contract(self):
        payload = app.RechargeRecordPayload(
            supplier_id="supplier-lingsuan",
            paid_amount_cny=30,
            billing_type="subscription",
            service_start="2026-07-01",
            service_end="2026-07-30",
            allocation_mode="daily-amortized",
        )

        self.assertEqual(payload.billing_type, "subscription")
        self.assertEqual(payload.service_end, "2026-07-30")

    def test_supplier_summary_reads_auto_detected_platform_batches(self):
        platform_key = "lingsuan.top"
        provider_key = app.supplier_platform_provider_key(platform_key)
        self.store.insert_recharge_record(
            {
                "provider_key": provider_key,
                "paid_amount_cny": 30,
                "credited_amount": 10,
                "balance_delta": 10,
                "credited_currency": "USD",
                "balance_before_recharge": 7.3,
                "balance_after_recharge": 17.3,
                "paid_at": "2026-07-10T00:00:00+08:00",
            }
        )
        self.store.insert_usage_event(
            {
                "source": "litellm_gateway",
                "request_id": "platform-batch-request",
                "created_at": "2026-07-10T08:00:00+08:00",
                "platform_key": platform_key,
                "provider_key": provider_key,
                "api_profile_id": "api-lingsuan",
                "public_model_name": "gpt-5.4",
                "upstream_model": "gpt-5.4",
                "total_tokens": 1000,
                "estimated_cost": 10,
                "currency": "USD",
                "status": "success",
            }
        )

        row = app.supplier_cost_attribution_snapshot(
            self.state,
            self.store,
            "2026-07-10T00:00:00+08:00",
            "2026-07-10T23:59:59+08:00",
        )[platform_key]

        self.assertAlmostEqual(row["attributed_cash_cost_cny"], 30)


class SupplierCostAttributionContractTests(unittest.TestCase):
    def test_supplier_cost_summary_function_exists(self):
        self.assertTrue(hasattr(app, "supplier_cost_attribution_snapshot"))

    def test_model_cost_uses_only_tokens_with_confirmed_cash_attribution(self):
        row = app.summarize_binding_model_usage(
            {
                "id": "route-gpt",
                "public_model_name": "gpt-5.4",
            },
            {
                "id": "binding-gpt",
                "model_route_id": "route-gpt",
                "upstream_model": "gpt-5.4",
            },
            {
                "id": "api-gpt",
                "label": "shared-api",
                "api_base": "https://example.test/v1",
                "custom_llm_provider": "openai",
            },
            {"id": "supplier-gpt", "name": "Example"},
            [
                {
                    "id": "paid-request",
                    "public_model_name": "gpt-5.4",
                    "upstream_model": "gpt-5.4",
                    "total_tokens": 1000,
                    "status": "success",
                },
                {
                    "id": "pending-request",
                    "public_model_name": "gpt-5.4",
                    "upstream_model": "gpt-5.4",
                    "total_tokens": 9000,
                    "status": "success",
                },
            ],
            {
                "paid-request": {
                    "status": "attributed-topup",
                    "total_cny": 30,
                },
                "pending-request": {
                    "status": "missing-paid",
                    "total_cny": 0,
                },
            },
        )

        self.assertEqual(row["cost_source"], "actual-cash")
        self.assertEqual(row["cash_tokens"], 1000)
        self.assertAlmostEqual(row["effective_cost_per_1m"], 30000)


if __name__ == "__main__":
    unittest.main()
