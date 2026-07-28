import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))


class MacOSPlatformSupportTests(unittest.TestCase):
    def test_macos_runtime_uses_bin_tools_and_shell_scripts(self):
        from platform_runtime import RuntimePaths

        runtime = RuntimePaths.from_platform(
            root=Path("/Applications/RelayDeck"),
            platform_name="darwin",
            env_root=Path("/opt/relaydeck/.venv"),
            python_executable=Path("/opt/relaydeck/.venv/bin/python"),
        )

        self.assertEqual(runtime.tool_path("litellm"), Path("/opt/relaydeck/.venv/bin/litellm"))
        self.assertEqual(runtime.script_path("start-litellm"), Path("/Applications/RelayDeck/scripts/start-litellm.sh"))
        self.assertEqual(runtime.claude_code_settings_path(Path("/Users/relay")), Path("/Users/relay/.claude/settings.json"))

    def test_macos_keychain_vault_keeps_secret_out_of_local_document(self):
        from credential_vault import CredentialVault

        with tempfile.TemporaryDirectory() as directory:
            vault_path = Path(directory) / "credential-vault.json"
            calls = []

            def fake_security(arguments, *, input_text=None):
                calls.append((arguments, input_text))
                if arguments[0] == "find-generic-password":
                    return json.dumps({"auth_token": "mac-secret"})
                return ""

            vault = CredentialVault(vault_path, platform_name="darwin", security_runner=fake_security)
            status = vault.replace("lingsuan.top", {"auth_token": "mac-secret"})

            self.assertEqual(status["storage"], "macos-keychain")
            self.assertEqual(vault.get("lingsuan.top"), {"auth_token": "mac-secret"})
            self.assertNotIn("mac-secret", vault_path.read_text(encoding="utf-8"))
            self.assertEqual(calls[0][0][0], "add-generic-password")
            self.assertIn("find-generic-password", [call[0][0] for call in calls])


if __name__ == "__main__":
    unittest.main()
