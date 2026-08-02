# Dual Protocol Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve one public model route across the OpenAI and Anthropic client gateways while letting each API profile select its real upstream protocol.

**Architecture:** Keep the existing twin LiteLLM configurations. The OpenAI view uses public model names and the Claude view uses generated Anthropic-client aliases, but both copy the same provider bindings and `custom_llm_provider`. Clarify this separation in management state and UI rather than deriving provider protocol from model family.

**Tech Stack:** Python, FastAPI, LiteLLM YAML generation, browser-side JavaScript, unittest.

---

### Task 1: Lock the gateway configuration contract with tests

**Files:**
- Modify: `admin-panel/tests/test_claude_model_discovery.py`
- Modify: `admin-panel/app.py:4025-4175`

- [ ] **Step 1: Write failing tests**

Add a provider for `claude-fable-5` with `custom_llm_provider: "openai"` and assert that both generated configs retain `openai`, while their model names differ only by gateway alias. Add an `anthropic` provider and assert it remains `anthropic` in both configs.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `python -m unittest admin-panel.tests.test_claude_model_discovery -v`

Expected: failure until the generated config exposes the protocol-boundary metadata required by the new assertions.

- [ ] **Step 3: Implement the minimal config metadata changes**

Ensure `build_litellm_config_from_providers` writes the provider protocol and public model family into `relaydeck` metadata without inferring protocol from a public model name. Ensure `build_litellm_gateway_configs_from_providers` preserves the same `litellm_params` for both gateways.

- [ ] **Step 4: Re-run the focused tests**

Run: `python -m unittest admin-panel.tests.test_claude_model_discovery -v`

Expected: PASS.

### Task 2: Make protocol mismatch diagnostics actionable

**Files:**
- Modify: `admin-panel/tests/test_supplier_quota_state.py`
- Modify: `admin-panel/app.py:769-780, 3394-3455`

- [ ] **Step 1: Write failing tests**

Add tests that an API profile marked `openai` probes `/models`, while one marked `anthropic` probes `/v1/models`; assert a 404 mismatch hint names the API profile protocol and does not prescribe a model-family change.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `python -m unittest admin-panel.tests.test_supplier_quota_state -v`

Expected: the new diagnostic assertion fails before implementation.

- [ ] **Step 3: Implement the minimal diagnostic change**

Change `api_base_mismatch_hint` to state that `custom_llm_provider` is an API-profile setting independent of the model family, and preserve the current URL construction behavior.

- [ ] **Step 4: Re-run the focused tests**

Run: `python -m unittest admin-panel.tests.test_supplier_quota_state -v`

Expected: PASS.

### Task 3: Expose the three independent concepts in the management UI

**Files:**
- Modify: `admin-panel/app.py` state response serialization for model routes and API profiles
- Modify: `admin-panel/static/app.js` model binding and API profile renderers
- Modify: `admin-panel/static/styles.css` only if existing UI tokens need layout support
- Test: existing admin-panel API/state tests plus a targeted browser/manual check

- [ ] **Step 1: Write failing server-side assertions**

Add or extend a state serialization test to assert each binding can display `model_family`, each API profile exposes `custom_llm_provider`, and each route exposes both client compatibility labels without changing saved provider protocol.

- [ ] **Step 2: Run the focused test and verify the expected failure**

Run: `python -m unittest discover -s admin-panel/tests -p "test_*state*.py" -v`

Expected: failure because compatibility labels are absent.

- [ ] **Step 3: Implement UI labels and helper text**

Render `模型家族`, `客户端入口：OpenAI / Anthropic`, and `上游协议：OpenAI 或 Anthropic` as separate read-only fields on route/API cards. Add a concise note that a Claude family model may use an OpenAI-compatible supplier API.

- [ ] **Step 4: Re-run focused tests and inspect the local management view**

Run: `python -m unittest discover -s admin-panel/tests -p "test_*state*.py" -v`

Expected: PASS; the model route and API profile screens show independent labels without overlap.

### Task 4: Verify the complete dual-gateway behavior

**Files:**
- Modify: `admin-panel/tests/test_claude_model_discovery.py`
- Modify: `README.md`

- [ ] **Step 1: Add end-to-end configuration assertions**

Create one route with an OpenAI upstream and one backup Anthropic upstream. Assert the OpenAI and Claude config views expose their respective client aliases, preserve both providers' protocols, and generate equivalent fallback ordering.

- [ ] **Step 2: Run the new test and verify failure before implementation is complete**

Run: `python -m unittest admin-panel.tests.test_claude_model_discovery -v`

Expected: failure until all protocol metadata and fallback assertions pass.

- [ ] **Step 3: Update README routing documentation**

Document the separation among model family, client protocol, and API-profile protocol. State that LiteLLM performs compatibility translation and that a model name never determines an upstream endpoint protocol.

- [ ] **Step 4: Run verification**

Run: `python -m unittest discover -s admin-panel/tests -p "test_*.py" -v`

Expected: PASS.
