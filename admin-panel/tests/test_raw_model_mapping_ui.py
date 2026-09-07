import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "admin-panel"))

import app  # noqa: E402


INDEX_HTML = ROOT / "admin-panel" / "static" / "index.html"


class RawModelMappingBackendTests(unittest.TestCase):
    def test_state_text_repair_restores_display_and_model_fields_without_touching_api_keys(self):
        repaired, changed = app.repair_utf8_mojibake_in_state({
            "suppliers": [{"name": "ç\x81µç®claude", "label": "ç\x81µç®claude"}],
            "api_profiles": [{"label": "ç\x81µç®claude", "known_models": ["æºè°±GLM5.1"], "api_key_value": "ç³»ç»-key"}],
            "model_routes": [{"public_model_name": "claude-fable-5-ç ´ç²"}],
        })

        self.assertTrue(changed)
        self.assertEqual(repaired["suppliers"][0]["name"], "灵算claude")
        self.assertEqual(repaired["api_profiles"][0]["known_models"], ["智谱GLM5.1"])
        self.assertEqual(repaired["model_routes"][0]["public_model_name"], "claude-fable-5-破甲")
        self.assertEqual(repaired["api_profiles"][0]["api_key_value"], "ç³»ç»-key")

    def test_model_family_normalization_repairs_utf8_mojibake_and_deduplicates(self):
        repaired = app.normalize_model_families([
            "Claude 系列",
            "Claude ç³»å\x88\x97",
            "GPT ç³»å\x88\x97",
        ])

        self.assertEqual(repaired, ["Claude 系列", "GPT 系列"])
        routes = app.normalize_model_routes([
            {"id": "route-claude", "public_model_name": "claude-fable-5", "model_family": "Claude ç³»å\x88\x97"},
        ])
        self.assertEqual(routes[0]["model_family"], "Claude 系列")

    def test_public_model_name_allows_unicode_and_common_separator_characters(self):
        routes = app.normalize_model_routes([
            {"id": "route-unicode", "public_model_name": "claude-fable-5-破甲"},
            {"id": "route-slash", "public_model_name": "openai/gpt-5.4"},
            {"id": "route-space", "public_model_name": "GLM-5.1 high"},
        ])
        self.assertEqual([item["public_model_name"] for item in routes], ["claude-fable-5-破甲", "openai/gpt-5.4", "GLM-5.1 high"])

        with self.assertRaisesRegex(ValueError, "公共模型名称"):
            app.normalize_model_routes([
                {"id": "route-invalid", "public_model_name": "bad\u0000model"},
            ])

        routes = app.normalize_model_routes([
            {"id": "route-valid", "public_model_name": "claude-fable-5:priority_1.0"},
        ])
        self.assertEqual(routes[0]["public_model_name"], "claude-fable-5:priority_1.0")

    def test_same_api_and_raw_model_keeps_one_active_binding_and_marks_conflict(self):
        profiles = [{"id": "api-1", "supplier_id": "supplier-1", "label": "Example"}]
        routes = [
            {"id": "route-a", "public_model_name": "claude-primary"},
            {"id": "route-b", "public_model_name": "claude-backup"},
        ]
        bindings = app.normalize_route_bindings(
            [
                {"id": "binding-a", "model_route_id": "route-a", "api_profile_id": "api-1", "upstream_model": "claude-fable-5", "priority": 20, "enabled": True},
                {"id": "binding-b", "model_route_id": "route-b", "api_profile_id": "api-1", "upstream_model": "claude-fable-5", "priority": 10, "enabled": True},
            ],
            profiles,
            routes,
        )

        active = [item for item in bindings if item["enabled"]]
        conflicts = [item for item in bindings if item.get("mapping_conflict")]
        self.assertEqual([item["id"] for item in active], ["binding-b"])
        self.assertEqual([item["id"] for item in conflicts], ["binding-a"])


class RawModelMappingUiTests(unittest.TestCase):
    def test_raw_model_rows_expose_family_filtered_mapping_and_original_name_shortcut(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("模型系列", page)
        self.assertIn("映射到公共模型", page)
        self.assertIn("直接使用原始模型名", page)
        self.assertIn("待映射", page)
        self.assertIn("data-raw-model-family", page)
        self.assertIn("rawModelRouteOptions", page)

    def test_raw_model_mapping_fields_keep_full_select_lists_and_offer_separate_creation_inputs(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn('<select data-raw-model-family', page)
        self.assertIn('<select data-raw-model-route', page)
        self.assertIn('data-raw-model-family-custom', page)
        self.assertIn('data-raw-model-route-custom', page)
        self.assertIn('rawModelFamilyOptions', page)
        self.assertIn('rawModelRouteOptions', page)
        self.assertIn('findOrCreateRawModelRoute', page)
        self.assertIn('createRawModelFamilyFromInput', page)
        self.assertIn('createRawModelRouteFromInput', page)

    def test_raw_model_mapping_creation_inputs_prefill_the_current_family_and_public_model_name(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn('data-raw-model-family-custom', page)
        self.assertIn('value="${escapeHtml(family)}"', page)
        self.assertIn('const suggestedPublicModelName = String(activeRoute?.public_model_name || rawModel || \'\').trim();', page)
        self.assertIn('value="${escapeHtml(suggestedPublicModelName)}"', page)

    def test_mapping_ui_replaces_multiple_locate_actions_with_conflict_cleanup_state(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("历史映射冲突", page)
        self.assertIn("整理为当前选择", page)
        self.assertNotIn('data-action="locate-public-model"', page)

    def test_model_sync_uses_floating_notice_for_pending_success_empty_and_failures(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("正在请求供应商模型列表", page)
        self.assertIn("同步完成，但未发现模型", page)
        self.assertIn("同步完成：发现", page)
        self.assertIn("formatModelSyncError", page)
        self.assertIn("showFloatingActionNotice", page)

    def test_api_model_list_offers_batch_public_model_suffixing(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn('data-action="apply-api-model-suffix"', page)
        self.assertIn('data-action="remove-api-model-suffix"', page)
        self.assertIn('data-api-model-suffix', page)
        self.assertIn('applyApiModelSuffix', page)
        self.assertIn('removeApiModelSuffix', page)
        self.assertIn('已批量加入后缀并保存路由草稿', page)
        self.assertIn('已批量移除后缀', page)

    def test_batch_model_suffixing_reports_progress_and_save_results_in_floating_notice(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("正在批量加入后缀", page)
        self.assertIn("已批量加入后缀并保存路由草稿", page)
        self.assertIn("批量加入后缀但保存路由草稿失败", page)
        self.assertIn("showFloatingActionNotice", page)

    def test_public_model_id_inputs_validate_and_report_errors_in_the_floating_status(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("validatePublicModelName", page)
        self.assertIn("validatePublicModelSuffix", page)
        self.assertIn("公共模型名称不能包含控制字符", page)
        self.assertIn("setRoutingSaveStatus(validation.message, 'warn', true)", page)

    def test_publish_and_pin_immediately_applies_and_verifies_gateway_models(self):
        page = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("verifyPublishedGatewayModel", page)
        self.assertIn("await saveRoutingDraft({ silent: false })", page)
        self.assertIn("已发布并已在网关模型列表中确认", page)


if __name__ == "__main__":
    unittest.main()
