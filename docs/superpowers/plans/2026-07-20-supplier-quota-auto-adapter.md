# Supplier Quota Auto Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove user-selectable quota methods and resolve exactly one supplier-specific quota adapter from the supplier platform domain.

**Architecture:** Built-in adapters are selected by `KNOWN_PORTAL_HOST_ADAPTERS`; optional script adapters declare `supported_hosts` in their manifest and are selected automatically. Persisted supplier quota state retains only monitoring and safe provider configuration, while credentials remain in the DPAPI vault.

**Tech Stack:** FastAPI, Python, vanilla JavaScript, Node test runner, Python unittest.

---

### Task 1: Automatic supplier adapter resolution

**Files:**
- Modify: `admin-panel/app.py`
- Test: `admin-panel/tests/test_supplier_quota_state.py`

- [x] Add failing tests proving `lingsuan.top` resolves to `lingsuan-web`, `auto-code.net` resolves to `autocode-web`, a manifest `supported_hosts` entry resolves to its script, and unknown hosts resolve to unsupported.
- [x] Run `python -m unittest admin-panel.tests.test_supplier_quota_state` and verify the new tests fail because the resolver does not exist.
- [x] Implement `resolve_supplier_quota_adapter(platform_key)` and make `supplier_quota_targets()` override stale persisted adapter selections.
- [x] Re-run the state tests and verify they pass.

### Task 2: Remove generic quota execution branches

**Files:**
- Modify: `admin-panel/app.py`
- Test: `admin-panel/tests/test_supplier_quota_refresh.py`

- [x] Add failing tests proving unsupported suppliers do not run manual, OpenAI-compatible, or admin probes and return a clear unsupported result.
- [x] Run the focused refresh tests and verify the unsupported-supplier assertion fails.
- [x] Restrict `refresh_balances_v2()` to built-in website adapters and automatically resolved script adapters; remove manual, OpenAI-compatible and admin execution branches from the refresh path.
- [x] Re-run the focused refresh tests and verify they pass.

### Task 3: Simplify the supplier quota panel

**Files:**
- Modify: `admin-panel/static/index.html`
- Test: `admin-panel/tests/supplier-quota-ui.test.js`

- [x] Add failing source-contract tests proving the quota-method selector and generic manual/admin/script fields are absent, built-in adapter status is shown, and unsupported suppliers receive an explicit message.
- [x] Run `node --test admin-panel/tests/supplier-quota-ui.test.js` and verify the new assertions fail.
- [x] Replace the selector with a read-only supplier adapter status, retain the automatic-monitoring switch and supplier login controls, and remove the unused quota adapter script fetch from page initialization.
- [x] Re-run the Node tests and verify they pass.

### Task 4: Migrate and verify

**Files:**
- Modify: `admin-panel/app.py`
- Modify: `docs/provider-quota-adapter-guide.md`
- Test: `admin-panel/tests/test_supplier_quota_state.py`

- [x] Add a migration assertion proving stale `adapter`, manual quota, admin and script selection fields are removed without deleting encrypted credentials.
- [x] Implement public-state normalization and persistence cleanup for supplier quota configuration.
- [x] Update the adapter guide so manifests bind scripts through `supported_hosts` rather than a control-panel selector.
- [x] Run all Python and Node tests, compile Python, check inline JavaScript syntax, restart only the admin panel, and verify LingSuan remains authenticated with a successful quota snapshot.
