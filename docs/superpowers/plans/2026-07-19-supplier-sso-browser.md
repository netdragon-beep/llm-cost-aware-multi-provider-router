# Supplier Browser SSO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated Playwright Chromium login session for supplier SSO, initially supporting LingSuan Google login and automatic credential renewal after quota authentication failures.

**Architecture:** A focused `supplier_sso.py` module owns task state, profile directories and optional Playwright execution. FastAPI validates supplier targets, sends successful supplier credentials to the existing DPAPI vault and exposes redacted task APIs. The management page polls safe task status, while quota refresh retries once after a successful silent browser renewal.

**Tech Stack:** Python 3.11, FastAPI, Playwright Python sync API, Chromium persistent contexts, Windows DPAPI, vanilla JavaScript, Python unittest, Node test runner.

**Repository note:** The current branch contains required uncommitted quota work. Do not create commits or a clean worktree during this plan; use test checkpoints and leave final Git integration to the user.

---

### Task 1: Browser SSO task state and profile isolation

**Files:**
- Create: `admin-panel/supplier_sso.py`
- Create: `admin-panel/tests/test_supplier_sso_manager.py`

- [x] Write failing tests for one active task per supplier, redacted public status, cancellation, interruption recovery and safe profile deletion.
- [x] Run `D:\conda\envs\llm-stack-local\python.exe admin-panel\tests\test_supplier_sso_manager.py -v` and confirm imports fail because `supplier_sso.py` does not exist.
- [x] Implement `SupplierSsoManager`, `SupplierSsoTask`, `SsoRunResult`, `normalize_supplier_key()` and `profile_path_for_supplier()`.
- [x] Make `start()` run an injected worker on a daemon thread and expose only `status`, timestamps, `message`, `error_code` and `requires_interaction`.
- [x] Make `cancel()` set a task event without killing unrelated processes; make `clear_session()` reject active tasks and verify the resolved path remains under the configured profile root.
- [x] Re-run the focused tests and the existing Python suite.

### Task 2: Optional Playwright LingSuan driver

**Files:**
- Modify: `admin-panel/supplier_sso.py`
- Create: `admin-panel/tests/test_supplier_sso_driver.py`

- [x] Write failing tests around pure helpers: exact `lingsuan.top` HTTPS matching, Bearer extraction, supplier-only Cookie header construction and rejection of Google/third-party requests.
- [x] Add a credential-capture test proving successful supplier credentials are returned without appearing in status or messages.
- [x] Implement lazy `playwright.sync_api` import and `browser_runtime_status()` so the admin panel still starts when Playwright or Chromium is missing.
- [x] Implement `run_lingsuan_browser_sso()` using `launch_persistent_context`, a visible window for interactive login and headless mode for silent renewal.
- [x] Listen only for successful same-origin `/api/v1/auth/me` traffic, collect only LingSuan credentials and close the context in `finally`.
- [x] Return `interaction_required`, `browser_missing`, `timed_out`, `cancelled` or a redacted error instead of logging browser storage or request headers.
- [x] Run focused and full Python tests.

### Task 3: Supplier SSO APIs and DPAPI credential sink

**Files:**
- Modify: `admin-panel/app.py`
- Create: `admin-panel/tests/test_supplier_sso_api.py`

- [x] Write failing API/helper tests for unsupported suppliers, start/status/cancel/session-clear behavior and successful DPAPI merge.
- [x] Instantiate one manager rooted at `data/browser-sessions`; active task state is process-local, so a restart naturally returns `idle` while preserving the profile.
- [x] Add helpers that build the LingSuan worker from `supplier_quota_targets()` and merge only allowed `auth_token`, `refresh_token` and `session_cookie` fields into `CredentialVault`.
- [x] Add `POST /api/supplier-sso/{supplier_key}/start`, `GET /status`, `POST /cancel` and `DELETE /session` endpoints.
- [x] Include `sso_status` and `browser_runtime` in supplier quota public state without exposing profile paths or credentials.
- [x] Run API/helper tests and the full Python suite.

### Task 4: Supplier control-panel login workflow

**Files:**
- Modify: `admin-panel/static/index.html`
- Modify: `admin-panel/tests/supplier-quota-ui.test.js`

- [x] Add failing source-contract tests for SSO actions, status polling cleanup, interaction-required text and adapter-specific guidance.
- [x] Extend frontend state with supplier SSO status and one polling timer per active supplier.
- [x] Render “建立浏览器登录”, “重新授权”, “取消登录” and “清除浏览器会话” according to status.
- [x] On start, show immediate button feedback and poll `GET /status` every two seconds until a terminal state.
- [x] Stop polling after completion or page unload; preserve active polling across in-page view changes and never place returned data into credential inputs.
- [x] When `lingsuan-web` is selected, show the next required action instead of automatically opening a browser.
- [x] Run Node tests, inline JavaScript syntax and duplicate DOM ID checks.

### Task 5: One-shot automatic renewal on quota authentication failure

**Files:**
- Modify: `admin-panel/app.py`
- Modify: `admin-panel/tests/test_supplier_quota_refresh.py`

- [x] Write failing tests proving a `401/403`-classified LingSuan result triggers one silent renewal, reloads DPAPI credentials and retries the quota probe once.
- [x] Add tests proving non-authentication failures do not launch a browser, concurrent renewal reports the existing task and failed renewal does not loop.
- [x] Inject the silent SSO refresher at the LingSuan web-adapter branch for deterministic tests.
- [x] Use silent Playwright renewal only for enabled `lingsuan-web` suppliers with an existing browser profile.
- [x] Preserve the previous successful balance and return `interaction_required` when Google asks for user action.
- [x] Run focused and full Python tests.

### Task 6: Installation, security documentation and end-to-end verification

**Files:**
- Modify: `requirements.txt`
- Create: `scripts/install-browser-runtime.ps1`
- Modify: `.gitignore`
- Modify: `README.md`
- Modify: `docs/provider-quota-adapter-guide.md`

- [x] Pin the tested Playwright package version in `requirements.txt`.
- [x] Add a PowerShell installer that resolves the configured Conda Python, installs dependencies and executes `python -m playwright install chromium` with clear failure output.
- [x] Explicitly ignore `data/browser-sessions/` and document that it contains a dedicated browser profile protected by the current Windows account.
- [x] Document first authorization, automatic renewal limits, browser disk/memory cost and how to clear a supplier session.
- [x] Install Playwright and Chromium in the local `llm-stack-local` environment.
- [x] Run all Python tests, Node tests, Python compile, inline JavaScript syntax, duplicate-ID checks and `git diff --check`.
- [x] Restart the admin panel and verify `/api/health`, runtime availability and the supplier SSO controls in the browser without completing a real Google login.
- [x] Confirm no Token, Cookie, Authorization header, browser profile or credential-vault data is tracked by Git or returned by `/api/state`.
