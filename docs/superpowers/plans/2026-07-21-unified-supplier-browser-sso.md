# Unified Supplier Browser SSO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every supported website supplier the same isolated browser SSO flow, configure AutoCode alongside LingSuan, and make quota refresh renew expired sessions through that flow.

**Architecture:** Keep supplier-specific website differences in a small browser SSO adapter registry and manifest contract. The shared SSO manager owns browser discovery, isolated profiles, user confirmation, session validation, credential capture, DPAPI persistence, polling, cancellation, and silent renewal. AutoCode and LingSuan use the same driver with different portal host and endpoint metadata.

**Tech Stack:** Python, FastAPI, Playwright, Windows browser profiles, Windows DPAPI, vanilla JavaScript, Python unittest, Node test runner.

---

### Task 1: Browser SSO adapter contract

**Files:**
- Modify: `admin-panel/supplier_sso.py`
- Modify: `admin-panel/app.py`
- Modify: `quota-adapters/adapter_template.adapter.json`
- Create: `admin-panel/tests/test_supplier_sso_adapter.py`

- [x] Add failing tests for host validation, generic `/api/v1/auth/me` capture, supplier-scoped cookie capture, and the built-in LingSuan/AutoCode adapter registry.
- [x] Run the focused tests and verify the generic adapter contract is absent.
- [x] Add a data-driven adapter configuration with portal host, login path, auth-me path, cookie domains, and safe display metadata.
- [x] Replace the LingSuan-only capture worker with a generic browser worker driven by that configuration.
- [x] Re-run the focused tests.

### Task 2: AutoCode SSO and silent renewal

**Files:**
- Modify: `admin-panel/app.py`
- Modify: `admin-panel/supplier_sso.py`
- Modify: `admin-panel/tests/test_supplier_sso_api.py`
- Modify: `admin-panel/tests/test_supplier_quota_refresh.py`

- [x] Add failing tests proving AutoCode can start interactive SSO and an auth failure invokes the same silent SSO renewal path as LingSuan.
- [x] Run the focused tests and verify AutoCode is currently rejected or skipped.
- [x] Resolve the SSO adapter from the supplier quota configuration instead of hard-coding `lingsuan.top`.
- [x] Pass adapter metadata into interactive and silent SSO tasks, and remove the AutoCode email/password login path from automatic refresh.
- [x] Re-run focused SSO and quota-refresh tests.

### Task 3: Unified user-facing recovery flow

**Files:**
- Modify: `admin-panel/static/index.html`
- Modify: `admin-panel/tests/supplier-quota-ui.test.js`
- Modify: `admin-panel/tests/view-mode-refresh.test.js`

- [x] Add failing source-contract tests for a supplier-wide browser login action and for removing manual Token/Cookie actions from the normal quota panel.
- [x] Replace the provider-specific SSO label with a shared `浏览器登录 / 重新登录` flow for all adapters that declare browser SSO support.
- [x] Remove `加密保存凭据` and `测试并刷新额度` from the advanced login panel; keep quota refresh in the main supplier controls.
- [x] Replace `清除登录凭据` with a hidden/reset-session recovery action that clears the isolated browser profile and stored session together.
- [x] Re-run frontend tests and verify browser SSO status remains visible without exposing credentials.

### Task 4: Documentation and verification

**Files:**
- Modify: `docs/provider-quota-adapter-guide.md`
- Modify: `quota-adapters/README.md`
- Modify: `quota-adapters/adapter_template.adapter.json`
- Modify: `docs/superpowers/plans/2026-07-21-unified-supplier-browser-sso.md`

- [x] Document the unified browser SSO adapter fields and the AutoCode configuration.
- [x] Run all Python tests, Node tests, Python compilation, inline JavaScript syntax, adapter manifest parsing, duplicate literal DOM ID checks, and `git diff --check`.
- [x] Regenerate/restart the admin service and verify both supported supplier SSO start endpoints select their own adapter configuration without exposing credentials.
- [x] Mark all plan items complete only after fresh verification evidence.
