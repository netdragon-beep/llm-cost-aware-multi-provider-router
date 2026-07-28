import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / "admin-panel" / "static" / "index.html"


class ProtocolBoundaryUiTests(unittest.TestCase):
    def test_routing_screen_explains_client_and_upstream_protocol_boundaries(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("客户端兼容：OpenAI + Anthropic", page)
        self.assertIn("上游协议 Provider 类型", page)
        self.assertIn("由 API 分组决定，与模型家族无关", page)

    def test_binding_card_exposes_the_api_group_protocol_configuration_entry(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("上游协议：${escapeHtml(providerName)}", page)
        self.assertIn('data-action="open-supplier-api"', page)
        self.assertIn(">配置上游协议</button>", page)

    def test_model_card_shows_classification_prompt_feedback(self):
        page = INDEX_HTML.read_text(encoding="utf-8")

        self.assertIn("服务器分类提示词", page)
        self.assertIn("分类提示词已应用", page)
        self.assertIn("尚未应用分类提示词", page)


if __name__ == "__main__":
    unittest.main()
