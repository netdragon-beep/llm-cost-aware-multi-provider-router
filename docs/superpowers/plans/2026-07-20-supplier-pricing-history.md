# Supplier Pricing History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add supplier-selected pricing automation, versioned API/model prices, adapter-driven price candidates, an inline shared-quota progress bar, and a collapsed advanced credential repair area.

**Architecture:** Persist only the supplier automation choice in management state. Store immutable price versions and candidates in the existing SQLite usage database, and process normalized adapter `pricing_catalog` observations according to the supplier policy. Reuse the existing supplier quota dashboard row for inline progress rendering.

**Tech Stack:** FastAPI, Python unittest, SQLite, vanilla JavaScript, Node test runner.

---

### Task 1: Price version ledger

**Files:**
- Modify: `admin-panel/usage_quota_store.py`
- Create: `admin-panel/tests/test_pricing_history.py`

- [x] Write failing tests for active-version replacement, historical retention, candidate observation counts and candidate rejection.
- [x] Run `python -m unittest admin-panel.tests.test_pricing_history -v` and verify the missing store methods fail.
- [x] Add `pricing_versions` schema and store methods for insert, list, active lookup, activation, repeated observation and rejection.
- [x] Re-run the focused tests and verify they pass.

### Task 2: Pricing policy and observation processing

**Files:**
- Modify: `admin-panel/app.py`
- Create: `admin-panel/tests/test_supplier_pricing.py`

- [x] Write failing tests for the three policies, normalized fingerprints, display-name-only changes, two-observation auto apply and the 50% safety threshold.
- [x] Run the focused test and verify failures are caused by missing pricing processors.
- [x] Persist `pricing_automation`, normalize adapter observations, and process manual, candidate and auto-apply paths.
- [x] Add list/create/activate/reject FastAPI endpoints scoped by validated supplier platform and API Profile ownership.
- [x] Re-run the focused tests and verify they pass.

### Task 3: Adapter price capability

**Files:**
- Modify: `admin-panel/app.py`
- Modify: `quota-adapters/adapter_template.py`
- Modify: `quota-adapters/adapter_template.adapter.json`
- Modify: `docs/provider-quota-adapter-guide.md`

- [x] Add a failing test proving `pricing_catalog` survives script normalization and is processed only when the manifest declares `fetch_pricing`.
- [x] Extend the custom-script result contract with a redacted `pricing_catalog` and call the pricing processor during supplier refresh.
- [x] Add a disabled example pricing section to the template and document the normalized fields.
- [x] Run supplier quota and pricing tests.

### Task 4: Supplier quota and pricing UI

**Files:**
- Modify: `admin-panel/static/index.html`
- Modify: `admin-panel/tests/supplier-quota-ui.test.js`

- [x] Add failing source-contract tests for inline progress, the pricing strategy selector, price-history actions and a collapsed advanced credential section.
- [x] Render the current supplier dashboard row directly under adapter status using the existing progress styles.
- [x] Replace the always-open credential form with a default-closed secondary panel while keeping SSO actions visible.
- [x] Add supplier pricing strategy, current/candidate counts and price-history controls; wire state refresh after actions.
- [x] Run all Node tests and inline JavaScript syntax checks.

### Task 5: Full verification

**Files:**
- Modify: `docs/superpowers/plans/2026-07-20-supplier-pricing-history.md`

- [x] Run all Python and Node tests, Python compilation, manifest JSON parsing, duplicate DOM ID detection and `git diff --check`.
- [x] Restart only the admin panel.
- [x] Verify LingSuan quota refresh remains successful and the browser shows inline progress, automatic adapter status, pricing policy and collapsed credential repair controls.
