# RelayDeck Electron Desktop Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a Windows Electron desktop shell that starts or reuses RelayDeck, embeds the existing management panel, runs in the system tray, and produces an NSIS installer.

**Architecture:** Electron main process owns the desktop window, tray, fixed IPC, local health checks, and the Electron-owned process ledger. The existing PowerShell launchers remain the authoritative service startup/stop implementation. A context-isolated preload exposes a small desktop API to a static shell renderer; the existing FastAPI page stays browser-compatible and has no Node access.

**Tech Stack:** Electron, Electron Builder, Node.js test runner, PowerShell, existing FastAPI/LiteLLM services.

---

### Task 1: Scaffold a safe Electron package and source-contract tests

**Files:**
- Create: `E:\llm-stack-local\desktop\package.json`
- Create: `E:\llm-stack-local\desktop\src\main.js`
- Create: `E:\llm-stack-local\desktop\src\preload.js`
- Create: `E:\llm-stack-local\desktop\src\shell.html`
- Create: `E:\llm-stack-local\desktop\tests\desktop-contract.test.mjs`
- Modify: `E:\llm-stack-local\.gitignore`

- [ ] Add a failing Node test that loads the Electron source as text and requires `contextIsolation: true`, `nodeIntegration: false`, fixed `relaydeck:` IPC channels, and no generic `exec` IPC handler.
- [ ] Run `node --test desktop/tests/desktop-contract.test.mjs` and confirm it fails before the package exists.
- [ ] Create `desktop/package.json` with scripts `test`, `dev`, `package:win`, Electron as a dev dependency, and Electron Builder as a dev dependency.
- [ ] Add `.relaydeck-desktop-runtime/` and Electron build output to `.gitignore`.
- [ ] Run `npm install` from `desktop` and `npm test`; expected result: the security contract passes.

### Task 2: Implement service ownership and health-check modules

**Files:**
- Create: `E:\llm-stack-local\desktop\src\service-manager.js`
- Create: `E:\llm-stack-local\desktop\tests\service-manager.test.mjs`
- Test: `E:\llm-stack-local\desktop\tests\desktop-contract.test.mjs`

- [ ] Write failing tests for `waitForManagementPanel(fetchImpl, url, timeoutMs)` with immediate health, retry-to-health, and timeout cases.
- [ ] Write failing tests for a ledger that persists only Electron-created child PIDs and returns no stop targets for reused services.
- [ ] Implement `waitForManagementPanel` using a bounded retry interval against `http://127.0.0.1:8091/api/status` with a fallback root request only when the status endpoint is unavailable.
- [ ] Implement `ensureRelayDeckRunning`: reuse an existing healthy stack; otherwise start `scripts/start-all.ps1` hidden through a fixed PowerShell invocation; record only its spawned process group in the app-data runtime file.
- [ ] Implement `stopElectronOwnedServices`: exit safely when the ledger is empty; otherwise invoke only the fixed `scripts/stop-llm-stack.ps1` route for a stack launched by Electron.
- [ ] Run `node --test desktop/tests/service-manager.test.mjs desktop/tests/desktop-contract.test.mjs`; expected result: all tests pass.

### Task 3: Implement Electron window, preload boundary, and desktop shell

**Files:**
- Modify: `E:\llm-stack-local\desktop\src\main.js`
- Modify: `E:\llm-stack-local\desktop\src\preload.js`
- Modify: `E:\llm-stack-local\desktop\src\shell.html`
- Create: `E:\llm-stack-local\desktop\src\shell.css`
- Create: `E:\llm-stack-local\desktop\src\shell.js`
- Test: `E:\llm-stack-local\desktop\tests\desktop-contract.test.mjs`

- [ ] Add failing source assertions that the preload exposes only `window.relaydeckDesktop` methods: `getStatus`, `restartServices`, `openLogs`, `minimizeWindow`, `closeWindow`, and `onStatusChanged`.
- [ ] Create a frameless `BrowserWindow` that loads `shell.html`, with safe web preferences and a restricted navigation handler allowing only the shell file and loopback management origin.
- [ ] Make `shell.html` render a Chinese title bar, four service status indicators, a Restart services command, window controls, and an iframe whose source becomes `http://127.0.0.1:8091` only after health succeeds.
- [ ] During startup show a compact Chinese progress state. On failure, show the log folder action and the health-check error instead of a blank iframe.
- [ ] Use only fixed IPC handlers; validate all renderer requests and never pass renderer strings to shell execution.
- [ ] Run `npm test`; expected result: preload API and Electron hardening tests pass.

### Task 4: Implement tray lifecycle and explicit exit behavior

**Files:**
- Modify: `E:\llm-stack-local\desktop\src\main.js`
- Modify: `E:\llm-stack-local\desktop\src\shell.js`
- Test: `E:\llm-stack-local\desktop\tests\desktop-contract.test.mjs`

- [ ] Add failing contract tests that `close` prevents default destruction unless an explicit-exit flag is set, and a tray menu includes Open RelayDeck, Restart services, Open logs, and Exit RelayDeck.
- [ ] Create the tray after `app.whenReady()`. Its Open action restores/focuses the main window; Restart calls the fixed service-manager restart; Open logs opens the known local log directory; Exit sets explicit-exit then calls the Electron-owned stop routine.
- [ ] On window close, hide the window and show a one-time Chinese tray hint. Do not stop services.
- [ ] Ensure `before-quit` does not kill reused user-owned services.
- [ ] Run `npm test`; expected result: tray and close semantics pass source and unit contracts.

### Task 5: Configure Windows packaging and verify the development desktop app

**Files:**
- Modify: `E:\llm-stack-local\desktop\package.json`
- Create: `E:\llm-stack-local\desktop\assets\icon.ico`
- Modify: `E:\llm-stack-local\README.md`
- Test: `E:\llm-stack-local\desktop\tests\desktop-contract.test.mjs`

- [ ] Add Electron Builder Windows x64 NSIS configuration with product name RelayDeck, application ID, icon, and output directory `desktop/release`.
- [ ] Add a README Desktop section describing Windows installer location, close-to-tray behavior, and the distinction between window close and tray Exit.
- [ ] Run `npm test`.
- [ ] Run `npm run dev`, confirm the Electron window reaches the existing management page, close it, restore from tray, and explicitly exit.
- [ ] Run `npm run package:win`; expected output: `desktop/release/RelayDeck Setup *.exe`.
- [ ] Record the installer path and verification result in Harness run 114.
