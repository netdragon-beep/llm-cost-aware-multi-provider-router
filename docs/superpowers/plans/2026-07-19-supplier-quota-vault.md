# Supplier Quota Vault Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Windows-encrypted supplier credential vault and make quota collection execute once per supplier platform instead of once per API.

**Architecture:** A standalone DPAPI module owns encrypted credentials. Management state stores non-sensitive `supplier_quotas`; the refresh service combines one supplier quota configuration, one representative API profile and scoped decrypted credentials before invoking an adapter. The frontend edits supplier quota settings and uses dedicated credential endpoints.

**Tech Stack:** Python 3.11+, ctypes/Windows DPAPI, FastAPI/Pydantic, vanilla JavaScript, Node test runner.

---

### Task 1: DPAPI credential vault

**Files:**
- Create: `admin-panel/credential_vault.py`
- Create: `admin-panel/tests/test_credential_vault.py`

- [x] Write tests proving round-trip encryption, absence of plaintext on disk, merge, delete and status behavior.
- [x] Run the tests and confirm they fail because the module does not exist.
- [x] Implement CurrentUser DPAPI encryption and atomic vault persistence.
- [x] Run the tests and confirm they pass.

### Task 2: Supplier quota state and credential APIs

**Files:**
- Modify: `admin-panel/app.py`
- Create: `admin-panel/tests/test_supplier_quota_state.py`

- [x] Write tests for platform grouping, sensitive-field stripping and legacy quota migration.
- [x] Add top-level `supplier_quotas` normalization and payload support.
- [x] Add credential status, update and delete endpoints that never return plaintext.
- [x] Preserve `supplier_quotas` in all management-state save paths.

### Task 3: Supplier-level refresh

**Files:**
- Modify: `admin-panel/app.py`
- Create: `admin-panel/tests/test_supplier_quota_refresh.py`

- [x] Write tests proving one refresh per normalized supplier platform.
- [x] Change balance snapshot keys from API profiles to supplier platforms.
- [x] Merge scoped vault credentials only during adapter execution.
- [x] Make dashboard aggregation read the supplier snapshot while API usage remains separately attributed.

### Task 4: Supplier quota UI

**Files:**
- Modify: `admin-panel/static/index.html`
- Create: `admin-panel/tests/supplier-quota-ui.test.js`

- [x] Write source-contract tests for supplier quota state, controls and credential actions.
- [x] Add a supplier-level quota panel with adapter, manual fallback, credential status and test/refresh actions.
- [x] Remove quota and website credential fields from API cards while retaining pricing and usage controls.
- [x] Connect dedicated credential endpoints and keep all password/token inputs write-only.

### Task 5: Migration and verification

**Files:**
- Modify: `.gitignore`
- Modify: `docs/provider-quota-adapter-guide.md`
- Modify: `quota-adapters/adapter_template.py`

- [x] Document the supplier-level input contract and credential permissions.
- [x] Verify legacy credentials are encrypted before plaintext fields are removed.
- [x] Run Python tests, Node tests, Python compile, inline JavaScript syntax, duplicate-ID checks, `git diff --check` and browser interaction checks.
