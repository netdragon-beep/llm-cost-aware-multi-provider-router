# Service Management Dialog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move sidebar service controls into one reusable dialog while keeping the top status summary and client actions outside the dialog.

**Architecture:** The existing service-status refresh remains the single source of truth. The sidebar renders compact service entry buttons, and a modal renders the selected service details and existing action controls. Existing action polling is reused through a dialog-aware render target.

**Tech Stack:** Static HTML, CSS, and browser JavaScript in `admin-panel/static/index.html`; Node built-in test runner.

---

### Task 1: Add the service dialog shell and compact sidebar entries

**Files:**
- Modify: `admin-panel/static/index.html` sidebar markup and styles
- Test: `admin-panel/tests/view-mode-refresh.test.js`

- [x] Add a hidden modal with title, status, port, description, action container, and close button.
- [x] Replace sidebar status spans and inline service action containers with compact buttons that carry `data-service-dialog`.
- [x] Keep the LiteLLM client integration button outside the modal.
- [x] Add a regression assertion for the modal and compact entry markup.

### Task 2: Connect dialog rendering to existing service actions

**Files:**
- Modify: `admin-panel/static/index.html` dialog state, event binding, and service status rendering

- [x] Add a selected service key and service metadata map for management, LiteLLM, and Claude gateway.
- [x] Render status and port inside the dialog using the existing semantic green/red status renderer.
- [x] Keep management read-only and render start/stop/restart actions only for controllable services.
- [x] Open and close the dialog without changing the top status bar or routing view.
- [x] Make service action polling update both the top status bar and the open dialog.

### Task 3: Verify the user-visible flow

**Files:**
- Modify: `admin-panel/tests/view-mode-refresh.test.js`

- [x] Assert service entries open the dialog and old `LISTENING`/`DOWN` sidebar markup is absent.
- [x] Run `node --test admin-panel/tests/view-mode-refresh.test.js admin-panel/tests/supplier-quota-ui.test.js`.
- [x] Run `D:\conda\envs\llm-stack-local\python.exe -m unittest admin-panel/tests/test_service_status_api.py -q`.
- [x] Run `npm test --prefix desktop` and `git diff --check`.
- [x] Restart the management panel and verify `/api/service-status` still responds.
