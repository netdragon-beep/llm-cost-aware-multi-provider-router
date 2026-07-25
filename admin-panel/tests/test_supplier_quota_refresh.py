import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

import app  # noqa: E402
from app import BalanceRefreshPayload, latest_balance_for_supplier_platform, supplier_quota_targets  # noqa: E402
from usage_quota_store import UsageQuotaStore  # noqa: E402


class SupplierQuotaRefreshTests(unittest.TestCase):
    def test_balance_mode_keeps_total_when_remaining_decreases(self):
        first = app.apply_billing_mode_snapshot(
            {"balance_total": 7919, "balance_remaining": 100},
            billing_mode="balance",
        )
        decreased = app.apply_billing_mode_snapshot(
            {"balance_total": 7919, "balance_remaining": 80},
            previous_snapshot=first,
            billing_mode="balance",
        )

        self.assertEqual(first["balance_total"], 100)
        self.assertEqual(first["balance_used"], 0)
        self.assertEqual(decreased["balance_total"], 100)
        self.assertEqual(decreased["balance_used"], 20)

    def test_balance_mode_ignores_upstream_total_and_uses_previous_remaining(self):
        previous = {
            "billing_mode": "balance",
            "balance_total": 7919,
            "balance_remaining": 7.3,
        }

        current = app.apply_billing_mode_snapshot(
            {"balance_total": 7919, "balance_remaining": 6.9},
            previous_snapshot=previous,
            billing_mode="balance",
        )

        self.assertAlmostEqual(current["balance_total"], 7.3)
        self.assertAlmostEqual(current["balance_used"], 0.4)

    def test_balance_mode_starts_new_total_when_remaining_increases(self):
        previous = {"billing_mode": "balance", "balance_total": 80, "balance_remaining": 20}

        refreshed = app.apply_billing_mode_snapshot(
            {"balance_remaining": 100},
            previous_snapshot=previous,
            billing_mode="balance",
        )

        self.assertEqual(refreshed["balance_total"], 100)
        self.assertEqual(refreshed["balance_used"], 0)

    def test_subscription_mode_preserves_period_and_provider_usage(self):
        snapshot = app.apply_billing_mode_snapshot(
            {
                "balance_total": 1000,
                "balance_used": 320,
                "balance_remaining": 680,
                "period_start": "2026-07-01T00:00:00+08:00",
                "period_end": "2026-07-31T23:59:59+08:00",
            },
            billing_mode="subscription",
        )

        self.assertEqual(snapshot["billing_mode"], "subscription")
        self.assertEqual(snapshot["balance_total"], 1000)
        self.assertEqual(snapshot["balance_used"], 320)
        self.assertEqual(snapshot["balance_remaining"], 680)
        self.assertEqual(snapshot["period_end"], "2026-07-31T23:59:59+08:00")

    def test_quota_items_keep_balance_and_subscription_as_separate_sources(self):
        items = app.normalize_quota_items(
            [
                {"id": "wallet", "type": "balance", "remaining": 6.9},
                {
                    "id": "monthly-plan",
                    "type": "subscription",
                    "total": 1000,
                    "used": 320,
                    "remaining": 680,
                    "period_end": "2026-07-31",
                },
            ],
            fallback_snapshot={"balance_remaining": 6.9},
            previous_snapshot={"quota_items": [{"id": "wallet", "remaining": 7.3}]},
        )

        self.assertEqual([item["id"] for item in items], ["wallet", "monthly-plan"])
        self.assertEqual(items[0]["billing_mode"], "balance")
        self.assertAlmostEqual(items[0]["balance_total"], 7.3)
        self.assertAlmostEqual(items[0]["balance_used"], 0.4)
        self.assertEqual(items[1]["billing_mode"], "subscription")
        self.assertEqual(items[1]["balance_total"], 1000)
        self.assertEqual(items[1]["balance_used"], 320)
        self.assertEqual(items[1]["period_end"], "2026-07-31")

    @staticmethod
    def lingsuan_state():
        return {
            "suppliers": [{"id": "supplier-a", "name": "LingSuan"}],
            "api_profiles": [
                {
                    "id": "api-a",
                    "supplier_id": "supplier-a",
                    "api_base": "https://lingsuan.top/v1",
                    "custom_llm_provider": "openai",
                    "enabled": True,
                }
            ],
            "supplier_quotas": {
                "lingsuan.top": {
                    "enabled": True,
                    "adapter": "lingsuan-web",
                    "portal_base": "https://lingsuan.top",
                    "currency": "CNY",
                }
            },
        }

    @staticmethod
    def autocode_state():
        return {
            "suppliers": [{"id": "supplier-auto", "name": "AutoCode"}],
            "api_profiles": [
                {
                    "id": "api-auto",
                    "supplier_id": "supplier-auto",
                    "api_base": "https://auto-code.net/v1",
                    "custom_llm_provider": "openai",
                    "enabled": True,
                }
            ],
            "supplier_quotas": {
                "auto-code.net": {
                    "enabled": True,
                    "adapter": "autocode-web",
                    "portal_base": "https://auto-code.net",
                    "currency": "CNY",
                }
            },
        }

    def test_supplier_http_client_forces_ipv4_transport(self):
        with (
            patch.object(app.httpx, "HTTPTransport") as transport_factory,
            patch.object(app.httpx, "Client") as client_factory,
        ):
            transport = transport_factory.return_value

            app.supplier_http_client(timeout=20)

        transport_factory.assert_called_once_with(local_address="0.0.0.0")
        client_factory.assert_called_once_with(timeout=20, follow_redirects=True, transport=transport)

    def test_multiple_api_profiles_create_one_supplier_refresh_target(self):
        state = {
            "suppliers": [
                {"id": "supplier-a", "name": "LingSuan Claude"},
                {"id": "supplier-b", "name": "LingSuan Codex"},
            ],
            "api_profiles": [
                {"id": "api-a", "supplier_id": "supplier-a", "api_base": "https://lingsuan.top/v1", "enabled": True},
                {"id": "api-b", "supplier_id": "supplier-b", "api_base": "https://lingsuan.top/v1", "enabled": True},
            ],
            "supplier_quotas": {
                "lingsuan.top": {"enabled": True, "adapter": "lingsuan-web", "currency": "CNY"}
            },
        }

        targets = supplier_quota_targets(state)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["platform_key"], "lingsuan.top")
        self.assertEqual([profile["id"] for profile in targets[0]["profiles"]], ["api-a", "api-b"])
        self.assertEqual(targets[0]["quota"]["adapter"], "lingsuan-web")

    def test_api_profile_filter_still_refreshes_supplier_only_once(self):
        state = {
            "suppliers": [{"id": "supplier-a", "name": "LingSuan"}],
            "api_profiles": [
                {"id": "api-a", "supplier_id": "supplier-a", "api_base": "https://lingsuan.top/v1"},
                {"id": "api-b", "supplier_id": "supplier-a", "api_base": "https://lingsuan.top/v1"},
            ],
            "supplier_quotas": {"lingsuan.top": {"enabled": True}},
        }

        targets = supplier_quota_targets(state, {"api-b"})

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["selected_profile_ids"], ["api-b"])

    def test_disabled_automatic_monitoring_skips_background_probe(self):
        state = self.lingsuan_state()
        state["supplier_quotas"]["lingsuan.top"]["enabled"] = False
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()
            with (
                patch.object(app, "load_management_state", return_value=state),
                patch.object(app, "get_usage_quota_store", return_value=store),
                patch.object(app, "get_credential_vault"),
                patch.object(app, "probe_portal_quota") as probe,
            ):
                result = app.refresh_balances_v2(BalanceRefreshPayload(force=False))

        self.assertEqual(result["results"][0]["status"], "disabled")
        self.assertIn("自动额度监控已关闭", result["results"][0]["message"])
        probe.assert_not_called()

    def test_manual_refresh_probes_when_automatic_monitoring_is_disabled(self):
        state = self.lingsuan_state()
        state["supplier_quotas"]["lingsuan.top"]["enabled"] = False
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()

            class Vault:
                def get(self, supplier_key):
                    return {"auth_token": "stored-token"}

                def merge(self, supplier_key, updates):
                    return None

            with (
                patch.object(app, "load_management_state", return_value=state),
                patch.object(app, "get_usage_quota_store", return_value=store),
                patch.object(app, "get_credential_vault", return_value=Vault()),
                patch.object(
                    app,
                    "probe_portal_quota",
                    return_value={
                        "status": "ok",
                        "message": "",
                        "adapter": "lingsuan-web",
                        "currency": "CNY",
                        "balance_remaining": 12.5,
                    },
                ) as probe,
            ):
                result = app.refresh_balances_v2(BalanceRefreshPayload(force=True))

        probe.assert_called_once()
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][0]["balance_remaining"], 12.5)

    def test_balance_increase_during_refresh_creates_one_pending_recharge_batch(self):
        state = self.lingsuan_state()
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()

            class Vault:
                def get(self, supplier_key):
                    return {"auth_token": "stored-token"}

                def merge(self, supplier_key, updates):
                    return None

            with (
                patch.object(app, "load_management_state", return_value=state),
                patch.object(app, "get_usage_quota_store", return_value=store),
                patch.object(app, "get_credential_vault", return_value=Vault()),
                patch.object(
                    app,
                    "probe_portal_quota",
                    side_effect=[
                        {
                            "status": "ok",
                            "adapter": "lingsuan-web",
                            "currency": "USD",
                            "balance_remaining": 7.3,
                        },
                        {
                            "status": "ok",
                            "adapter": "lingsuan-web",
                            "currency": "USD",
                            "balance_remaining": 37.3,
                        },
                        {
                            "status": "ok",
                            "adapter": "lingsuan-web",
                            "currency": "USD",
                            "balance_remaining": 37.3,
                        },
                    ],
                ),
            ):
                first = app.refresh_balances_v2(BalanceRefreshPayload(force=True))
                second = app.refresh_balances_v2(BalanceRefreshPayload(force=True))
                third = app.refresh_balances_v2(BalanceRefreshPayload(force=True))

            self.assertIsNone(first["results"][0]["recharge_event"])
            self.assertTrue(second["results"][0]["recharge_event_created"])
            self.assertEqual(second["results"][0]["recharge_event"]["balance_delta"], 30.0)
            self.assertFalse(third["results"][0]["recharge_event_created"])
            self.assertEqual(len(store.list_recharge_records(limit=10)), 1)

    def test_background_refresh_respects_automatic_monitoring_switch(self):
        with patch.object(app, "refresh_balances_v2") as refresh:
            app.refresh_balances_in_background()

        payload = refresh.call_args.args[0]
        self.assertFalse(payload.force)

    def test_unknown_supplier_does_not_run_generic_quota_probes(self):
        state = {
            "suppliers": [{"id": "supplier-x", "name": "Unknown"}],
            "api_profiles": [
                {
                    "id": "api-x",
                    "supplier_id": "supplier-x",
                    "api_base": "https://unknown.example/v1",
                    "api_key_value": "configured-key",
                    "custom_llm_provider": "openai",
                    "enabled": True,
                }
            ],
            "supplier_quotas": {
                "unknown.example": {
                    "enabled": True,
                    "adapter": "openai-compatible",
                    "limit": 100,
                    "current_balance": 90,
                }
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()
            with (
                patch.object(app, "load_management_state", return_value=state),
                patch.object(app, "get_usage_quota_store", return_value=store),
                patch.object(app, "get_credential_vault"),
                patch.object(app, "probe_portal_quota") as portal_probe,
                patch.object(app, "probe_custom_quota_script") as script_probe,
            ):
                result = app.refresh_balances_v2(BalanceRefreshPayload(force=True))

        self.assertEqual(result["results"][0]["status"], "unsupported")
        self.assertIn("未安装额度适配器", result["results"][0]["message"])
        portal_probe.assert_not_called()
        script_probe.assert_not_called()
        self.assertFalse(hasattr(app, "probe_openai_compatible_balance"))

    def test_latest_error_keeps_last_successful_balance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()
            provider_key = app.supplier_platform_provider_key("lingsuan.top")
            store.insert_provider_balance_snapshot(
                {
                    "checked_at": "2026-07-19T10:00:00+08:00",
                    "provider_key": provider_key,
                    "adapter": "lingsuan-web",
                    "status": "ok",
                    "currency": "CNY",
                    "balance_remaining": 28.6,
                }
            )
            store.insert_provider_balance_snapshot(
                {
                    "checked_at": "2026-07-19T10:05:00+08:00",
                    "provider_key": provider_key,
                    "adapter": "lingsuan-web",
                    "status": "error",
                    "currency": "CNY",
                    "raw_summary": {"message": "HTTP 401"},
                }
            )

            latest = latest_balance_for_supplier_platform(store, "lingsuan.top", provider_key=provider_key)

        self.assertEqual(latest["status"], "error")
        self.assertEqual(latest["balance_remaining"], 28.6)
        self.assertTrue(latest["stale_balance"])

    def test_latest_balance_uses_previous_remaining_as_balance_total(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()
            provider_key = app.supplier_platform_provider_key("lingsuan.top")
            store.insert_provider_balance_snapshot(
                {
                    "checked_at": "2026-07-21T09:08:41Z",
                    "provider_key": provider_key,
                    "adapter": "lingsuan-web",
                    "status": "ok",
                    "billing_mode": "balance",
                    "balance_total": 7919,
                    "balance_remaining": 7.3,
                }
            )
            store.insert_provider_balance_snapshot(
                {
                    "checked_at": "2026-07-21T09:13:45Z",
                    "provider_key": provider_key,
                    "adapter": "lingsuan-web",
                    "status": "ok",
                    "billing_mode": "balance",
                    "balance_total": 7919,
                    "balance_remaining": 6.9,
                }
            )

            latest = app.latest_balance_for_supplier_platform(store, "lingsuan.top", provider_key=provider_key)

        self.assertAlmostEqual(latest["balance_total"], 7.3)
        self.assertAlmostEqual(latest["balance_used"], 0.4)

    def test_auth_failure_runs_one_silent_sso_renewal_and_retries_with_new_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()

            class Vault:
                credentials = {"auth_token": "expired-token"}

                def get(self, supplier_key):
                    return dict(self.credentials)

                def merge(self, supplier_key, updates):
                    self.credentials.update(updates)

            vault = Vault()
            renewal_calls = []

            def renew(supplier_key, portal_base, **kwargs):
                renewal_calls.append((supplier_key, portal_base, kwargs.get("adapter", {}).get("id")))
                vault.credentials = {"auth_token": "fresh-token"}
                return {"status": "authenticated", "requires_interaction": False}

            probe_results = [
                {"status": "error", "message": "HTTP 401 unauthorized", "adapter": "lingsuan-web"},
                {
                    "status": "ok",
                    "message": "",
                    "adapter": "lingsuan-web",
                    "currency": "CNY",
                    "balance_total": 30,
                    "balance_used": 8,
                    "balance_remaining": 22,
                },
            ]

            with (
                patch.object(app, "load_management_state", return_value=self.lingsuan_state()),
                patch.object(app, "get_usage_quota_store", return_value=store),
                patch.object(app, "get_credential_vault", return_value=vault),
                patch.object(app, "probe_portal_quota", side_effect=probe_results) as probe,
            ):
                result = app.refresh_balances_v2(BalanceRefreshPayload(force=True), silent_sso_refresher=renew)

        self.assertEqual(renewal_calls, [("lingsuan.top", "https://lingsuan.top", "lingsuan-web")])
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(probe.call_args_list[1].args[0]["quota"]["auth_token"], "fresh-token")
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][0]["balance_remaining"], 22)

    def test_non_auth_failure_does_not_start_browser_renewal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()

            class Vault:
                def get(self, supplier_key):
                    return {"auth_token": "token"}

                def merge(self, supplier_key, updates):
                    return None

            renewal_calls = []
            with (
                patch.object(app, "load_management_state", return_value=self.lingsuan_state()),
                patch.object(app, "get_usage_quota_store", return_value=store),
                patch.object(app, "get_credential_vault", return_value=Vault()),
                patch.object(
                    app,
                    "probe_portal_quota",
                    return_value={"status": "error", "message": "HTTP 500 upstream failure", "adapter": "lingsuan-web"},
                ) as probe,
            ):
                app.refresh_balances_v2(
                    BalanceRefreshPayload(force=True),
                    silent_sso_refresher=lambda *args, **kwargs: renewal_calls.append((args, kwargs)),
                )

        self.assertEqual(renewal_calls, [])
        self.assertEqual(probe.call_count, 1)

    def test_interaction_required_does_not_retry_or_replace_previous_balance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()
            provider_key = app.supplier_platform_provider_key("lingsuan.top")
            store.insert_provider_balance_snapshot(
                {
                    "checked_at": "2026-07-18T10:00:00+08:00",
                    "provider_key": provider_key,
                    "adapter": "lingsuan-web",
                    "status": "ok",
                    "currency": "CNY",
                    "balance_remaining": 9.5,
                }
            )

            class Vault:
                def get(self, supplier_key):
                    return {"auth_token": "expired-token"}

                def merge(self, supplier_key, updates):
                    return None

            with (
                patch.object(app, "load_management_state", return_value=self.lingsuan_state()),
                patch.object(app, "get_usage_quota_store", return_value=store),
                patch.object(app, "get_credential_vault", return_value=Vault()),
                patch.object(
                    app,
                    "probe_portal_quota",
                    return_value={"status": "error", "message": "HTTP 403 forbidden", "adapter": "lingsuan-web"},
                ) as probe,
            ):
                result = app.refresh_balances_v2(
                    BalanceRefreshPayload(force=True),
                    silent_sso_refresher=lambda *args, **kwargs: {
                        "status": "interaction_required",
                        "requires_interaction": True,
                        "message": "需要重新授权",
                    },
                )

            latest = latest_balance_for_supplier_platform(store, "lingsuan.top", provider_key=provider_key)

        self.assertEqual(probe.call_count, 1)
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertIn("重新授权", result["results"][0]["message"])
        self.assertEqual(latest["balance_remaining"], 9.5)
        self.assertTrue(latest["stale_balance"])

    def test_autocode_auth_failure_uses_the_same_silent_browser_sso_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = UsageQuotaStore(Path(temp_dir) / "usage.db")
            store.initialize()

            class Vault:
                credentials = {"auth_token": "expired-auto-token"}

                def get(self, supplier_key):
                    return dict(self.credentials)

                def merge(self, supplier_key, updates):
                    self.credentials.update(updates)

            vault = Vault()
            renewal_calls = []

            def renew(supplier_key, portal_base, **kwargs):
                renewal_calls.append((supplier_key, portal_base, kwargs["adapter"]["id"]))
                vault.credentials = {"auth_token": "fresh-auto-token"}
                return {"status": "authenticated", "requires_interaction": False}

            with (
                patch.object(app, "load_management_state", return_value=self.autocode_state()),
                patch.object(app, "get_usage_quota_store", return_value=store),
                patch.object(app, "get_credential_vault", return_value=vault),
                patch.object(
                    app,
                    "probe_portal_quota",
                    side_effect=[
                        {"status": "error", "message": "HTTP 401 unauthorized", "adapter": "autocode-web"},
                        {
                            "status": "ok",
                            "message": "",
                            "adapter": "autocode-web",
                            "currency": "CNY",
                            "balance_remaining": 18,
                        },
                    ],
                ) as probe,
            ):
                result = app.refresh_balances_v2(BalanceRefreshPayload(force=True), silent_sso_refresher=renew)

        self.assertEqual(renewal_calls, [("auto-code.net", "https://auto-code.net", "autocode-web")])
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(result["results"][0]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
