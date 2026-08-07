import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';

const desktopRoot = new URL('../', import.meta.url);
const projectRoot = new URL('../../', import.meta.url);

async function readDesktopSource(name) {
  return readFile(new URL(name, desktopRoot), 'utf8');
}

async function readProjectSource(name) {
  return readFile(new URL(name, projectRoot), 'utf8');
}

function browserWindowOptions(mainSource) {
  const match = /new\s+BrowserWindow\s*\(\s*({[\s\S]*?})\s*\)/.exec(mainSource);
  assert.ok(match, 'main process must construct a BrowserWindow with options');
  return match[1];
}

function exposedPreloadApi(preloadSource) {
  const match = /contextBridge\.exposeInMainWorld\(\s*['"]relaydeckDesktop['"]\s*,\s*({[\s\S]*?})\s*\)/.exec(preloadSource);
  assert.ok(match, 'preload must expose the relaydeckDesktop API');
  return match[1];
}

test('BrowserWindow construction keeps renderer privileges disabled and sandboxed', async () => {
  const mainSource = await readDesktopSource('src/main.js');
  const windowOptions = browserWindowOptions(mainSource);

  assert.match(windowOptions, /webPreferences\s*:\s*{[\s\S]*?sandbox\s*:\s*true/);
  assert.match(windowOptions, /webPreferences\s*:\s*{[\s\S]*?contextIsolation\s*:\s*true/);
  assert.match(windowOptions, /webPreferences\s*:\s*{[\s\S]*?nodeIntegration\s*:\s*false/);
  assert.match(windowOptions, /webPreferences\s*:\s*{[\s\S]*?preload\s*:/);
});

test('preload exposes the fixed RelayDeck API methods', async () => {
  const preloadSource = await readDesktopSource('src/preload.js');
  const apiSource = exposedPreloadApi(preloadSource);

  const methods = [...apiSource.matchAll(/(\w+)\s*:\s*\(\)\s*=>/g)].map((match) => match[1]);
  assert.deepEqual(methods, ['getStatus', 'restartServices', 'openLogs', 'minimizeWindow', 'closeWindow']);
  assert.match(apiSource, /getStatus\s*:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*['"]relaydeck:get-status['"]\s*\)/);
  assert.match(apiSource, /restartServices\s*:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*['"]relaydeck:restart-services['"]\s*\)/);
  assert.match(apiSource, /openLogs\s*:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*['"]relaydeck:open-logs['"]\s*\)/);
  assert.match(apiSource, /minimizeWindow\s*:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*['"]relaydeck:minimize-window['"]\s*\)/);
  assert.match(apiSource, /closeWindow\s*:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*['"]relaydeck:close-window['"]\s*\)/);
  assert.match(apiSource, /onStatusChanged\s*:\s*\(\s*callback\s*\)\s*=>/);
  assert.match(apiSource, /ipcRenderer\.on\(\s*['"]relaydeck:status-changed['"]/);
});

test('preload IPC invocations use literal fixed relaydeck channels', async () => {
  const preloadSource = await readDesktopSource('src/preload.js');
  const apiSource = exposedPreloadApi(preloadSource);
  const invocations = [...apiSource.matchAll(/ipcRenderer\.invoke\(\s*([^,)]+)/g)];

  assert.equal(invocations.length, 5);
  for (const [, channel] of invocations) {
    assert.match(channel.trim(), /^['"]relaydeck:[a-z-]+['"]$/);
  }
  assert.doesNotMatch(apiSource, /ipcRenderer\.invoke\(\s*`/);
  assert.doesNotMatch(apiSource, /ipcRenderer\.invoke\(\s*[^'"`\s]/);
});

test('main IPC handlers use literal channels and exclude generic command relays', async () => {
  const mainSource = await readDesktopSource('src/main.js');
  const handlers = [...mainSource.matchAll(/\bipcMain\s*\.\s*(?:handle|on|once)\s*\(\s*([^,\r\n)]+)/g)];

  assert.deepEqual(
    handlers.map(([, channel]) => channel.trim()),
    [
      "'relaydeck:get-status'",
      "'relaydeck:restart-services'",
      "'relaydeck:open-logs'",
      "'relaydeck:minimize-window'",
      "'relaydeck:close-window'",
    ],
  );

  for (const [, channel] of handlers) {
    assert.match(channel.trim(), /^['"][^'"\\\r\n]+['"]$/, 'IPC handler channels must be string literals');
  }

  assert.doesNotMatch(mainSource, /['"`]relaydeck:(?:exec|shell)['"`]/);
  assert.doesNotMatch(mainSource, /ipcMain\s*\.\s*(?:handle|on|once)\s*\(\s*[^'"]/);
});

test('desktop window accepts only the local management origin and shell file', async () => {
  const mainSource = await readDesktopSource('src/main.js');

  assert.match(mainSource, /will-navigate/);
  assert.match(mainSource, /MANAGEMENT_PANEL_URL/);
  assert.match(mainSource, /event\.preventDefault\(\)/);
  assert.match(mainSource, /setWindowOpenHandler/);
  assert.match(mainSource, /action:\s*['"]deny['"]/);
});

test('tray menu restores the desktop and explicitly exits through owned-service cleanup', async () => {
  const mainSource = await readDesktopSource('src/main.js');

  assert.match(mainSource, /new Tray\(/);
  assert.match(mainSource, /Menu\.buildFromTemplate/);
  assert.match(mainSource, /打开 RelayDeck/);
  assert.match(mainSource, /重启服务/);
  assert.match(mainSource, /打开日志/);
  assert.match(mainSource, /彻底退出 RelayDeck/);
  assert.match(mainSource, /function requestExplicitExit/);
  assert.match(mainSource, /stopElectronOwnedServices\s*\(\s*serviceOptions\(\)\s*\)/);
});

test('explicit Electron shutdown stops Electron-owned services while a normal window close hides the window', async () => {
  const mainSource = await readDesktopSource('src/main.js');

  assert.match(mainSource, /stopElectronOwnedServices\s*\(\s*serviceOptions\(\)\s*\)/);
  assert.match(mainSource, /\.on\(\s*['"]close['"]/);
  assert.match(mainSource, /event\.preventDefault\(\)/);
  assert.match(mainSource, /mainWindow\.hide\(\)/);
  assert.doesNotMatch(mainSource, /window-all-closed[\s\S]{0,160}app\.quit\(\)/);
});

test('status IPC checks use a bounded health request timeout', async () => {
  const mainSource = await readDesktopSource('src/main.js');

  assert.match(mainSource, /isManagementPanelHealthy\(fetch,\s*[^,]+,\s*\{\s*requestTimeoutMs:\s*\d+/);
});

test('package scripts and dependencies use pinned versions', async () => {
  const packageJson = JSON.parse(await readDesktopSource('package.json'));

  assert.equal(packageJson.scripts.test, 'node --test');
  assert.equal(packageJson.scripts.postinstall, 'node scripts/ensure-electron-runtime.js');
  for (const field of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
    for (const [name, version] of Object.entries(packageJson[field] ?? {})) {
      assert.match(version, /^\d+\.\d+\.\d+$/, `${field}.${name} must use an exact x.y.z version`);
    }
  }
});

test('Electron runtime repair uses a fixed local package path and Windows tar extraction', async () => {
  const source = await readDesktopSource('scripts/ensure-electron-runtime.js');

  assert.match(source, /require\('@electron\/get'\)/);
  assert.match(source, /downloadArtifact/);
  assert.match(source, /process\.platform !== 'win32'/);
  assert.match(source, /path\.join\(electronDirectory, 'dist'\)/);
  assert.match(source, /execFileSync\('tar\.exe'/);
  assert.match(source, /path\.join\(electronDirectory, 'path\.txt'\)/);
  assert.doesNotMatch(source, /shell:\s*true/);
  assert.match(source, /function findCachedArtifact/);
  assert.match(source, /process\.env\.LOCALAPPDATA/);
});

test('desktop shell hides inactive states and assigns the management iframe URL explicitly', async () => {
  const [shellScript, shellCss] = await Promise.all([
    readDesktopSource('src/shell.js'),
    readDesktopSource('src/shell.css'),
  ]);

  assert.match(shellScript, /panel\.getAttribute\('src'\)/);
  assert.match(shellCss, /\[hidden\]\s*\{\s*display:\s*none\s*!important/);
});

test('gitignore excludes desktop dependency installs', async () => {
  const gitignore = await readProjectSource('.gitignore');

  assert.match(gitignore, /^desktop\/node_modules\/$/m);
});
