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
  assert.deepEqual(methods, ['openAdmin', 'showLogs', 'getStatus']);
  assert.match(apiSource, /openAdmin\s*:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*['"]relaydeck:open-admin['"]\s*\)/);
  assert.match(apiSource, /showLogs\s*:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*['"]relaydeck:show-logs['"]\s*\)/);
  assert.match(apiSource, /getStatus\s*:\s*\(\)\s*=>\s*ipcRenderer\.invoke\(\s*['"]relaydeck:get-status['"]\s*\)/);
});

test('preload IPC invocations use literal fixed relaydeck channels', async () => {
  const preloadSource = await readDesktopSource('src/preload.js');
  const apiSource = exposedPreloadApi(preloadSource);
  const invocations = [...apiSource.matchAll(/ipcRenderer\.invoke\(\s*([^,)]+)/g)];

  assert.equal(invocations.length, 3);
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
    ["'relaydeck:get-status'", "'relaydeck:open-admin'", "'relaydeck:show-logs'"],
  );

  for (const [, channel] of handlers) {
    assert.match(channel.trim(), /^['"][^'"\\\r\n]+['"]$/, 'IPC handler channels must be string literals');
  }

  assert.doesNotMatch(mainSource, /['"`]relaydeck:(?:exec|shell)['"`]/);
  assert.doesNotMatch(mainSource, /ipcMain\s*\.\s*(?:handle|on|once)\s*\(\s*[^'"]/);
});

test('explicit Electron shutdown stops Electron-owned services while a normal window close hides the window', async () => {
  const mainSource = await readDesktopSource('src/main.js');

  assert.match(mainSource, /stopElectronOwnedServices\s*\(\s*serviceOptions\(\)\s*\)/);
  assert.match(mainSource, /window\.on\(\s*['"]close['"]/);
  assert.match(mainSource, /event\.preventDefault\(\)/);
  assert.match(mainSource, /window\.hide\(\)/);
  assert.doesNotMatch(mainSource, /window-all-closed[\s\S]{0,160}app\.quit\(\)/);
});

test('status IPC checks use a bounded health request timeout', async () => {
  const mainSource = await readDesktopSource('src/main.js');

  assert.match(mainSource, /isManagementPanelHealthy\(fetch, MANAGEMENT_PANEL_URL, \{\s*requestTimeoutMs:\s*\d+/);
});

test('package scripts and dependencies use pinned versions', async () => {
  const packageJson = JSON.parse(await readDesktopSource('package.json'));

  assert.equal(packageJson.scripts.test, 'node --test');
  for (const field of ['dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies']) {
    for (const [name, version] of Object.entries(packageJson[field] ?? {})) {
      assert.match(version, /^\d+\.\d+\.\d+$/, `${field}.${name} must use an exact x.y.z version`);
    }
  }
});

test('gitignore excludes desktop dependency installs', async () => {
  const gitignore = await readProjectSource('.gitignore');

  assert.match(gitignore, /^desktop\/node_modules\/$/m);
});
