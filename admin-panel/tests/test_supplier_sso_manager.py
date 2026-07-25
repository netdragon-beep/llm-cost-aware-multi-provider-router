import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ADMIN_PANEL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADMIN_PANEL_DIR))

from supplier_sso import (  # noqa: E402
    SsoRunResult,
    SsoTaskActiveError,
    SupplierSsoManager,
    normalize_supplier_key,
)


def wait_for_terminal(manager, supplier_key, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = manager.status(supplier_key)
        if status["status"] not in {"starting", "waiting_for_user", "validating"}:
            return status
        time.sleep(0.01)
    raise AssertionError(f"SSO task did not finish: {manager.status(supplier_key)}")


class SupplierSsoManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.profile_root = Path(self.temp_dir.name) / "browser-sessions"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_normalizes_supplier_key_and_rejects_path_values(self):
        self.assertEqual(normalize_supplier_key(" LingSuan.TOP "), "lingsuan.top")
        for value in ("", "../lingsuan.top", "lingsuan/top", "lingsuan\\top"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_supplier_key(value)

    def test_successful_task_sends_credentials_to_sink_but_never_exposes_them(self):
        saved = {}

        def worker(request, cancel_event, update_status):
            update_status("validating", "正在验证供应商登录")
            return SsoRunResult(
                status="authenticated",
                message="登录有效",
                credentials={"auth_token": "top-secret-token", "session_cookie": "session=secret"},
            )

        manager = SupplierSsoManager(
            self.profile_root,
            worker=worker,
            credential_sink=lambda key, credentials: saved.update({key: credentials}),
        )

        started = manager.start("lingsuan.top", "https://lingsuan.top", interactive=True)
        finished = wait_for_terminal(manager, "lingsuan.top")

        self.assertIn(started["status"], {"starting", "validating", "authenticated"})
        self.assertEqual(finished["status"], "authenticated")
        self.assertEqual(saved["lingsuan.top"]["auth_token"], "top-secret-token")
        self.assertNotIn("credentials", finished)
        self.assertNotIn("top-secret-token", str(finished))
        self.assertNotIn("session=secret", str(finished))

    def test_rejects_second_active_task_for_same_supplier(self):
        release = threading.Event()

        def worker(request, cancel_event, update_status):
            update_status("waiting_for_user", "等待用户登录")
            release.wait(1)
            return SsoRunResult(status="cancelled", message="已结束")

        manager = SupplierSsoManager(self.profile_root, worker=worker)
        manager.start("lingsuan.top", "https://lingsuan.top")

        with self.assertRaises(SsoTaskActiveError):
            manager.start("lingsuan.top", "https://lingsuan.top")

        release.set()
        wait_for_terminal(manager, "lingsuan.top")

    def test_cancel_signals_worker_and_keeps_existing_profile(self):
        def worker(request, cancel_event, update_status):
            update_status("waiting_for_user", "等待用户登录")
            cancel_event.wait(1)
            return SsoRunResult(status="cancelled", message="登录已取消")

        manager = SupplierSsoManager(self.profile_root, worker=worker)
        manager.start("lingsuan.top", "https://lingsuan.top")
        profile_dir = manager.profile_path("lingsuan.top")
        profile_dir.mkdir(parents=True)
        (profile_dir / "profile.marker").write_text("keep", encoding="utf-8")

        cancelled = manager.cancel("lingsuan.top")
        finished = wait_for_terminal(manager, "lingsuan.top")

        self.assertTrue(cancelled)
        self.assertEqual(finished["status"], "cancelled")
        self.assertTrue((profile_dir / "profile.marker").exists())

    def test_complete_interactive_login_signals_waiting_worker(self):
        completion_seen = threading.Event()

        def worker(request, cancel_event, update_status):
            update_status("waiting_for_user", "等待用户完成系统浏览器登录")
            if request.completion_event.wait(1):
                completion_seen.set()
                update_status("validating", "正在验证供应商登录")
                return SsoRunResult(status="authenticated", credentials={"auth_token": "fresh"})
            return SsoRunResult(status="timed_out")

        manager = SupplierSsoManager(self.profile_root, worker=worker)
        manager.start("lingsuan.top", "https://lingsuan.top", interactive=True)
        deadline = time.time() + 1
        while manager.status("lingsuan.top")["status"] == "starting" and time.time() < deadline:
            time.sleep(0.01)

        completed = manager.complete_interactive("lingsuan.top")
        finished = wait_for_terminal(manager, "lingsuan.top")

        self.assertTrue(completed)
        self.assertTrue(completion_seen.is_set())
        self.assertEqual(finished["status"], "authenticated")

    def test_complete_interactive_login_rejects_missing_or_silent_task(self):
        manager = SupplierSsoManager(self.profile_root)
        self.assertFalse(manager.complete_interactive("lingsuan.top"))

    def test_clear_session_removes_only_supplier_profile_and_rejects_active_task(self):
        release = threading.Event()

        def worker(request, cancel_event, update_status):
            update_status("waiting_for_user", "等待用户登录")
            release.wait(1)
            return SsoRunResult(status="cancelled")

        manager = SupplierSsoManager(self.profile_root, worker=worker)
        profile_dir = manager.profile_path("lingsuan.top")
        sibling_dir = manager.profile_path("other.example")
        profile_dir.mkdir(parents=True)
        sibling_dir.mkdir(parents=True)
        (profile_dir / "profile.marker").write_text("delete", encoding="utf-8")
        (sibling_dir / "profile.marker").write_text("keep", encoding="utf-8")
        manager.start("lingsuan.top", "https://lingsuan.top")

        with self.assertRaises(SsoTaskActiveError):
            manager.clear_session("lingsuan.top")

        release.set()
        wait_for_terminal(manager, "lingsuan.top")
        self.assertTrue(manager.clear_session("lingsuan.top"))
        self.assertFalse(profile_dir.exists())
        self.assertTrue(sibling_dir.exists())

    def test_mark_interrupted_converts_active_tasks_without_exposing_internal_state(self):
        release = threading.Event()

        def worker(request, cancel_event, update_status):
            update_status("waiting_for_user", "等待用户登录")
            release.wait(1)
            return SsoRunResult(status="cancelled")

        manager = SupplierSsoManager(self.profile_root, worker=worker)
        manager.start("lingsuan.top", "https://lingsuan.top")

        manager.mark_active_tasks_interrupted()
        status = manager.status("lingsuan.top")

        self.assertEqual(status["status"], "interrupted")
        self.assertTrue(status["requires_interaction"])
        release.set()

    def test_run_silent_waits_for_worker_and_returns_authenticated_status(self):
        def worker(request, cancel_event, update_status):
            self.assertFalse(request.interactive)
            update_status("validating", "正在静默续期")
            return SsoRunResult(status="authenticated", credentials={"auth_token": "fresh"})

        saved = {}
        manager = SupplierSsoManager(
            self.profile_root,
            worker=worker,
            credential_sink=lambda key, credentials: saved.update({key: credentials}),
        )
        manager.profile_path("lingsuan.top").mkdir(parents=True)

        status = manager.run_silent("lingsuan.top", "https://lingsuan.top", timeout_seconds=1)

        self.assertEqual(status["status"], "authenticated")
        self.assertEqual(saved["lingsuan.top"], {"auth_token": "fresh"})

    def test_run_silent_returns_existing_active_task_without_starting_another_worker(self):
        release = threading.Event()
        calls = []

        def worker(request, cancel_event, update_status):
            calls.append(request.interactive)
            update_status("waiting_for_user", "等待用户登录")
            release.wait(1)
            return SsoRunResult(status="cancelled")

        manager = SupplierSsoManager(self.profile_root, worker=worker)
        manager.start("lingsuan.top", "https://lingsuan.top", interactive=True)
        deadline = time.time() + 1
        while manager.status("lingsuan.top")["status"] == "starting" and time.time() < deadline:
            time.sleep(0.01)

        status = manager.run_silent("lingsuan.top", "https://lingsuan.top", timeout_seconds=1)

        self.assertIn(status["status"], {"starting", "waiting_for_user"})
        self.assertTrue(status["existing_task"])
        self.assertEqual(calls, [True])
        release.set()


if __name__ == "__main__":
    unittest.main()
