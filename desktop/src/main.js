const { app, BrowserWindow, ipcMain, Menu, nativeImage, shell, Tray } = require('electron');
const net = require('node:net');
const path = require('node:path');
const {
  MANAGEMENT_PANEL_URL,
  ensureRelayDeckRunning,
  isManagementPanelHealthy,
  stopElectronOwnedServices,
} = require('./service-manager');

const SERVICE_PORTS = Object.freeze({
  management: 8091,
  litellm: 4100,
  claude: 4101,
  openWebUI: 8090,
});

let mainWindow;
let tray;
let stoppingServices = false;

function serviceOptions() {
  return {
    appDataPath: app.getPath('userData'),
    repoRoot: path.resolve(__dirname, '../..'),
  };
}

function isPortOpen(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: '127.0.0.1', port });
    const finish = (open) => {
      socket.destroy();
      resolve(open);
    };
    socket.setTimeout(1_500);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

async function getRelayDeckStatus() {
  const [management, litellm, claude, openWebUI] = await Promise.all([
    isManagementPanelHealthy(fetch, MANAGEMENT_PANEL_URL, { requestTimeoutMs: 1_500 }),
    isPortOpen(SERVICE_PORTS.litellm),
    isPortOpen(SERVICE_PORTS.claude),
    isPortOpen(SERVICE_PORTS.openWebUI),
  ]);
  return {
    services: {
      management,
      litellm,
      claude,
      openWebUI,
    },
    healthy: management,
    managementUrl: MANAGEMENT_PANEL_URL,
  };
}

async function isRelayDeckStackHealthy() {
  const status = await getRelayDeckStatus();
  return Object.values(status.services).every(Boolean);
}

function desktopServiceOptions() {
  return {
    ...serviceOptions(),
    requireFullStack: true,
    isStackHealthy: isRelayDeckStackHealthy,
  };
}

async function startRelayDeck() {
  try {
    await ensureRelayDeckRunning(desktopServiceOptions());
    mainWindow?.webContents.send('relaydeck:status-changed', await getRelayDeckStatus());
  } catch (error) {
    mainWindow?.webContents.send('relaydeck:status-changed', {
      healthy: false,
      error: error instanceof Error ? error.message : String(error),
    });
  }
}

async function restartRelayDeck() {
  await stopElectronOwnedServices(serviceOptions());
  await ensureRelayDeckRunning(desktopServiceOptions());
  return getRelayDeckStatus();
}

async function showRelayDeckLogs() {
  const logPath = path.join(app.getPath('userData'), 'logs');
  const result = await shell.openPath(logPath);
  return { opened: result === '', path: logPath, error: result || null };
}

function isAllowedManagementUrl(url) {
  return url === MANAGEMENT_PANEL_URL || url.startsWith(`${MANAGEMENT_PANEL_URL}/`);
}

function attachNavigationGuards(window) {
  window.webContents.on('will-navigate', (event, url) => {
    if (!isAllowedManagementUrl(url) && !url.startsWith('file://')) {
      event.preventDefault();
    }
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedManagementUrl(url)) {
      return { action: 'allow' };
    }
    return { action: 'deny' };
  });
}

function createTrayIcon() {
  const iconPath = path.join(__dirname, '../assets/icon.ico');
  const fileIcon = nativeImage.createFromPath(iconPath);
  if (!fileIcon.isEmpty()) {
    return fileIcon;
  }
  return nativeImage.createFromDataURL(
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  );
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  mainWindow.show();
  mainWindow.focus();
}

function requestExplicitExit() {
  app.quit();
}

function createTray() {
  tray = new Tray(createTrayIcon());
  tray.setToolTip('RelayDeck');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开 RelayDeck', click: showMainWindow },
    { label: '重启服务', click: () => restartRelayDeck().catch((error) => console.error(error)) },
    { label: '打开日志', click: () => showRelayDeckLogs().catch((error) => console.error(error)) },
    { type: 'separator' },
    { label: '彻底退出 RelayDeck', click: requestExplicitExit },
  ]));
  tray.on('double-click', showMainWindow);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 960,
    minHeight: 640,
    frame: false,
    backgroundColor: '#0f172a',
    webPreferences: {
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  attachNavigationGuards(mainWindow);
  mainWindow.loadFile(path.join(__dirname, 'shell.html'));
  mainWindow.on('close', (event) => {
    if (!stoppingServices) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on('closed', () => {
    mainWindow = undefined;
  });
}

ipcMain.handle('relaydeck:get-status', () => getRelayDeckStatus());
ipcMain.handle('relaydeck:restart-services', () => restartRelayDeck());
ipcMain.handle('relaydeck:open-logs', () => showRelayDeckLogs());
ipcMain.handle('relaydeck:minimize-window', () => mainWindow?.minimize());
ipcMain.handle('relaydeck:close-window', () => mainWindow?.close());

app.whenReady().then(() => {
  createTray();
  createWindow();
  startRelayDeck();
});

app.on('before-quit', (event) => {
  if (stoppingServices) {
    return;
  }
  stoppingServices = true;
  event.preventDefault();
  stopElectronOwnedServices(serviceOptions())
    .catch((error) => console.error('RelayDeck service cleanup failed:', error))
    .finally(() => app.exit(0));
});

module.exports = { isAllowedManagementUrl };
