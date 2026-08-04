# Supplier Route Appearance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a compact supplier-to-model-route relationship panel and persistent fixed appearance themes to the RelayDeck admin panel.

**Architecture:** Extend the existing static admin page only. The relationship panel derives its display from the existing suppliers, API profiles, model routes, route bindings, runtime status, and quota state. Appearance is a browser-local preference stored in localStorage and applied through document-level CSS custom properties; it never changes generated gateway configuration.

**Tech Stack:** FastAPI static page, semantic HTML, CSS custom properties, vanilla JavaScript, Python unittest source assertions, Playwright visual verification.

---

### Task 1: Define the UI contracts with focused source tests

**Files:**
- Modify: `E:\llm-stack-local\admin-panel\tests\test_client_integrations.py`
- Test: `E:\llm-stack-local\admin-panel\tests\test_client_integrations.py`

- [ ] Add two tests reading `admin-panel/static/index.html`. The relationship test asserts the presence of `id="routing-relationship-panel"`, `function renderRoutingRelationshipPanel`, and `data-action="focus-relationship-target"`. The theme test asserts `id="appearance-theme-options"`, `const APPEARANCE_THEME_STORAGE_KEY`, `function applyAppearanceTheme`, and `localStorage.setItem`.
- [ ] Run `C:\Users\Administrator\.venv\Scripts\python.exe -m unittest admin-panel.tests.test_client_integrations -v` and confirm the new tests fail before implementation.
- [ ] After implementation, rerun the same command and confirm all source assertions pass.

### Task 2: Add the supplier-route relationship panel

**Files:**
- Modify: `E:\llm-stack-local\admin-panel\static\index.html`
- Test: `E:\llm-stack-local\admin-panel\tests\test_client_integrations.py`

- [ ] Extend the existing JavaScript `state` with a routing relationship selection object: `{ mode: 'model', routeId: '', apiProfileId: '' }`.
- [ ] Insert `<section class="routing-relationship-panel" id="routing-relationship-panel" aria-live="polite"></section>` after the routing tools and before `provider-list`.
- [ ] Add CSS for a narrow framed relationship block that stacks at the existing 1120px breakpoint and uses current surface, border, text, status, and button styles.
- [ ] Implement `ensureRoutingRelationshipSelection()` and `renderRoutingRelationshipPanel()`. Model-first content lists supplier/API, upstream model, priority, health, and quota hint. Supplier/API-first content lists public model, priority, and enabled/published state. Empty states distinguish no bindings from no enabled published bindings.
- [ ] Reuse `bindingsForRoute()`, `bindingsForApiProfile()`, `routeById()`, `apiProfileById()`, and `supplierDisplayName()`; never duplicate route state or render credentials/base URLs.
- [ ] Add `data-action="focus-relationship-target"` controls. The existing action handler switches the current routing view, sets relationship state, invokes `renderRouting()`, and scrolls to the actual related card. It does not edit or save a route.
- [ ] Call `renderRoutingRelationshipPanel()` from `renderRouting()` before the re-bound action listeners are attached.
- [ ] Commit the completed panel with `git add admin-panel/static/index.html admin-panel/tests/test_client_integrations.py; git commit -m "feat: relate suppliers and model routes in admin panel"`.

### Task 3: Add persistent fixed themes in System Settings

**Files:**
- Modify: `E:\llm-stack-local\admin-panel\static\index.html`
- Test: `E:\llm-stack-local\admin-panel\tests\test_client_integrations.py`

- [ ] Introduce surface, border, text, muted, accent, and accent-contrast CSS custom properties. Migrate broad page backgrounds, elevated surfaces, focus rings, and general borders to these properties.
- [ ] Define fixed `body[data-appearance-theme]` presets: `morning` (晨白), `mist` (雾灰), `deep-space` (深空), `ink` (墨黑), `forest` (森绿), and `amber` (琥珀).
- [ ] Preserve semantic success, warning, and error colours across all presets.
- [ ] Add an `appearance-settings` section inside the existing System Settings dialog with an `appearance-theme-options` target, a Chinese note that the setting remains in the current browser, and selectable swatches.
- [ ] Define `APPEARANCE_THEME_STORAGE_KEY = 'relaydeck.appearance-theme'` and the six-item `APPEARANCE_THEMES` array.
- [ ] Implement `applyAppearanceTheme(themeId, persist = true)`: normalize to deep-space, set `document.body.dataset.appearanceTheme`, persist with localStorage only when requested, and re-render the option buttons.
- [ ] Initialize the saved value during startup, and add `apply-appearance-theme` to the current data-action handler.
- [ ] Commit the completed appearance feature with `git add admin-panel/static/index.html admin-panel/tests/test_client_integrations.py; git commit -m "feat: add persistent admin panel themes"`.

### Task 4: Verify behavior in browser

**Files:**
- Verify: `E:\llm-stack-local\admin-panel\static\index.html`
- Verify: `E:\llm-stack-local\admin-panel\tests\test_client_integrations.py`

- [ ] Run `C:\Users\Administrator\.venv\Scripts\python.exe -m unittest admin-panel.tests.test_client_integrations -v`.
- [ ] Start or reuse the management panel on port 8091 and inspect `http://127.0.0.1:8091`.
- [ ] Exercise model-first and API-first panel selection, then each focus button.
- [ ] Apply 晨白 and 墨黑 from System Settings, reload after each choice, and confirm persistence plus semantic health/warning/failure contrast.
- [ ] At 390px width confirm the relationship block stacks without clipped text or overflowing buttons.
- [ ] Record tests, screenshots, and outcomes in Harness run 103 before marking it complete.
