const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('node:path');
const {
  MANAGEMENT_PANEL_URL,
  ensureRelayDeckRunning,
  isManagementPanelHealthy,
  stopElectronOwnedServices,
} = require('./service-manager');

let shuttingDown = false;

function serviceOptions() {
  return {
    appDataPath: app.getPath('userData'),
    repoRoot: path.resolve(__dirname, '../..'),
  };
}

async function getRelayDeckStatus() {
  try {
    return {
      healthy: await isManagementPanelHealthy(fetch, MANAGEMENT_PANEL_URL, {
        requestTimeoutMs: 2_000,
      }),
    };
  } catch {
    return { healthy: false };
  }
}

async function openRelayDeckAdmin() {
  await ensureRelayDeckRunning(serviceOptions());
  await shell.openExternal(MANAGEMENT_PANEL_URL);
  return { opened: true };
}

async function showRelayDeckLogs() {
  const result = await shell.openPath(path.join(app.getPath('userData'), 'logs'));
  return { opened: result === '' };
}

ipcMain.handle('relaydeck:get-status', () => getRelayDeckStatus());
ipcMain.handle('relaydeck:open-admin', () => openRelayDeckAdmin());
ipcMain.handle('relaydeck:show-logs', () => showRelayDeckLogs());

function createWindow() {
  const window = new BrowserWindow({
    width: 1100,
    height: 760,
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  window.loadFile(path.join(__dirname, 'shell.html'));
  window.on('close', (event) => {
    if (!shuttingDown) {
      event.preventDefault();
      window.hide();
    }
  });
}

app.whenReady().then(createWindow);

app.on('before-quit', (event) => {
  if (!shuttingDown) {
    shuttingDown = true;
    event.preventDefault();
    stopElectronOwnedServices(serviceOptions())
      .catch((error) => console.error('RelayDeck service cleanup failed:', error))
      .finally(() => app.exit(0));
  }
});
