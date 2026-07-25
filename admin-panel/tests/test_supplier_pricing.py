import sys
import tempfile
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

import app  # noqa: E402
from usage_quota_store import UsageQuotaStore  # noqa: E402


class SupplierPricingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = UsageQuotaStore(Path(self.temp_dir.name) / "usage.db")
        self.store.initialize()
        self.profile = {"id": "api-lingsuan", "label": "LingSuan Pro"}

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def observation(**overrides):
        return {
            "upstream_model": "gpt-5.4",
            "group_name": "Pro",
            "base_input_per_1m": 10,
            "base_output_per_1m": 40,
            "multiplier": 0.5,
            "currency": "USD",
            "effective_from": "2026-07-20T00:00:00+08:00",
            **overrides,
        }

    def seed_active(self, **overrides):
        normalized = app.normalize_pricing_observation(
            "lingsuan.top",
            self.profile,
            self.observation(**overrides),
            source="manual",
        )
        row = self.store.insert_pricing_version(normalized)
        return self.store.activate_pricing_version(row["id"])

    def test_group_display_name_does_not_change_price_fingerprint(self):
        first = app.normalize_pricing_observation(
            "lingsuan.top", self.profile, self.observation(group_name="Pro"), source="adapter"
        )
        renamed = app.normalize_pricing_observation(
            "lingsuan.top", self.profile, self.observation(group_name="高级组"), source="adapter"
        )

        self.assertEqual(first["fingerprint"], renamed["fingerprint"])

    def test_manual_policy_ignores_adapter_observations(self):
        result = app.process_pricing_observations(
            "lingsuan.top",
            self.profile,
            [self.observation()],
            automation_mode="manual",
            store=self.store,
        )

        self.assertEqual(result["ignored"], 1)
        self.assertEqual(self.store.list_pricing_versions(platform_key="lingsuan.top"), [])

    def test_detect_confirm_creates_candidate(self):
        self.seed_active()
        result = app.process_pricing_observations(
            "lingsuan.top",
            self.profile,
            [self.observation(base_input_per_1m=12)],
            automation_mode="detect-confirm",
            store=self.store,
        )

        self.assertEqual(result["candidates"], 1)
        self.assertEqual(len(self.store.list_pricing_versions(platform_key="lingsuan.top", status="candidate")), 1)

    def test_auto_apply_requires_two_matching_observations(self):
        self.seed_active()
        changed = self.observation(base_input_per_1m=11)

        first = app.process_pricing_observations(
            "lingsuan.top", self.profile, [changed], automation_mode="auto-apply", store=self.store
        )
        second = app.process_pricing_observations(
            "lingsuan.top", self.profile, [changed], automation_mode="auto-apply", store=self.store
        )

        self.assertEqual(first["applied"], 0)
        self.assertEqual(second["applied"], 1)
        self.assertEqual(self.store.get_active_pricing_version("api-lingsuan", "gpt-5.4")["input_per_1m"], 5.5)

    def test_large_change_stays_candidate_even_after_two_observations(self):
        self.seed_active()
        changed = self.observation(base_input_per_1m=30)

        app.process_pricing_observations(
            "lingsuan.top", self.profile, [changed], automation_mode="auto-apply", store=self.store
        )
        result = app.process_pricing_observations(
            "lingsuan.top", self.profile, [changed], automation_mode="auto-apply", store=self.store
        )

        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["requires_confirmation"], 1)
        self.assertEqual(self.store.get_active_pricing_version("api-lingsuan", "gpt-5.4")["input_per_1m"], 5)

    def test_adapter_pricing_catalog_requires_manifest_capability(self):
        payload = {"pricing_catalog": [self.observation(), "invalid-row"]}

        blocked = app.pricing_catalog_from_adapter_payload(payload, {"capabilities": ["fetch_quota"]})
        allowed = app.pricing_catalog_from_adapter_payload(
            payload, {"capabilities": ["fetch_quota", "fetch_pricing"]}
        )

        self.assertEqual(blocked, [])
        self.assertEqual(allowed, [self.observation()])


if __name__ == "__main__":
    unittest.main()
