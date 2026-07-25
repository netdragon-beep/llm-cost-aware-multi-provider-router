# Supplier Cost Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Attribute real LiteLLM requests to immutable price versions and supplier cash-cost batches, including weighted top-up cost and daily subscription amortization.

**Architecture:** Extend the existing SQLite ledger with backward-compatible columns, keep deterministic cost calculations in a focused module, and register a fail-open LiteLLM custom callback. Extend the existing supplier quota panel instead of creating another top-level page.

**Tech Stack:** Python, SQLite, FastAPI, LiteLLM custom callbacks, vanilla JavaScript, unittest, Node test runner.

---

### Task 1: Cost ledger migration

**Files:**
- Modify: `admin-panel/usage_quota_store.py`
- Create: `admin-panel/tests/test_cost_attribution_store.py`

- [x] Write failing tests for idempotent schema migration, extended cost batches, request identifiers, price-version binding and request deduplication.
- [x] Run the focused tests and verify failure is caused by missing fields and methods.
- [x] Add the columns, migration helper, effective price lookup and idempotent usage insertion.
- [x] Re-run the focused tests.

### Task 2: Cost attribution engine

**Files:**
- Create: `admin-panel/cost_attribution.py`
- Create: `admin-panel/tests/test_cost_attribution.py`

- [x] Write failing tests for weighted top-up cost, bonus quota, currency mismatch, inclusive subscription days and daily request allocation.
- [x] Run the focused tests and verify the module is missing.
- [x] Implement pure calculation helpers with explicit missing-data statuses.
- [x] Re-run the focused tests.

### Task 3: Real LiteLLM request callback

**Files:**
- Create: `relaydeck_litellm_callback.py`
- Create: `admin-panel/tests/test_litellm_usage_callback.py`
- Modify: `admin-panel/app.py`

- [x] Write failing tests for callback payload normalization, fail-open behavior, duplicate request protection and LiteLLM configuration metadata.
- [x] Run the focused tests and verify the callback/config support is absent.
- [x] Implement the custom callback and inject callback registration plus route identifiers into generated LiteLLM config.
- [x] Re-run callback and existing routing tests.

### Task 4: Billing API and supplier UI

**Files:**
- Modify: `admin-panel/app.py`
- Modify: `admin-panel/static/index.html`
- Create: `admin-panel/tests/test_cost_attribution_api.py`
- Extend: `admin-panel/tests/supplier-quota-ui.test.js`

- [x] Write failing API and source-contract tests for billing type, subscription dates, attribution summary, coverage and recent gateway requests.
- [x] Run the focused tests and verify the new contract is absent.
- [x] Extend recharge endpoints, usage aggregation and the supplier cost panel.
- [x] Re-run focused and full frontend tests.

### Task 5: Full verification

**Files:**
- Modify: `docs/superpowers/plans/2026-07-21-supplier-cost-attribution.md`

- [x] Run all Python and Node tests, compilation, inline JavaScript syntax, manifest JSON, duplicate literal DOM IDs and `git diff --check`.
- [x] Restart LiteLLM and verify the admin panel remains available.
- [x] Send one low-cost gateway test and verify exactly one real gateway event is stored with route identifiers and a price-version status.
- [x] Verify the supplier cost panel in the browser without creating a real user cost batch.
