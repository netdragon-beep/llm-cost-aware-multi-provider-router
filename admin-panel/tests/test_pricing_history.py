import sys
import tempfile
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

from usage_quota_store import UsageQuotaStore  # noqa: E402


class PricingHistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = UsageQuotaStore(Path(self.temp_dir.name) / "usage.db")
        self.store.initialize()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def record(**overrides):
        return {
            "platform_key": "lingsuan.top",
            "api_profile_id": "api-lingsuan",
            "upstream_model": "gpt-5.4",
            "group_name": "Pro",
            "base_input_per_1m": 10,
            "base_output_per_1m": 40,
            "multiplier": 0.5,
            "input_per_1m": 5,
            "output_per_1m": 20,
            "currency": "USD",
            "effective_from": "2026-07-01T00:00:00+08:00",
            "status": "candidate",
            "source": "manual",
            "confidence": "exact",
            "fingerprint": "price-v1",
            **overrides,
        }

    def test_activating_new_version_supersedes_previous_active_version(self):
        first = self.store.insert_pricing_version(self.record())
        self.store.activate_pricing_version(first["id"])
        second = self.store.insert_pricing_version(
            self.record(
                input_per_1m=6,
                output_per_1m=24,
                effective_from="2026-07-20T00:00:00+08:00",
                fingerprint="price-v2",
            )
        )

        activated = self.store.activate_pricing_version(second["id"])
        rows = self.store.list_pricing_versions(platform_key="lingsuan.top")
        previous = next(item for item in rows if item["id"] == first["id"])

        self.assertEqual(activated["status"], "active")
        self.assertEqual(previous["status"], "superseded")
        self.assertEqual(previous["effective_to"], "2026-07-20T00:00:00+08:00")
        self.assertEqual(
            self.store.get_active_pricing_version("api-lingsuan", "gpt-5.4")["id"],
            second["id"],
        )

    def test_repeated_candidate_observation_updates_one_row(self):
        first = self.store.observe_pricing_candidate(self.record(source="adapter", fingerprint="observed-price"))
        second = self.store.observe_pricing_candidate(self.record(source="adapter", fingerprint="observed-price"))

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["observation_count"], 2)
        self.assertEqual(len(self.store.list_pricing_versions(platform_key="lingsuan.top", status="candidate")), 1)

    def test_rejecting_candidate_preserves_active_version(self):
        active = self.store.insert_pricing_version(self.record(fingerprint="active-price"))
        self.store.activate_pricing_version(active["id"])
        candidate = self.store.observe_pricing_candidate(
            self.record(input_per_1m=7, fingerprint="candidate-price", source="adapter")
        )

        rejected = self.store.reject_pricing_version(candidate["id"])

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(
            self.store.get_active_pricing_version("api-lingsuan", "gpt-5.4")["id"],
            active["id"],
        )


if __name__ == "__main__":
    unittest.main()
