import json
import sys
import tempfile
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

from credential_vault import CredentialVault  # noqa: E402


class CredentialVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.temp_dir.name) / "credential-vault.json"
        self.vault = CredentialVault(self.vault_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_round_trip_encrypts_credentials_without_plaintext_on_disk(self):
        credentials = {
            "login_email": "quota@example.com",
            "login_password": "correct horse battery staple",
            "auth_token": "secret-token-value",
        }

        status = self.vault.replace("lingsuan.top", credentials)

        self.assertEqual(self.vault.get("lingsuan.top"), credentials)
        self.assertEqual(status["configured_fields"], sorted(credentials))
        raw = self.vault_path.read_text(encoding="utf-8")
        self.assertNotIn(credentials["login_email"], raw)
        self.assertNotIn(credentials["login_password"], raw)
        self.assertNotIn(credentials["auth_token"], raw)
        stored = json.loads(raw)
        self.assertEqual(stored["version"], 1)
        self.assertIn("protected_data", stored["suppliers"]["lingsuan.top"])

    def test_merge_preserves_existing_fields_and_removes_blank_updates(self):
        self.vault.replace("lingsuan.top", {"login_email": "quota@example.com", "auth_token": "old"})

        status = self.vault.merge("lingsuan.top", {"auth_token": "new", "session_cookie": "session=abc", "login_password": ""})

        self.assertEqual(
            self.vault.get("lingsuan.top"),
            {"login_email": "quota@example.com", "auth_token": "new", "session_cookie": "session=abc"},
        )
        self.assertEqual(status["configured_fields"], ["auth_token", "login_email", "session_cookie"])

    def test_delete_removes_supplier_credentials(self):
        self.vault.replace("lingsuan.top", {"auth_token": "secret"})

        self.assertTrue(self.vault.delete("lingsuan.top"))

        self.assertEqual(self.vault.get("lingsuan.top"), {})
        self.assertEqual(self.vault.status("lingsuan.top")["configured_fields"], [])

    def test_rejects_empty_supplier_key(self):
        with self.assertRaises(ValueError):
            self.vault.replace("", {"auth_token": "secret"})


if __name__ == "__main__":
    unittest.main()
