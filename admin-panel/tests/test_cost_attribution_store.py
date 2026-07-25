import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

from usage_quota_store import UsageQuotaStore  # noqa: E402


class CostAttributionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "usage.db"
        self.store = UsageQuotaStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initialize_migrates_existing_usage_and_recharge_tables(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE usage_events (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    request_id TEXT
                );
                CREATE TABLE recharge_records (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    paid_at TEXT NOT NULL,
                    provider_key TEXT NOT NULL,
                    paid_amount_cny REAL NOT NULL
                );
                """
            )

        self.store.initialize()
        self.store.initialize()

        with closing(sqlite3.connect(self.db_path)) as conn:
            usage_columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
            recharge_columns = {row[1] for row in conn.execute("PRAGMA table_info(recharge_records)")}

        self.assertTrue(
            {
                "platform_key",
                "api_profile_id",
                "model_route_id",
                "route_binding_id",
                "pricing_version_id",
                "cash_cost_cny",
                "cost_attribution_status",
            }.issubset(usage_columns)
        )
        self.assertTrue(
            {"billing_type", "service_start", "service_end", "allocation_mode"}.issubset(recharge_columns)
        )

    def test_cost_batch_fields_round_trip(self):
        self.store.initialize()

        row = self.store.insert_recharge_record(
            {
                "provider_key": "supplier:lingsuan.top",
                "paid_amount_cny": 30,
                "billing_type": "subscription",
                "service_start": "2026-07-01",
                "service_end": "2026-07-30",
                "allocation_mode": "daily-amortized",
            }
        )

        stored = self.store.get_recharge_record(row["id"])
        self.assertEqual(stored["billing_type"], "subscription")
        self.assertEqual(stored["service_start"], "2026-07-01")
        self.assertEqual(stored["service_end"], "2026-07-30")
        self.assertEqual(stored["allocation_mode"], "daily-amortized")

    def test_cost_batch_payment_amount_can_be_filled_later(self):
        self.store.initialize()
        row = self.store.insert_recharge_record(
            {
                "provider_key": "supplier:pending",
                "paid_amount_cny": None,
                "billing_type": "subscription",
                "service_start": "2026-07-01",
                "service_end": "2026-07-31",
            }
        )

        updated = self.store.update_recharge_record(row["id"], {"paid_amount_cny": 30})

        self.assertEqual(updated["paid_amount_cny"], 30)
        self.assertEqual(self.store.get_recharge_record(row["id"])["paid_amount_cny"], 30)

    def test_balance_increase_creates_one_pending_credit_batch(self):
        self.store.initialize()

        first, first_created = self.store.record_balance_increase(
            provider_key="supplier:shared",
            relay_label="Shared supplier",
            previous_balance=7.3,
            current_balance=37.3,
            currency="USD",
            detected_at="2026-07-22T10:00:00Z",
        )
        second, second_created = self.store.record_balance_increase(
            provider_key="supplier:shared",
            relay_label="Shared supplier",
            previous_balance=7.3,
            current_balance=37.3,
            currency="USD",
            detected_at="2026-07-22T10:05:00Z",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["balance_before_recharge"], 7.3)
        self.assertEqual(first["balance_after_recharge"], 37.3)
        self.assertEqual(first["credited_amount"], 30.0)
        self.assertIsNone(first["paid_amount_cny"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.store.list_recharge_records(provider_key="supplier:shared")), 1)

    def test_usage_event_is_idempotent_by_source_and_request_id(self):
        self.store.initialize()
        event = {
            "source": "litellm_gateway",
            "request_id": "request-123",
            "platform_key": "lingsuan.top",
            "provider_key": "billing-supplier-lingsuan",
            "api_profile_id": "api-lingsuan",
            "model_route_id": "route-gpt-5-4",
            "route_binding_id": "binding-lingsuan-gpt-5-4",
            "pricing_version_id": "price-v1",
            "upstream_model": "gpt-5.4",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cash_cost_cny": 0.01,
            "cost_attribution_status": "attributed-topup",
        }

        first, first_created = self.store.insert_usage_event_once(event)
        second, second_created = self.store.insert_usage_event_once(event)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.list_usage_events(limit=10)), 1)
        self.assertEqual(second["pricing_version_id"], "price-v1")
        self.assertEqual(second["provider_key"], "billing-supplier-lingsuan")

    def test_effective_price_lookup_uses_request_time(self):
        self.store.initialize()
        first = self.store.insert_pricing_version(
            {
                "platform_key": "lingsuan.top",
                "api_profile_id": "api-lingsuan",
                "upstream_model": "gpt-5.4",
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
        self.store.activate_pricing_version(first["id"])
        second = self.store.insert_pricing_version(
            {
                "platform_key": "lingsuan.top",
                "api_profile_id": "api-lingsuan",
                "upstream_model": "gpt-5.4",
                "base_input_per_1m": 12,
                "base_output_per_1m": 48,
                "multiplier": 1,
                "input_per_1m": 12,
                "output_per_1m": 48,
                "currency": "USD",
                "effective_from": "2026-07-20T00:00:00Z",
                "fingerprint": "price-v2",
            }
        )
        self.store.activate_pricing_version(second["id"])

        old_price = self.store.get_effective_pricing_version(
            "api-lingsuan", "gpt-5.4", "2026-07-10T00:00:00Z"
        )
        new_price = self.store.get_effective_pricing_version(
            "api-lingsuan", "gpt-5.4", "2026-07-21T00:00:00Z"
        )

        self.assertEqual(old_price["id"], first["id"])
        self.assertEqual(new_price["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
