# RelayDeck Electron Desktop Shell Design

## Objective

Package RelayDeck as a user-installable desktop application without replacing its working local gateway or management UI. Electron owns native-window, process-lifecycle, tray, and packaging concerns; the existing FastAPI management page continues to own supplier, routing, client integration, diagnostic, and local-state workflows.

## Architecture

### Electron main process

- Creates a frameless Windows desktop window with a native-feeling title bar region.
- Checks whether the local management page is reachable at `http://127.0.0.1:8091`.
- Reuses a healthy existing local service stack. When unavailable, starts the repository's supported service launcher and waits for the management page health check.
- Creates a system-tray menu with Open RelayDeck, Restart services, Open logs, and Exit RelayDeck.
- On normal window close, hides the window and keeps services running.
- On explicit tray Exit RelayDeck, asks the renderer to close gracefully, then stops only the processes that Electron started and exits.
- Writes runtime information only under an app-data runtime directory, never into source files, `.env`, or management state.

### Renderer and preload

- The desktop window uses a small local shell document: native title bar, aggregate service indicators, Restart all, and a content frame.
- The content frame loads the existing local management page on port 8091.
- A context-isolated preload exposes a narrow API for window control, reading sanitized service status, restart requests, opening the local log folder, and receiving status updates.
- The management page has no Node.js access and continues to work in an ordinary browser.

### Service ownership

Electron records every PID it directly starts in its runtime state. It may stop only recorded child processes. Existing user-started services are detected and reused, but never terminated by the desktop app.

## User experience

- First launch shows a compact startup state while services are checked or started.
- Once healthy, the title bar shows management page, LiteLLM, and Claude gateway status.
- Closing the window hides it to the tray. A one-time Chinese hint explains that requests remain available.
- Tray Exit is the only action that stops Electron-owned services.
- When the management page cannot start, the shell shows a diagnostic action and the relevant local log path instead of a blank frame.

## Packaging

Stage 1 targets Windows x64 through Electron Builder and produces an NSIS installer. It includes Electron files and uses the existing Python virtual environment or a later packaged Python runtime through a launcher abstraction.

macOS packaging remains a follow-up stage: Apple Silicon and Intel DMG builds use the same Electron structure but need a macOS-specific service launcher and signing/notarization work.

## Security boundaries

- `contextIsolation: true`
- `nodeIntegration: false`
- No arbitrary shell command IPC
- IPC handlers accept only fixed desktop actions
- The embedded management page is restricted to the known loopback origin
- API keys, supplier credentials, and env values never cross IPC or appear in Electron logs

## Stage 1 acceptance

1. Electron opens RelayDeck and starts or reuses the management page.
2. The existing `http://127.0.0.1:8091` page remains usable in both browser and desktop window.
3. Closing the window hides it; tray Open restores it.
4. Tray Exit stops only Electron-owned processes.
5. A Windows installer installs and launches the app.
6. Startup failure produces an actionable Chinese diagnostic, not a blank window.
