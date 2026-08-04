import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "admin-panel"))
import app  # noqa: E402


class ClientIntegrationTests(unittest.TestCase):
    def test_appearance_theme_controls_are_fixed_and_local_only(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="appearance-theme-options"', source)
        self.assertIn("const APPEARANCE_THEME_STORAGE_KEY = 'relaydeck.appearance-theme';", source)
        self.assertIn("const APPEARANCE_THEMES =", source)
        self.assertIn("function renderAppearanceThemeOptions()", source)
        self.assertIn("function applyAppearanceTheme(themeId, persist = true)", source)
        self.assertIn("window.localStorage.setItem(APPEARANCE_THEME_STORAGE_KEY, theme.id);", source)
        self.assertIn("document.body.dataset.appearanceTheme = theme.id;", source)
        self.assertIn("type=\"button\"", source)
        self.assertIn("aria-pressed", source)
        self.assertIn("button.dataset.appearanceThemeId = theme.id;", source)
        for theme_id in ("morning", "mist", "deep-space", "ink", "forest", "amber"):
            self.assertIn(f"id: '{theme_id}'", source)
        self.assertIn("\u6668\u767d", source)
        self.assertIn("\u96fe\u7070", source)
        self.assertIn("\u6df1\u7a7a", source)
        self.assertIn("\u58a8\u9ed1", source)
        self.assertIn("\u68ee\u7eff", source)
        self.assertIn("\u7425\u73c0", source)

    def test_appearance_themes_keep_semantic_alert_colors_and_avoid_gateway_save_flow(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertEqual(source.count("--warn: #ffcb6b;"), 1)
        self.assertEqual(source.count("--danger: #ff7a7a;"), 1)
        appearance_start = source.index("const APPEARANCE_THEME_STORAGE_KEY")
        appearance_end = source.index("function readAutoRefreshBalancesPreference", appearance_start)
        appearance_code = source[appearance_start:appearance_end]
        self.assertNotIn("saveRoutingDraft", appearance_code)
        self.assertNotIn("/api/", appearance_code)

    def test_gateway_model_lists_render_each_model_as_a_row(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("function renderClientGatewayModelList", source)
        self.assertIn("client-gateway-model-row", source)
        self.assertIn("target.appendChild(row)", source)

    def test_non_codex_client_sync_has_visible_progress_and_outcome_feedback(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("async function applyClientIntegration(clientName, triggerButton = null)", source)
        self.assertIn("正在同步 ${label} 配置…", source)
        self.assertIn("showFloatingActionNotice(`正在同步 ${label} 配置…`, 'saving', { persistent: true })", source)
        self.assertIn("showFloatingActionNotice(message, 'ok', { durationMs: 9000 })", source)
        self.assertIn("applyClientIntegration(button.dataset.clientIntegration, button)", source)

    def test_client_integration_uses_local_brand_icon_assets(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")
        asset_dir = ROOT / "admin-panel" / "static" / "assets" / "client-icons"

        self.assertIn('/static/assets/client-icons/codex.svg', source)
        self.assertIn('/static/assets/client-icons/opencode.svg', source)
        self.assertIn('/static/assets/client-icons/claude-code.svg', source)
        for name in ("codex.svg", "opencode.svg", "claude-code.svg"):
            self.assertTrue((asset_dir / name).is_file())

    def test_model_mapping_controls_are_scoped_to_codex_and_claude_cards(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-client-shortcut-client="codex"', source)
        self.assertIn('data-client-shortcut-client="claude_code"', source)
        self.assertIn('>模型对应映射</button>', source)
        self.assertNotIn('id="btn-manage-client-shortcuts"', source)
        self.assertIn('let clientShortcutSlots = {};', source)
        self.assertIn('data-claude-shortcut-row', source)
        self.assertIn('新增模型映射', source)
        self.assertIn('data-claude-shortcut-name', source)
        self.assertNotIn("data-claude-shortcut-tier", source)
        self.assertIn("会自动识别类型", source)

    def test_routing_relationship_panel_is_between_tools_and_routing_list(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        tools_index = source.index('id="model-route-tools"')
        panel_index = source.index('id="routing-relationship-panel"')
        list_index = source.index('id="provider-list"')
        self.assertLess(tools_index, panel_index)
        self.assertLess(panel_index, list_index)
        self.assertIn('class="routing-relationship-panel"', source)
        self.assertIn('aria-live="polite"', source)

    def test_routing_relationship_panel_has_selection_and_renderer_contracts(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")
        renderer_start = source.index("function renderRoutingRelationshipPanel()")
        renderer = source[renderer_start:source.index("function statusLabelText", renderer_start)]

        self.assertIn("relationshipSelection: { mode: 'model', routeId: '', apiProfileId: '' }", source)
        self.assertIn("function ensureRoutingRelationshipSelection()", source)
        self.assertIn("function renderRoutingRelationshipPanel()", source)
        self.assertIn("renderRoutingRelationshipPanel();", source)
        self.assertIn("bindingsForRoute(route.id)", renderer)
        self.assertIn("supplierDisplayName(supplier)", renderer)
        self.assertIn("profile.label || profile.id", renderer)
        self.assertIn("binding.upstream_model", renderer)
        self.assertIn("binding.priority", renderer)
        self.assertIn("bindingNetworkStatus", renderer)
        self.assertIn("relationshipQuotaHint(supplier)", renderer)
        self.assertIn("bindingsForApiProfile(profile.id)", renderer)
        self.assertIn("route.public_model_name", renderer)
        self.assertIn("route.gateway_enabled", renderer)
        self.assertIn("这个 API 还没有关联公共模型", renderer)
        self.assertIn("这个公共模型还没有上游绑定", renderer)

    def test_supplier_relationship_panel_only_shows_published_routes(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")
        renderer_start = source.index("function renderRoutingRelationshipPanel()")
        renderer = source[renderer_start:source.index("function statusLabelText", renderer_start)]

        self.assertRegex(
            renderer,
            r"const publishedBindings = bindings\.filter\(binding => \{\s*const route = routeById\(binding\.model_route_id\) \|\| \{\};\s*return route\.gateway_enabled && route\.enabled !== false;\s*\}\);",
        )
        self.assertIn("这个 API 还没有关联公共模型", renderer)
        self.assertIn("这个 API 的所有公共模型关联均未发布或已停用，请发布或启用路由后再试", renderer)

    def test_routing_relationship_panel_focus_action_is_wired(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")
        action_start = source.index("if (action === 'focus-relationship-target')")
        focus_action = source[action_start:source.index("if (action === 'activate-pricing-version'", action_start)]
        focus_restore_start = source.index("function restorePendingRelationshipFocus()")
        focus_restore = source[focus_restore_start:source.index("function saveRoutingDraft", focus_restore_start)]
        render_start = source.index("function renderRouting()")
        render_routing = source[render_start:source.index("function renderEnv()", render_start)]
        media_start = source.index("@media (max-width: 1120px)")
        media_block = source[media_start:source.index("</style>", media_start)]

        self.assertIn('data-action="focus-relationship-target"', source)
        self.assertIn("button.dataset.relationshipMode", focus_action)
        self.assertIn("state.routingViewMode = 'by_model'", focus_action)
        self.assertIn("state.routingViewMode = 'by_supplier'", focus_action)
        self.assertIn("data-route-group-id", focus_restore)
        self.assertIn("data-api-card-id", focus_restore)
        self.assertIn("scrollIntoView({ behavior: 'smooth', block: 'center' })", focus_restore)
        self.assertEqual(focus_action.count("renderRouting();"), 2)
        self.assertNotRegex(
            focus_action,
            r"(?:state\.(?:modelRoutes|routeBindings|apiProfiles|suppliers)(?:\s*(?:\[[^\]]*\]|\.\w+))*\s*(?:=(?!=)|[+\-*/%]=|\+\+|--)|state\.(?:modelRoutes|routeBindings|apiProfiles|suppliers)\s*\.\s*(?:push|pop|shift|unshift|splice|sort|reverse|copyWithin|fill)\s*\(|(?:Object\.assign|Reflect\.set)\(\s*state\.(?:modelRoutes|routeBindings|apiProfiles|suppliers))",
        )
        self.assertLess(render_routing.index("renderRoutingRelationshipPanel();"), render_routing.index("bindRoutingInputs();"))
        self.assertRegex(
            media_block,
            r"\.relationship-panel-head\s*,\s*\.relationship-row\s*\{[^}]*flex-direction:\s*column\s*;",
        )

    def test_relationship_panel_focus_actions_rebind_after_panel_refresh(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")
        renderer_start = source.index("function renderRoutingRelationshipPanel()")
        renderer = source[renderer_start:source.index("function statusLabelText", renderer_start)]
        binder_start = source.index("function bindRoutingRelationshipPanelActions()")
        binder = source[binder_start:source.index("function bindRoutingInputs()", binder_start)]
        routing_binder_start = source.index("function bindRoutingInputs()")
        routing_binder = source[routing_binder_start:source.index("function envRowTemplate", routing_binder_start)]

        self.assertIn("function bindRoutingRelationshipPanelActions()", source)
        self.assertIn('panel.querySelectorAll(\'[data-action="focus-relationship-target"]\')', binder)
        self.assertIn("button.addEventListener('click', handleRoutingAction);", binder)
        self.assertGreaterEqual(renderer.count("bindRoutingRelationshipPanelActions();"), 2)
        self.assertIn("if (button.closest('#routing-relationship-panel')) return;", routing_binder)

    def test_relationship_focus_target_restores_open_card_after_layout_snapshot(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")
        focus_start = source.index("function restorePendingRelationshipFocus()")
        focus_restore = source[focus_start:source.index("function saveRoutingDraft", focus_start)]
        action_start = source.index("if (action === 'focus-relationship-target')")
        focus_action = source[action_start:source.index("if (action === 'activate-pricing-version'", action_start)]
        render_start = source.index("function renderRouting()")
        render_routing = source[render_start:source.index("function renderEnv()", render_start)]

        self.assertIn("pendingRelationshipFocus: null", source)
        self.assertIn("state.pendingRelationshipFocus = { mode: 'model', targetId: routeId };", focus_action)
        self.assertIn(
            "state.pendingRelationshipFocus = { mode: 'supplier', targetId: apiProfileId };",
            focus_action,
        )
        self.assertIn("card.open = true;", focus_restore)
        self.assertIn("platformCard.open = true;", focus_restore)
        self.assertIn("const familyCard = card.closest('.fold-card');", focus_restore)
        self.assertIn("if (familyCard) familyCard.open = true;", focus_restore)
        self.assertIn("card.scrollIntoView({ behavior: 'smooth', block: 'center' });", focus_restore)
        self.assertLess(render_routing.index("restoreRoutingLayout();"), render_routing.index("restorePendingRelationshipFocus();"))

    def test_relationship_focus_target_is_consumed_when_filtered_out(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")
        focus_start = source.index("function restorePendingRelationshipFocus()")
        focus_restore = source[focus_start:source.index("function saveRoutingDraft", focus_start)]

        self.assertIn(
            "if (!card) {\n        state.pendingRelationshipFocus = null;\n        return;\n      }",
            focus_restore,
        )

    def test_published_models_excludes_unpublished_and_marks_visual_models(self):
        models = app.published_client_models(
            {
                "model_routes": [
                    {"public_model_name": "gpt-5.6-terra", "gateway_enabled": True, "enabled": True},
                    {"public_model_name": "draft-model", "gateway_enabled": False, "enabled": True},
                    {"public_model_name": "disabled-model", "gateway_enabled": True, "enabled": False},
                ]
            }
        )

        self.assertEqual(models, [{"id": "gpt-5.6-terra", "display_name": "gpt-5.6-terra", "supports_image": True}])

    def test_legacy_claude_slots_migrate_to_editable_model_alias_rows(self):
        shortcuts = app.normalize_client_shortcuts(
            {
                "codex": {"gpt-5.6-sol": "gpt-5.6-sol"},
                "claude_code": {"claude-haiku": "", "claude-sonnet": "gpt-5.6-terra"},
            }
        )

        self.assertEqual(
            shortcuts["claude_code"],
            [
                {"name": "claude-haiku", "tier": "haiku", "target": ""},
                {"name": "claude-sonnet", "tier": "sonnet", "target": "gpt-5.6-terra"},
            ],
        )

    def test_editable_claude_aliases_preserve_the_fable_tier(self):
        shortcuts = app.normalize_client_shortcuts(
            {"claude_code": [{"name": "claude-fable-5", "tier": "fable", "target": "claude-fable-5"}]}
        )

        self.assertEqual(shortcuts["claude_code"][0]["tier"], "fable")

    def test_claude_alias_tier_is_inferred_from_name_not_submitted_type(self):
        shortcuts = app.normalize_client_shortcuts(
            {"claude_code": [{"name": "claude-opus-5", "tier": "sonnet", "target": "gpt-5.6-terra"}]}
        )

        self.assertEqual(shortcuts["claude_code"][0]["tier"], "opus")
        self.assertEqual(app.infer_claude_shortcut_tier("custom-relay"), "sonnet")

    def test_claude_mapping_ui_hides_client_type_selector(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertNotIn("data-claude-shortcut-tier", source)
        self.assertIn("会自动识别类型", source)

    def test_codex_configuration_preserves_other_providers_and_writes_catalog(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            catalog_path = root / "relaydeck-model-catalog.json"
            config_path.write_text('[model_providers.other]\nname = "Other"\n', encoding="utf-8")

            result = app.configure_codex_client(
                config_path,
                catalog_path,
                [{"id": "gpt-5.6-terra", "display_name": "Terra", "supports_image": True}],
                "http://127.0.0.1:4100/v1",
            )

            saved = config_path.read_text(encoding="utf-8")
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertIn('[model_providers.other]', saved)
            self.assertIn('[model_providers.relaydeck]', saved)
            self.assertIn('model_catalog_json', saved)
            self.assertEqual(catalog["models"][0]["slug"], "gpt-5.6-terra")
            self.assertEqual(catalog["models"][0]["input_modalities"], ["text", "image"])
            self.assertEqual(result["model_count"], 1)

    def test_codex_candidate_escapes_windows_catalog_path_for_toml(self):
        candidate = app.build_codex_config_content(
            "",
            Path(r"C:\Users\Administrator\.codex\relaydeck-model-catalog.json"),
            [{"id": "gpt-5.6-terra", "display_name": "Terra", "supports_image": True}],
            "http://127.0.0.1:4100/v1",
        )

        self.assertIn('model_catalog_json = "C:/Users/Administrator/.codex/relaydeck-model-catalog.json"', candidate)

    def test_codex_preview_candidate_does_not_mutate_config_until_apply(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.toml"
            catalog_path = root / "relaydeck-model-catalog.json"
            original = 'model_provider = "other"\n'
            config_path.write_text(original, encoding="utf-8")
            models = [{"id": "gpt-5.6-terra", "display_name": "Terra", "supports_image": True}]
            candidate = app.build_codex_config_content(original, catalog_path, models, "http://127.0.0.1:4100/v1")

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)
            result = app.apply_codex_config_candidate(config_path, catalog_path, candidate, models)

            self.assertTrue(Path(result["backup_path"]).is_file())
            self.assertIn('model_provider = "relaydeck"', config_path.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(catalog_path.read_text(encoding="utf-8"))["models"][0]["slug"], "gpt-5.6-terra")

    def test_codex_backup_restore_rejects_path_traversal_and_restores_valid_backup(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text('model = "first"\n', encoding="utf-8")
            backup = app.backup_file(config_path)
            config_path.write_text('model = "second"\n', encoding="utf-8")

            with self.assertRaises(ValueError):
                app.restore_codex_config_backup(config_path, "../config.toml")
            app.restore_codex_config_backup(config_path, backup.name)

            self.assertEqual(config_path.read_text(encoding="utf-8"), 'model = "first"\n')

    def test_claude_code_preview_redacts_token_and_apply_creates_backup(self):
        with TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text(
                json.dumps({"env": {"OTHER": "preserved", "ANTHROPIC_AUTH_TOKEN": "private-token"}}),
                encoding="utf-8",
            )
            candidate = app.build_claude_code_settings_content(
                settings_path.read_text(encoding="utf-8"), "http://127.0.0.1:4101"
            )

            self.assertNotIn("private-token", candidate)
            self.assertIn("<managed by RelayDeck when applied>", candidate)
            result = app.apply_claude_code_settings_candidate(
                settings_path, candidate, "http://127.0.0.1:4101", "gateway-token"
            )
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertTrue(Path(result["backup_path"]).is_file())
            self.assertEqual(saved["env"]["OTHER"], "preserved")
            self.assertEqual(saved["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:4101")
            self.assertEqual(saved["env"]["ANTHROPIC_AUTH_TOKEN"], "gateway-token")

    def test_claude_code_backup_restore_rejects_path_traversal(self):
        with TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            settings_path.write_text('{"env": {"MODE": "first"}}\n', encoding="utf-8")
            backup = app.backup_file(settings_path)
            settings_path.write_text('{"env": {"MODE": "second"}}\n', encoding="utf-8")

            with self.assertRaises(ValueError):
                app.restore_claude_code_settings_backup(settings_path, "../settings.json")
            app.restore_claude_code_settings_backup(settings_path, backup.name)

            self.assertEqual(json.loads(settings_path.read_text(encoding="utf-8"))["env"]["MODE"], "first")

    def test_claude_code_sync_uses_preview_modal_instead_of_direct_apply(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="claude-sync-preview-overlay"', source)
        self.assertIn("function showClaudeSyncPreview", source)
        self.assertIn("/api/client-integrations/claude-code/apply-preview", source)
        self.assertIn("? showClaudeSyncPreview()", source)

    def test_configuration_diffs_render_colored_additions_and_deletions(self):
        source = (ROOT / "admin-panel" / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn('class="config-diff" id="codex-sync-diff"', source)
        self.assertIn('class="config-diff" id="claude-sync-diff"', source)
        self.assertIn("function renderConfigDiff(targetId, diff)", source)
        self.assertIn("row.classList.add('addition')", source)
        self.assertIn("row.classList.add('deletion')", source)
        self.assertIn(".config-diff-line.addition", source)
        self.assertIn(".config-diff-line.deletion", source)

    def test_opencode_configuration_preserves_other_provider_and_sets_modalities(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "opencode.jsonc"
            config_path.write_text('{\n  // preserve config\n  "provider": {"other": {"name": "Other"}}\n}\n', encoding="utf-8")

            result = app.configure_opencode_client(
                config_path,
                [{"id": "gpt-5.6-terra", "display_name": "Terra", "supports_image": True}],
                "http://127.0.0.1:4100/v1",
                "test-key",
            )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            model = saved["provider"]["relaydeck"]["models"]["gpt-5.6-terra"]
            self.assertEqual(saved["provider"]["other"]["name"], "Other")
            self.assertEqual(model["modalities"]["input"], ["text", "image"])
            self.assertEqual(result["model_count"], 1)
            self.assertTrue(Path(result["backup_path"]).exists())


if __name__ == "__main__":
    unittest.main()
