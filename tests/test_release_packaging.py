import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGING_DIR = ROOT / "packaging"


def load_packaging_module(name: str):
    module_path = PACKAGING_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleasePackagingTests(unittest.TestCase):
    def test_payload_builder_copies_application_files_without_local_state(self):
        build_payload = load_packaging_module("build_payload")

        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source"
            payload = Path(temporary_directory) / "payload"
            (source / "scripts").mkdir(parents=True)
            (source / "admin-panel").mkdir()
            (source / "config").mkdir()
            (source / "data").mkdir()
            (source / "logs").mkdir()
            (source / "run").mkdir()
            (source / "scripts" / "start-all.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (source / "admin-panel" / "app.py").write_text("app = object()\n", encoding="utf-8")
            (source / "config" / "litellm.example.yaml").write_text("model_list: []\n", encoding="utf-8")
            (source / "config" / "litellm.yaml").write_text("secret: local\n", encoding="utf-8")
            (source / "requirements.txt").write_text("fastapi==0.1\n", encoding="utf-8")
            (source / ".env.example").write_text("EXAMPLE=value\n", encoding="utf-8")
            (source / ".env").write_text("SECRET=value\n", encoding="utf-8")
            (source / "data" / "credential-vault.json").write_text("secret\n", encoding="utf-8")
            (source / "logs" / "service.log").write_text("log\n", encoding="utf-8")
            (source / "run" / "service.pid").write_text("1\n", encoding="utf-8")

            build_payload.copy_payload(source, payload)

            self.assertTrue((payload / "scripts" / "start-all.sh").is_file())
            self.assertTrue((payload / "admin-panel" / "app.py").is_file())
            self.assertTrue((payload / "config" / "litellm.example.yaml").is_file())
            self.assertTrue((payload / "requirements.txt").is_file())
            self.assertFalse((payload / ".env").exists())
            self.assertFalse((payload / "config" / "litellm.yaml").exists())
            self.assertFalse((payload / "data").exists())
            self.assertFalse((payload / "logs").exists())
            self.assertFalse((payload / "run").exists())

    def test_launcher_uses_packaged_runtime_and_platform_script(self):
        launcher = load_packaging_module("launcher")
        packaged_root = Path("/Applications/RelayDeck.app/Contents/Resources/app")

        mac_environment = launcher.runtime_environment(packaged_root, is_windows=False)
        self.assertEqual(mac_environment["RELAYDECK_ENV_ROOT"], str(packaged_root / "runtime"))
        self.assertEqual(
            launcher.command_for(packaged_root, is_windows=False, install_browser_runtime=False),
            ["/bin/sh", str(packaged_root / "scripts" / "start-all.sh")],
        )
        self.assertEqual(
            launcher.command_for(packaged_root, is_windows=False, install_browser_runtime=True),
            ["/bin/sh", str(packaged_root / "scripts" / "install-browser-runtime.sh")],
        )

    def test_launcher_bootstraps_config_from_templates_without_overwriting_state(self):
        launcher = load_packaging_module("launcher")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config"
            config.mkdir()
            (config / "litellm.yaml.example").write_text("model_list: []\n", encoding="utf-8")
            (config / "relaydeck-state.example.json").write_text("{}\n", encoding="utf-8")
            (config / "relaydeck-state.json").write_text('{"providers": ["existing"]}\n', encoding="utf-8")

            launcher.bootstrap_configuration(root)

            self.assertEqual((config / "litellm.yaml").read_text(encoding="utf-8"), "model_list: []\n")
            self.assertEqual((config / "relaydeck-state.json").read_text(encoding="utf-8"), '{"providers": ["existing"]}\n')

    def test_release_workflow_and_packaging_metadata_declare_required_assets(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        installer = (PACKAGING_DIR / "windows" / "RelayDeck.iss").read_text(encoding="utf-8")
        mac_builder = (PACKAGING_DIR / "build_macos.sh").read_text(encoding="utf-8")

        self.assertIn("windows-latest", workflow)
        self.assertIn("macos-13", workflow)
        self.assertIn("macos-14", workflow)
        self.assertIn("RelayDeck-Setup-x64.exe", workflow)
        self.assertIn("RelayDeck-x64.dmg", workflow)
        self.assertIn("RelayDeck-arm64.dmg", workflow)
        self.assertIn("SHA256SUMS.txt", workflow)
        self.assertIn("RELEASE_NOTES.md", workflow)
        self.assertIn("push:", workflow)
        self.assertIn("tags:", workflow)
        self.assertIn("release:", workflow)
        self.assertIn("OutputBaseFilename=RelayDeck-Setup-x64", installer)
        self.assertIn("DefaultDirName={localappdata}\\RelayDeck Local", installer)
        self.assertIn("hdiutil create", mac_builder)


if __name__ == "__main__":
    unittest.main()
