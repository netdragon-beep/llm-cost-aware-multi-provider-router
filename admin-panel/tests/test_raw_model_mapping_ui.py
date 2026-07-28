import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "admin-panel"))

import app  # noqa: E402


INDEX_HTML = ROOT / "admin-panel" / "static" / "index.html"


class RawModelMappingBackendTests(unittest.TestCase):
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

    def test_mapping_ui_replaces_multiple_locate_actions_with_conflict_cleanup_state(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("历史映射冲突", page)
        self.assertIn("整理为当前选择", page)
        self.assertNotIn('data-action="locate-public-model"', page)

    def test_model_sync_uses_floating_notice_for_pending_success_empty_and_failures(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("正在同步模型列表", page)
        self.assertIn("同步完成，但未发现模型", page)
        self.assertIn("同步完成：发现", page)
        self.assertIn("formatModelSyncError", page)
        self.assertIn("showFloatingActionNotice", page)


if __name__ == "__main__":
    unittest.main()
