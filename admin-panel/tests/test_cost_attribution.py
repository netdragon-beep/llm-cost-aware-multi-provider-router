import importlib
import sys
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))
COST_MODULE_PATH = ADMIN_PANEL_DIR / "cost_attribution.py"


@unittest.skipUnless(COST_MODULE_PATH.exists(), "cost attribution module not implemented")
class CostAttributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("cost_attribution")

    def test_weighted_topup_basis_includes_matching_bonus_quota(self):
        records = [
            {
                "billing_type": "topup",
                "paid_amount_cny": 30,
                "credited_amount": 90,
                "credited_currency": "USD",
                "bonus_amount": 10,
                "bonus_currency": "USD",
            },
            {
                "billing_type": "topup",
                "paid_amount_cny": 20,
                "credited_amount": 40,
                "credited_currency": "USD",
                "bonus_amount": 10,
                "bonus_currency": "QUOTA",
            },
            {
                "billing_type": "topup",
                "paid_amount_cny": 999,
                "credited_amount": 999,
                "credited_currency": "EUR",
            },
        ]

        result = self.module.weighted_topup_basis(records, "USD")

        self.assertEqual(result["status"], "available")
        self.assertAlmostEqual(result["paid_cny"], 50)
        self.assertAlmostEqual(result["credits"], 150)
        self.assertAlmostEqual(result["cash_per_credit_cny"], 1 / 3)

    def test_weighted_topup_basis_reports_currency_mismatch(self):
        result = self.module.weighted_topup_basis(
            [
                {
                    "billing_type": "topup",
                    "paid_amount_cny": 30,
                    "credited_amount": 90,
                    "credited_currency": "EUR",
                }
            ],
            "USD",
        )

        self.assertEqual(result["status"], "currency-mismatch")
        self.assertIsNone(result["cash_per_credit_cny"])

    def test_subscription_amortization_counts_both_end_dates(self):
        record = {
            "billing_type": "subscription",
            "paid_amount_cny": 30,
            "service_start": "2026-07-01",
            "service_end": "2026-07-30",
        }

        result = self.module.subscription_amortization(
            [record], "2026-07-10T00:00:00+08:00", "2026-07-12T23:59:59+08:00"
        )

        self.assertAlmostEqual(result["daily_cny"], 1)
        self.assertAlmostEqual(result["amortized_cny"], 3)
        self.assertEqual(result["covered_days"], 3)

    def test_subscription_daily_cost_is_allocated_by_price_weight(self):
        events = [
            {
                "id": "event-a",
                "created_at": "2026-07-10T08:00:00+08:00",
                "estimated_cost": 1,
                "currency": "USD",
                "total_tokens": 100,
                "status": "success",
            },
            {
                "id": "event-b",
                "created_at": "2026-07-10T09:00:00+08:00",
                "estimated_cost": 3,
                "currency": "USD",
                "total_tokens": 100,
                "status": "success",
            },
        ]
        records = [
            {
                "billing_type": "subscription",
                "paid_amount_cny": 30,
                "service_start": "2026-07-01",
                "service_end": "2026-07-30",
            }
        ]

        result = self.module.attribute_supplier_costs(
            events,
            records,
            "2026-07-10T00:00:00+08:00",
            "2026-07-11T23:59:59+08:00",
        )

        self.assertAlmostEqual(result["event_costs"]["event-a"]["subscription_cny"], 0.25)
        self.assertAlmostEqual(result["event_costs"]["event-b"]["subscription_cny"], 0.75)
        self.assertAlmostEqual(result["subscription_amortized_cny"], 2)
        self.assertAlmostEqual(result["subscription_allocated_cny"], 1)
        self.assertAlmostEqual(result["subscription_unallocated_cny"], 1)

    def test_topup_cost_applies_only_outside_active_subscription_dates(self):
        events = [
            {
                "id": "subscription-event",
                "created_at": "2026-07-10T08:00:00+08:00",
                "estimated_cost": 2,
                "currency": "USD",
                "total_tokens": 100,
                "status": "success",
            },
            {
                "id": "topup-event",
                "created_at": "2026-08-01T08:00:00+08:00",
                "estimated_cost": 2,
                "currency": "USD",
                "total_tokens": 100,
                "status": "success",
            },
        ]
        records = [
            {
                "billing_type": "subscription",
                "paid_amount_cny": 30,
                "service_start": "2026-07-01",
                "service_end": "2026-07-30",
            },
            {
                "billing_type": "topup",
                "paid_amount_cny": 30,
                "credited_amount": 90,
                "credited_currency": "USD",
            },
        ]

        result = self.module.attribute_supplier_costs(
            events,
            records,
            "2026-07-10T00:00:00+08:00",
            "2026-08-01T23:59:59+08:00",
        )

        self.assertEqual(result["event_costs"]["subscription-event"]["source"], "subscription")
        self.assertEqual(result["event_costs"]["topup-event"]["source"], "topup")
        self.assertAlmostEqual(result["event_costs"]["topup-event"]["topup_cny"], 2 / 3)

    def test_topup_cost_uses_credit_batches_and_skips_unpaid_batches(self):
        events = [
            {
                "id": "event-a",
                "created_at": "2026-07-01T08:00:00+08:00",
                "estimated_cost": 10,
                "currency": "USD",
                "total_tokens": 1000,
                "status": "success",
            },
            {
                "id": "event-b",
                "created_at": "2026-07-02T08:00:00+08:00",
                "estimated_cost": 4,
                "currency": "USD",
                "total_tokens": 400,
                "status": "success",
            },
        ]
        records = [
            {
                "id": "batch-a",
                "billing_type": "topup",
                "paid_amount_cny": 30,
                "credited_amount": 10,
                "credited_currency": "USD",
                "paid_at": "2026-07-01T00:00:00+08:00",
            },
            {
                "id": "batch-b",
                "billing_type": "topup",
                "paid_amount_cny": None,
                "credited_amount": 20,
                "credited_currency": "USD",
                "paid_at": "2026-07-02T00:00:00+08:00",
            },
        ]

        result = self.module.attribute_supplier_costs(
            events,
            records,
            "2026-07-01T00:00:00+08:00",
            "2026-07-03T00:00:00+08:00",
        )

        self.assertEqual(result["event_costs"]["event-a"]["source"], "topup")
        self.assertAlmostEqual(result["event_costs"]["event-a"]["topup_cny"], 30)
        self.assertEqual(result["event_costs"]["event-b"]["status"], "missing-paid")
        self.assertEqual(result["unattributed_requests"], 1)

    def test_subscription_usage_metrics_reports_realized_and_full_use_costs(self):
        result = self.module.subscription_usage_metrics(
            {
                "billing_type": "subscription",
                "paid_amount_cny": 30,
                "service_start": "2026-07-01",
                "service_end": "2026-07-31",
            },
            {
                "id": "monthly-plan",
                "type": "subscription",
                "total": 1000,
                "used": 100,
                "remaining": 900,
                "period_end": "2026-07-31",
            },
            as_of="2026-07-10",
        )

        self.assertEqual(result["status"], "active")
        self.assertAlmostEqual(result["consumed_cost_cny"], 3)
        self.assertAlmostEqual(result["full_use_cost_per_credit_cny"], 0.03)
        self.assertAlmostEqual(result["projected_expiry_loss_cny"], 20.7)
        self.assertAlmostEqual(result["utilization_ratio"], 0.1)

    def test_subscription_usage_metrics_marks_unused_cost_after_expiry(self):
        result = self.module.subscription_usage_metrics(
            {
                "billing_type": "subscription",
                "paid_amount_cny": 30,
                "service_start": "2026-07-01",
                "service_end": "2026-07-31",
            },
            {"type": "subscription", "total": 1000, "used": 600, "remaining": 400},
            as_of="2026-08-01",
        )

        self.assertEqual(result["status"], "expired")
        self.assertAlmostEqual(result["consumed_cost_cny"], 18)
        self.assertAlmostEqual(result["confirmed_expiry_loss_cny"], 12)

    def test_subscription_usage_metrics_keeps_missing_payment_as_pending(self):
        result = self.module.subscription_usage_metrics(
            {
                "billing_type": "subscription",
                "paid_amount_cny": 0,
                "service_start": "2026-07-01",
                "service_end": "2026-07-31",
            },
            {"type": "subscription", "total": 1000, "used": 100, "remaining": 900},
            as_of="2026-07-10",
        )

        self.assertEqual(result["data_status"], "missing-paid")
        self.assertIsNone(result["consumed_cost_cny"])

    def test_subscription_usage_metrics_supports_month_two_month_and_year_periods(self):
        periods = [
            ("2026-07-01", "2026-07-31", 31),
            ("2026-07-01", "2026-08-31", 62),
            ("2026-01-01", "2026-12-31", 365),
        ]
        for start, end, expected_days in periods:
            with self.subTest(start=start, end=end):
                result = self.module.subscription_usage_metrics(
                    {
                        "billing_type": "subscription",
                        "paid_amount_cny": 30,
                        "service_start": start,
                        "service_end": end,
                    },
                    {"type": "subscription", "total": 1000, "used": 100, "remaining": 900},
                    as_of=start,
                )
                self.assertEqual(result["period_days"], expected_days)


class CostAttributionModuleContractTests(unittest.TestCase):
    def test_cost_attribution_module_exists(self):
        self.assertTrue(COST_MODULE_PATH.exists())


if __name__ == "__main__":
    unittest.main()
