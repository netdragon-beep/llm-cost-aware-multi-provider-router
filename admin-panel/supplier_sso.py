from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


ACTIVE_STATUSES = {"starting", "waiting_for_user", "validating"}
TERMINAL_STATUSES = {
    "authenticated",
    "interaction_required",
    "cancelled",
    "interrupted",
    "browser_missing",
    "timed_out",
    "error",
}


class SsoTaskActiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class SsoRunRequest:
    supplier_key: str
    portal_base: str
    profile_dir: Path
    interactive: bool
    timeout_seconds: int
    completion_event: threading.Event | None = None
    adapter: dict[str, Any] = field(default_factory=dict)


@dataclass
class SsoRunResult:
    status: str
    message: str = ""
    error_code: str = ""
    credentials: dict[str, str] = field(default_factory=dict)


@dataclass
class _Task:
    task_id: str
    supplier_key: str
    status: str
    message: str
    error_code: str
    interactive: bool
    created_at: str
    updated_at: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    completion_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


Worker = Callable[[SsoRunRequest, threading.Event, Callable[[str, str], None]], SsoRunResult]
CredentialSink = Callable[[str, dict[str, str]], Any]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_supplier_key(value: str) -> str:
    key = str(value or "").strip().lower()
    if not key or "/" in key or "\\" in key or ".." in key:
        raise ValueError("Invalid supplier key")
    if len(key) > 200:
        raise ValueError("Supplier key is too long")
    return key


def profile_path_for_supplier(profile_root: str | Path, supplier_key: str) -> Path:
    key = normalize_supplier_key(supplier_key)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
    return Path(profile_root).resolve() / digest


def _safe_message(message: str, credentials: dict[str, str] | None = None) -> str:
    safe = str(message or "")[:500]
    for secret in (credentials or {}).values():
        if secret:
            safe = safe.replace(str(secret), "[redacted]")
    return safe


BUILTIN_BROWSER_SSO_ADAPTERS: dict[str, dict[str, Any]] = {
    "lingsuan-web": {
        "id": "lingsuan-web",
        "display_name": "灵算",
        "supplier_key": "lingsuan.top",
        "portal_base": "https://lingsuan.top",
        "portal_hosts": ["lingsuan.top"],
        "login_path": "/dashboard",
        "auth_me_path": "/api/v1/auth/me",
        "cookie_domains": ["lingsuan.top"],
        "google_rejection_check": True,
    },
    "autocode-web": {
        "id": "autocode-web",
        "display_name": "AutoCode",
        "supplier_key": "auto-code.net",
        "portal_base": "https://vip.auto-code.net",
        "portal_hosts": ["vip.auto-code.net", "auto-code.net"],
        "login_path": "/dashboard",
        "auth_me_path": "/api/v1/auth/me",
        "cookie_domains": ["vip.auto-code.net", "auto-code.net"],
        "google_rejection_check": True,
    },
}


def browser_sso_adapter_for_supplier(supplier_key: str, adapter_id: str = "") -> dict[str, Any]:
    key = normalize_supplier_key(supplier_key)
    requested = str(adapter_id or "").strip().lower()
    for candidate in BUILTIN_BROWSER_SSO_ADAPTERS.values():
        if requested and str(candidate.get("id") or "").lower() != requested:
            continue
        if key == str(candidate.get("supplier_key") or "").lower() or key in {
            str(host).strip().lower() for host in candidate.get("portal_hosts", [])
        }:
            return dict(candidate)
    raise ValueError(f"Browser SSO is not configured for supplier {key}")


def is_browser_sso_auth_me_url(url: str, adapter: dict[str, Any]) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    hosts = {
        str(host).strip().lower().lstrip(".")
        for host in adapter.get("portal_hosts", [])
        if str(host).strip()
    }
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower().lstrip(".") in hosts
        and parsed.path.rstrip("/") == str(adapter.get("auth_me_path") or "/api/v1/auth/me").rstrip("/")
    )


def is_lingsuan_auth_me_url(url: str) -> bool:
    return is_browser_sso_auth_me_url(url, BUILTIN_BROWSER_SSO_ADAPTERS["lingsuan-web"])


def is_google_signin_rejected_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "accounts.google.com"
        and parsed.path.rstrip("/").endswith("/signin/rejected")
    )


def _system_browser_candidates() -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    for name, path in (
        ("Microsoft Edge", Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe"),
        ("Microsoft Edge", Path(program_files) / "Microsoft/Edge/Application/msedge.exe"),
        ("Microsoft Edge", Path(local_app_data) / "Microsoft/Edge/Application/msedge.exe"),
        ("Google Chrome", Path(program_files) / "Google/Chrome/Application/chrome.exe"),
        ("Google Chrome", Path(program_files_x86) / "Google/Chrome/Application/chrome.exe"),
        ("Google Chrome", Path(local_app_data) / "Google/Chrome/Application/chrome.exe"),
    ):
        candidates.append((name, path))
    return candidates


def find_system_browser(candidates: list[tuple[str, Path]] | None = None) -> dict[str, str]:
    for name, candidate in candidates or _system_browser_candidates():
        path = Path(candidate).expanduser()
        if path.is_file():
            return {"name": str(name), "executable_path": str(path.resolve())}
    return {}


def build_system_browser_command(executable_path: str | Path, profile_dir: str | Path, login_url: str) -> list[str]:
    return [
        str(executable_path),
        f"--user-data-dir={Path(profile_dir)}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--new-window",
        str(login_url),
    ]


def _stop_browser_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=3)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def extract_bearer_token(headers: dict[str, Any] | None) -> str:
    authorization = ""
    for key, value in (headers or {}).items():
        if str(key).strip().lower() == "authorization":
            authorization = str(value or "").strip()
            break
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return ""
    return token.strip()


def build_supplier_cookie_header(cookies: list[dict[str, Any]] | None, hostname: str) -> str:
    expected = str(hostname or "").strip().lower().lstrip(".")
    parts: list[str] = []
    for cookie in cookies or []:
        domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if domain != expected or not name or not value:
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


class BrowserCredentialCapture:
    def __init__(self, adapter: dict[str, Any]):
        self.adapter = dict(adapter)
        self._auth_token = ""
        self._auth_response_ok = False

    @property
    def authenticated(self) -> bool:
        return self._auth_response_ok

    def observe_response(self, url: str, status_code: int, request_headers: dict[str, Any] | None) -> None:
        if not is_browser_sso_auth_me_url(url, self.adapter) or not 200 <= int(status_code or 0) < 300:
            return
        self._auth_response_ok = True
        token = extract_bearer_token(request_headers)
        if token:
            self._auth_token = token

    def credentials(self, cookies: list[dict[str, Any]] | None = None) -> dict[str, str]:
        result: dict[str, str] = {}
        if self._auth_token:
            result["auth_token"] = self._auth_token
        for domain in self.adapter.get("cookie_domains", []):
            cookie_header = build_supplier_cookie_header(cookies, str(domain))
            if cookie_header:
                result["session_cookie"] = cookie_header
                break
        return result

    def __repr__(self) -> str:
        return f"<BrowserCredentialCapture adapter={self.adapter.get('id', '')} authenticated={self.authenticated}>"


class LingSuanCredentialCapture(BrowserCredentialCapture):
    def __init__(self):
        super().__init__(BUILTIN_BROWSER_SSO_ADAPTERS["lingsuan-web"])


def browser_runtime_status() -> dict[str, Any]:
    install_command = "python -m playwright install chromium"
    system_browser = find_system_browser()
    if importlib.util.find_spec("playwright") is None:
        return {
            "available": False,
            "interactive_available": False,
            "package_installed": False,
            "browser_installed": False,
            "system_browser_available": bool(system_browser),
            "system_browser_name": str(system_browser.get("name") or ""),
            "install_command": install_command,
            "message": "Playwright 尚未安装。",
        }
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable_path = Path(playwright.chromium.executable_path)
            browser_installed = executable_path.exists()
        return {
            "available": browser_installed,
            "interactive_available": browser_installed and bool(system_browser),
            "package_installed": True,
            "browser_installed": browser_installed,
            "system_browser_available": bool(system_browser),
            "system_browser_name": str(system_browser.get("name") or ""),
            "install_command": install_command,
            "message": (
                f"浏览器运行时可用，将使用 {system_browser['name']} 完成人工登录。"
                if browser_installed and system_browser
                else "未找到系统 Edge 或 Chrome，无法进行人工 Google 登录。"
                if browser_installed
                else "Playwright 已安装，但 Chromium 尚未安装。"
            ),
        }
    except Exception:
        return {
            "available": False,
            "interactive_available": False,
            "package_installed": True,
            "browser_installed": False,
            "system_browser_available": bool(system_browser),
            "system_browser_name": str(system_browser.get("name") or ""),
            "install_command": install_command,
            "message": "无法检查 Chromium 运行时。",
        }


def run_supplier_browser_sso(
    request: SsoRunRequest,
    cancel_event: threading.Event,
    update_status: Callable[[str, str], None],
) -> SsoRunResult:
    try:
        adapter = dict(request.adapter) if request.adapter else browser_sso_adapter_for_supplier(request.supplier_key)
    except ValueError as exc:
        return SsoRunResult(status="error", message=str(exc), error_code="sso_adapter_missing")
    parsed_portal = urlparse(request.portal_base)
    allowed_hosts = {
        str(host).strip().lower().lstrip(".")
        for host in adapter.get("portal_hosts", [])
        if str(host).strip()
    }
    if parsed_portal.scheme.lower() != "https" or (parsed_portal.hostname or "").lower().lstrip(".") not in allowed_hosts:
        return SsoRunResult(status="error", message="灵算 SSO 只允许访问 https://lingsuan.top。", error_code="invalid_portal_base")

    runtime = browser_runtime_status()
    if not runtime.get("available"):
        return SsoRunResult(
            status="browser_missing",
            message=str(runtime.get("message") or "Chromium 运行时不可用。"),
            error_code="browser_runtime_missing",
        )

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return SsoRunResult(
            status="browser_missing",
            message="Playwright 浏览器运行时不可用。",
            error_code="browser_runtime_missing",
        )

    system_browser = find_system_browser()
    if request.interactive and not system_browser:
        return SsoRunResult(
            status="browser_missing",
            message="未找到系统 Edge 或 Chrome，无法打开安全的人工登录窗口。",
            error_code="system_browser_missing",
        )

    request.profile_dir.mkdir(parents=True, exist_ok=True)
    capture = BrowserCredentialCapture(adapter)
    deadline = time.monotonic() + max(1, request.timeout_seconds)
    context = None
    system_process: subprocess.Popen[Any] | None = None
    try:
        if request.interactive:
            login_url = f"{request.portal_base.rstrip('/')}/{str(adapter.get('login_path') or '/dashboard').lstrip('/')}"
            command = build_system_browser_command(
                str(system_browser.get("executable_path") or ""),
                request.profile_dir,
                login_url,
            )
            system_process = subprocess.Popen(command)
            update_status(
                "waiting_for_user",
                f"请在系统 {system_browser.get('name') or '浏览器'} 中完成 Google 登录，成功进入灵算后台后关闭该窗口，再点击“我已完成登录”。",
            )
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    _stop_browser_process(system_process)
                    return SsoRunResult(status="cancelled", message="浏览器登录已取消。")
                browser_closed = system_process.poll() is not None
                user_confirmed = bool(request.completion_event and request.completion_event.is_set())
                if browser_closed and user_confirmed:
                    break
                if user_confirmed and not browser_closed:
                    update_status("waiting_for_user", "已收到确认。请先关闭系统浏览器登录窗口，RelayDeck 随后会验证登录状态。")
                time.sleep(0.25)
            else:
                _stop_browser_process(system_process)
                return SsoRunResult(status="timed_out", message="等待系统浏览器登录超时，请重新发起授权。", error_code="login_timeout")

            update_status("validating", "正在验证灵算登录状态并提取供应商凭据。")

        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": True,
                "viewport": {"width": 1280, "height": 800},
            }
            if system_browser.get("executable_path"):
                launch_options["executable_path"] = str(system_browser["executable_path"])
            context = playwright.chromium.launch_persistent_context(str(request.profile_dir), **launch_options)
            page = context.pages[0] if context.pages else context.new_page()

            def handle_response(response: Any) -> None:
                try:
                    capture.observe_response(response.url, response.status, response.request.headers)
                except Exception:
                    return

            page.on("response", handle_response)
            update_status("validating", "正在验证灵算浏览器会话。")
            page.goto(
                f"{request.portal_base.rstrip('/')}/{str(adapter.get('login_path') or '/dashboard').lstrip('/')}",
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    return SsoRunResult(status="cancelled", message="浏览器登录已取消。")
                if capture.authenticated:
                    update_status("validating", "正在验证灵算登录状态。")
                    cookies = context.cookies([request.portal_base])
                    credentials = capture.credentials(cookies)
                    if credentials:
                        return SsoRunResult(
                            status="authenticated",
                            message="灵算浏览器登录已建立。",
                            credentials=credentials,
                        )
                if adapter.get("google_rejection_check") and is_google_signin_rejected_url(page.url):
                    return SsoRunResult(
                        status="interaction_required",
                        message="Google 拒绝了自动化浏览器登录，请重新使用系统浏览器授权。",
                        error_code="google_browser_rejected",
                    )
                if page.is_closed():
                    return SsoRunResult(
                        status="interaction_required" if request.interactive else "cancelled",
                        message="登录窗口已关闭，请重新发起授权。" if request.interactive else "静默续期窗口已关闭。",
                        error_code="browser_closed",
                    )
                page.wait_for_timeout(250)
    except Exception:
        if cancel_event.is_set():
            return SsoRunResult(status="cancelled", message="浏览器登录已取消。")
        return SsoRunResult(
            status="interaction_required" if not request.interactive else "error",
            message="独立浏览器未能完成灵算登录。" if request.interactive else "浏览器会话需要重新授权。",
            error_code="browser_login_failed",
        )
    finally:
        _stop_browser_process(system_process)
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    if request.interactive:
        return SsoRunResult(status="timed_out", message="等待 Google 登录超时，请重新发起授权。", error_code="login_timeout")
    return SsoRunResult(
        status="interaction_required",
        message="浏览器会话需要重新授权。",
        error_code="interaction_required",
    )


def run_lingsuan_browser_sso(
    request: SsoRunRequest,
    cancel_event: threading.Event,
    update_status: Callable[[str, str], None],
) -> SsoRunResult:
    """Backward-compatible entry point for existing callers and tests."""
    return run_supplier_browser_sso(request, cancel_event, update_status)


class SupplierSsoManager:
    def __init__(
        self,
        profile_root: str | Path,
        *,
        worker: Worker | None = None,
        credential_sink: CredentialSink | None = None,
    ):
        self.profile_root = Path(profile_root).resolve()
        self.worker = worker or self._missing_worker
        self.credential_sink = credential_sink
        self._lock = threading.RLock()
        self._tasks: dict[str, _Task] = {}

    @staticmethod
    def _missing_worker(
        request: SsoRunRequest,
        cancel_event: threading.Event,
        update_status: Callable[[str, str], None],
    ) -> SsoRunResult:
        return SsoRunResult(
            status="browser_missing",
            message="浏览器登录运行时尚未安装。",
            error_code="browser_runtime_missing",
        )

    def profile_path(self, supplier_key: str) -> Path:
        return profile_path_for_supplier(self.profile_root, supplier_key)

    def _public_status(self, key: str, task: _Task | None) -> dict[str, Any]:
        if task is None:
            return {
                "supplier_key": key,
                "status": "idle",
                "message": "",
                "error_code": "",
                "requires_interaction": False,
                "interactive": False,
                "created_at": "",
                "updated_at": "",
                "has_browser_session": self.profile_path(key).exists(),
            }
        return {
            "supplier_key": key,
            "task_id": task.task_id,
            "status": task.status,
            "message": task.message,
            "error_code": task.error_code,
            "requires_interaction": task.status in {"interaction_required", "interrupted"},
            "interactive": task.interactive,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "has_browser_session": self.profile_path(key).exists(),
        }

    def status(self, supplier_key: str) -> dict[str, Any]:
        key = normalize_supplier_key(supplier_key)
        with self._lock:
            return self._public_status(key, self._tasks.get(key))

    def start(
        self,
        supplier_key: str,
        portal_base: str,
        *,
        interactive: bool = True,
        timeout_seconds: int = 600,
        adapter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = normalize_supplier_key(supplier_key)
        with self._lock:
            existing = self._tasks.get(key)
            if existing and existing.status in ACTIVE_STATUSES:
                raise SsoTaskActiveError(f"SSO task already active for {key}")
            now = _now_iso()
            task = _Task(
                task_id=uuid.uuid4().hex,
                supplier_key=key,
                status="starting",
                message="正在启动独立浏览器登录",
                error_code="",
                interactive=bool(interactive),
                created_at=now,
                updated_at=now,
            )
            self._tasks[key] = task
            request = SsoRunRequest(
                supplier_key=key,
                portal_base=str(portal_base or "").rstrip("/"),
                profile_dir=self.profile_path(key),
                interactive=bool(interactive),
                timeout_seconds=max(1, int(timeout_seconds or 600)),
                completion_event=task.completion_event,
                adapter=dict(adapter or {}),
            )
            thread = threading.Thread(
                target=self._run_task,
                args=(task, request),
                name=f"supplier-sso-{key}",
                daemon=True,
            )
            task.thread = thread
            thread.start()
            return self._public_status(key, task)

    def _run_task(self, task: _Task, request: SsoRunRequest) -> None:
        def update_status(status: str, message: str = "") -> None:
            if status not in ACTIVE_STATUSES:
                raise ValueError(f"Invalid active SSO status: {status}")
            with self._lock:
                current = self._tasks.get(task.supplier_key)
                if current is not task or current.status == "interrupted":
                    return
                current.status = status
                current.message = _safe_message(message)
                current.updated_at = _now_iso()

        try:
            result = self.worker(request, task.cancel_event, update_status)
            if result.status not in TERMINAL_STATUSES:
                raise ValueError(f"Invalid terminal SSO status: {result.status}")
            credentials = {
                str(key): str(value)
                for key, value in (result.credentials or {}).items()
                if str(key).strip() and value not in (None, "")
            }
            if result.status == "authenticated" and credentials and self.credential_sink:
                self.credential_sink(task.supplier_key, credentials)
            with self._lock:
                current = self._tasks.get(task.supplier_key)
                if current is not task or current.status == "interrupted":
                    return
                current.status = result.status
                current.message = _safe_message(result.message, credentials)
                current.error_code = str(result.error_code or "")[:100]
                current.updated_at = _now_iso()
        except Exception:
            with self._lock:
                current = self._tasks.get(task.supplier_key)
                if current is not task or current.status == "interrupted":
                    return
                current.status = "error"
                current.message = "浏览器登录任务执行失败。"
                current.error_code = "sso_worker_failed"
                current.updated_at = _now_iso()

    def cancel(self, supplier_key: str) -> bool:
        key = normalize_supplier_key(supplier_key)
        with self._lock:
            task = self._tasks.get(key)
            if not task or task.status not in ACTIVE_STATUSES:
                return False
            task.cancel_event.set()
            task.message = "正在取消浏览器登录"
            task.updated_at = _now_iso()
            return True

    def complete_interactive(self, supplier_key: str) -> bool:
        key = normalize_supplier_key(supplier_key)
        with self._lock:
            task = self._tasks.get(key)
            if not task or not task.interactive or task.status not in {"starting", "waiting_for_user"}:
                return False
            task.completion_event.set()
            task.message = "已收到完成确认，等待系统浏览器窗口关闭后验证登录状态。"
            task.updated_at = _now_iso()
            return True

    def run_silent(
        self,
        supplier_key: str,
        portal_base: str,
        *,
        timeout_seconds: int = 45,
        adapter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = normalize_supplier_key(supplier_key)
        with self._lock:
            existing = self._tasks.get(key)
            if existing and existing.status in ACTIVE_STATUSES:
                return {**self._public_status(key, existing), "existing_task": True}
        if not self.profile_path(key).exists():
            status = self.status(key)
            return {
                **status,
                "status": "interaction_required",
                "message": "尚未建立独立浏览器会话。",
                "error_code": "browser_session_missing",
                "requires_interaction": True,
                "existing_task": False,
            }
        self.start(
            key,
            portal_base,
            interactive=False,
            timeout_seconds=timeout_seconds,
            adapter=adapter,
        )
        with self._lock:
            task = self._tasks.get(key)
            thread = task.thread if task else None
        if thread:
            thread.join(max(1, timeout_seconds) + 5)
        status = self.status(key)
        if thread and thread.is_alive():
            self.cancel(key)
            return {
                **status,
                "status": "timed_out",
                "message": "静默浏览器续期超时。",
                "error_code": "silent_renewal_timeout",
                "requires_interaction": False,
                "existing_task": False,
            }
        return {**status, "existing_task": False}

    def clear_session(self, supplier_key: str) -> bool:
        key = normalize_supplier_key(supplier_key)
        with self._lock:
            task = self._tasks.get(key)
            if task and task.status in ACTIVE_STATUSES:
                raise SsoTaskActiveError(f"Cannot clear active SSO session for {key}")
        profile_path = self.profile_path(key).resolve()
        root = self.profile_root.resolve()
        if profile_path.parent != root:
            raise ValueError("Supplier profile escaped configured root")
        if not profile_path.exists():
            return False
        shutil.rmtree(profile_path)
        return True

    def mark_active_tasks_interrupted(self) -> None:
        with self._lock:
            for task in self._tasks.values():
                if task.status not in ACTIVE_STATUSES:
                    continue
                task.cancel_event.set()
                task.status = "interrupted"
                task.message = "管理服务已重启，请重新发起登录。"
                task.error_code = "service_interrupted"
                task.updated_at = _now_iso()


__all__ = [
    "ACTIVE_STATUSES",
    "BUILTIN_BROWSER_SSO_ADAPTERS",
    "BrowserCredentialCapture",
    "LingSuanCredentialCapture",
    "SsoRunRequest",
    "SsoRunResult",
    "SsoTaskActiveError",
    "SupplierSsoManager",
    "build_system_browser_command",
    "browser_runtime_status",
    "browser_sso_adapter_for_supplier",
    "build_supplier_cookie_header",
    "extract_bearer_token",
    "find_system_browser",
    "is_google_signin_rejected_url",
    "is_browser_sso_auth_me_url",
    "is_lingsuan_auth_me_url",
    "normalize_supplier_key",
    "profile_path_for_supplier",
    "run_lingsuan_browser_sso",
    "run_supplier_browser_sso",
]
