import copy
import sys
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

from app import (  # noqa: E402
    adapter_scoped_quota,
    api_base_mismatch_hint,
    build_direct_request,
    migrate_legacy_supplier_quota_state,
    provider_models_probe,
    resolve_supplier_quota_adapter,
    split_quota_credentials,
    supplier_quota_targets,
)


class FakeVault:
    def __init__(self):
        self.values = {}

    def merge(self, supplier_key, updates):
        self.values.setdefault(supplier_key, {}).update({key: value for key, value in updates.items() if value})
        return {"configured_fields": sorted(self.values[supplier_key])}


class SupplierQuotaStateTests(unittest.TestCase):
    def test_anthropic_probe_does_not_duplicate_v1_when_api_base_already_has_it(self):
        url, _ = provider_models_probe(
            {
                "custom_llm_provider": "anthropic",
                "api_base": "https://lingsuan.top/v1",
                "api_key_env": "LING_KEY",
            },
            {"LING_KEY": "test-key"},
        )
        self.assertEqual(url, "https://lingsuan.top/v1/models")

    def test_anthropic_messages_request_does_not_duplicate_v1_when_api_base_already_has_it(self):
        url, _, _ = build_direct_request(
            api_base="https://lingsuan.top/v1",
            api_key="test-key",
            custom_llm_provider="anthropic",
            upstream_model="claude-sonnet",
            prompt="hello",
            max_tokens=8,
        )
        self.assertEqual(url, "https://lingsuan.top/v1/messages")

    def test_anthropic_404_hint_keeps_model_family_independent_from_api_protocol(self):
        hint = api_base_mismatch_hint(
            "anthropic",
            "https://example.test",
            status_code=404,
        )

        self.assertIn("API 分组", hint)
        self.assertIn("模型家族", hint)
        self.assertIn("openai", hint)

    def test_builtin_supplier_hosts_resolve_without_user_selection(self):
        self.assertEqual(resolve_supplier_quota_adapter("lingsuan.top")["adapter"], "lingsuan-web")
        self.assertEqual(resolve_supplier_quota_adapter("api.lingsuan.top")["adapter"], "lingsuan-web")
        self.assertEqual(resolve_supplier_quota_adapter("auto-code.net")["adapter"], "autocode-web")

    def test_script_adapter_is_bound_by_manifest_supported_hosts(self):
        resolution = resolve_supplier_quota_adapter(
            "quota.example.com",
            adapter_scripts=[
                {
                    "name": "example.py",
                    "manifest": {
                        "id": "example-provider",
                        "supported_hosts": ["example.com"],
                    },
                }
            ],
        )

        self.assertTrue(resolution["supported"])
        self.assertEqual(resolution["adapter"], "custom-script")
        self.assertEqual(resolution["script_name"], "example.py")

    def test_unknown_supplier_is_explicitly_unsupported(self):
        resolution = resolve_supplier_quota_adapter("unknown.example", adapter_scripts=[])

        self.assertFalse(resolution["supported"])
        self.assertEqual(resolution["adapter"], "unsupported")

    def test_targets_override_stale_user_selected_adapter(self):
        state = {
            "suppliers": [{"id": "supplier-1", "name": "LingSuan"}],
            "api_profiles": [
                {
                    "id": "api-1",
                    "supplier_id": "supplier-1",
                    "api_base": "https://lingsuan.top/v1",
                    "enabled": True,
                }
            ],
            "supplier_quotas": {
                "lingsuan.top": {
                    "enabled": True,
                    "adapter": "openai-compatible",
                    "limit": 999,
                }
            },
        }

        target = supplier_quota_targets(state)[0]

        self.assertEqual(target["quota"]["adapter"], "lingsuan-web")
        self.assertNotIn("limit", target["quota"])
    def test_adapter_receives_only_declared_credential_fields(self):
        quota = {
            "adapter": "custom-script",
            "currency": "CNY",
            "auth_token": "allowed-token",
            "session_cookie": "blocked-cookie",
            "login_password": "blocked-password",
        }

        scoped = adapter_scoped_quota(quota, {"credential_permissions": ["auth_token"]})

        self.assertEqual(scoped, {"adapter": "custom-script", "currency": "CNY", "auth_token": "allowed-token"})

    def test_split_quota_credentials_keeps_only_non_sensitive_configuration(self):
        public, credentials = split_quota_credentials(
            {
                "enabled": True,
                "adapter": "lingsuan-web",
                "currency": "CNY",
                "auth_token": "secret-token",
                "session_cookie": "session=secret",
                "login_email": "quota@example.com",
                "login_password": "secret-password",
            }
        )

        self.assertEqual(public, {"enabled": True, "adapter": "lingsuan-web", "currency": "CNY"})
        self.assertEqual(
            credentials,
            {
                "auth_token": "secret-token",
                "session_cookie": "session=secret",
                "login_email": "quota@example.com",
                "login_password": "secret-password",
            },
        )

    def test_legacy_api_quotas_merge_into_one_supplier_platform(self):
        original = {
            "suppliers": [
                {"id": "supplier-claude", "name": "LingSuan Claude"},
                {"id": "supplier-codex", "name": "LingSuan Codex"},
            ],
            "api_profiles": [
                {
                    "id": "api-claude",
                    "supplier_id": "supplier-claude",
                    "api_base": "https://lingsuan.top/v1",
                    "quota": {
                        "enabled": True,
                        "adapter": "lingsuan-web",
                        "portal_base": "https://lingsuan.top",
                        "currency": "CNY",
                        "auth_token": "old-token",
                    },
                },
                {
                    "id": "api-codex",
                    "supplier_id": "supplier-codex",
                    "api_base": "https://lingsuan.top/v1",
                    "quota": {
                        "session_cookie": "session=old-cookie",
                        "login_email": "quota@example.com",
                    },
                },
            ],
            "supplier_quotas": {},
        }
        vault = FakeVault()

        migrated, changed = migrate_legacy_supplier_quota_state(copy.deepcopy(original), vault)

        self.assertTrue(changed)
        self.assertEqual(
            migrated["supplier_quotas"],
            {
                "lingsuan.top": {
                    "enabled": True,
                    "portal_base": "https://lingsuan.top",
                    "currency": "CNY",
                }
            },
        )
        self.assertEqual(vault.values["lingsuan.top"]["auth_token"], "old-token")
        self.assertEqual(vault.values["lingsuan.top"]["session_cookie"], "session=old-cookie")
        self.assertEqual(vault.values["lingsuan.top"]["login_email"], "quota@example.com")
        self.assertEqual([profile["quota"] for profile in migrated["api_profiles"]], [{}, {}])

    def test_existing_supplier_quota_wins_over_legacy_api_values(self):
        state = {
            "suppliers": [{"id": "supplier-1", "name": "LingSuan"}],
            "api_profiles": [
                {
                    "id": "api-1",
                    "supplier_id": "supplier-1",
                    "api_base": "https://lingsuan.top/v1",
                    "quota": {"currency": "USD", "auth_token": "legacy-token"},
                }
            ],
            "supplier_quotas": {"lingsuan.top": {"enabled": True, "currency": "CNY"}},
        }
        vault = FakeVault()

        migrated, _ = migrate_legacy_supplier_quota_state(copy.deepcopy(state), vault)

        self.assertEqual(migrated["supplier_quotas"]["lingsuan.top"]["currency"], "CNY")
        self.assertEqual(vault.values["lingsuan.top"]["auth_token"], "legacy-token")

    def test_migration_removes_obsolete_user_selected_quota_fields(self):
        state = {
            "suppliers": [{"id": "supplier-1", "name": "LingSuan"}],
            "api_profiles": [
                {"id": "api-1", "supplier_id": "supplier-1", "api_base": "https://lingsuan.top/v1"}
            ],
            "supplier_quotas": {
                "lingsuan.top": {
                    "enabled": True,
                    "adapter": "manual",
                    "currency": "CNY",
                    "portal_base": "https://lingsuan.top",
                    "limit": 100,
                    "current_balance": 20,
                    "used_amount": 80,
                    "user_id": "101265",
                    "admin_api_key": "obsolete-admin-key",
                    "script_name": "old.py",
                    "script_timeout_sec": 60,
                }
            },
        }

        vault = FakeVault()
        migrated, changed = migrate_legacy_supplier_quota_state(state, vault)

        self.assertTrue(changed)
        self.assertEqual(
            migrated["supplier_quotas"]["lingsuan.top"],
            {
                "enabled": True,
                "currency": "CNY",
                "portal_base": "https://lingsuan.top",
            },
        )
        self.assertEqual(vault.values, {})


if __name__ == "__main__":
    unittest.main()
