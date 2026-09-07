import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';

import serviceManager from '../src/service-manager.js';

const {
  ensureRelayDeckRunning,
  stopElectronOwnedServices,
  waitForManagementPanel,
} = serviceManager;

function response(ok) {
  return { ok, status: ok ? 200 : 503 };
}

function createMemoryFs() {
  const files = new Map();
  return {
    files,
    async mkdir() {},
    async readdir(directory) {
      const prefix = `${directory}\\`;
      return [...files.keys()]
        .filter((file) => file.startsWith(prefix))
        .map((file) => file.slice(prefix.length));
    },
    async readFile(file) {
      if (!files.has(file)) {
        const error = new Error('not found');
        error.code = 'ENOENT';
        throw error;
      }
      return files.get(file);
    },
    async writeFile(file, contents) {
      files.set(file, contents);
    },
    async rename(from, to) {
      const contents = await this.readFile(from);
      files.set(to, contents);
      files.delete(from);
    },
    async unlink(file) {
      if (!files.delete(file)) {
        const error = new Error('not found');
        error.code = 'ENOENT';
        throw error;
      }
    },
  };
}

function managerDependencies({ fetchImpl, spawnImpl, fs = createMemoryFs() } = {}) {
  return {
    fetchImpl,
    fs,
    spawnImpl,
    appDataPath: 'C:\\RelayDeckData',
    repoRoot: 'C:\\RelayDeckRepo',
    retryIntervalMs: 0,
  };
}

function createLogicalClock() {
  let currentMs = 0;
  const timers = [];
  return {
    now: () => currentMs,
    sleep: async (milliseconds) => { currentMs += milliseconds; },
    setTimeoutImpl(callback, milliseconds) {
      const timer = { callback, dueAt: currentMs + milliseconds, cancelled: false };
      timers.push(timer);
      return timer;
    },
    clearTimeoutImpl(timer) {
      timer.cancelled = true;
    },
    runNextTimer() {
      const timer = timers
        .filter((candidate) => !candidate.cancelled)
        .sort((left, right) => left.dueAt - right.dueAt)[0];
      if (!timer) {
        return false;
      }
      timer.cancelled = true;
      currentMs = timer.dueAt;
      timer.callback();
      return true;
    },
  };
}

async function settleWithLogicalTimers(promise, clock) {
  let outcome;
  promise.then(
    (value) => { outcome = { value }; },
    (error) => { outcome = { error }; },
  );

  for (let turn = 0; turn < 50 && !outcome; turn += 1) {
    for (let microtask = 0; microtask < 20 && !outcome; microtask += 1) {
      await Promise.resolve();
    }
    clock.runNextTimer();
  }

  assert.ok(outcome, 'promise should settle using only logical timers');
  if (outcome.error) {
    throw outcome.error;
  }
  return outcome.value;
}

test('waitForManagementPanel returns immediately when the status endpoint is healthy', async () => {
  const requestedUrls = [];
  const fetchImpl = async (url) => {
    requestedUrls.push(url);
    return response(true);
  };

  const result = await waitForManagementPanel(fetchImpl, 'http://127.0.0.1:8091', 20, {
    retryIntervalMs: 0,
  });

  assert.equal(result, 'http://127.0.0.1:8091');
  assert.deepEqual(requestedUrls, ['http://127.0.0.1:8091/api/status']);
});

test('waitForManagementPanel retries until the status endpoint is healthy', async () => {
  let attempts = 0;
  const fetchImpl = async () => {
    attempts += 1;
    return response(attempts === 2);
  };

  await waitForManagementPanel(fetchImpl, 'http://127.0.0.1:8091', 50, {
    retryIntervalMs: 0,
  });

  assert.equal(attempts, 2);
});

test('waitForManagementPanel falls back to the management root after a failed status check', async () => {
  const requestedUrls = [];
  const fetchImpl = async (url) => {
    requestedUrls.push(url);
    return url.endsWith('/api/status')
      ? { ok: false, status: 404 }
      : response(true);
  };

  await waitForManagementPanel(fetchImpl, 'http://127.0.0.1:8091', 20, {
    retryIntervalMs: 0,
  });

  assert.deepEqual(requestedUrls, [
    'http://127.0.0.1:8091/api/status',
    'http://127.0.0.1:8091',
  ]);
});

test('waitForManagementPanel does not fall back after a status 503 response', async () => {
  const requestedUrls = [];
  const fetchImpl = async (url) => {
    requestedUrls.push(url);
    return response(url === 'http://127.0.0.1:8091');
  };

  await assert.rejects(
    waitForManagementPanel(fetchImpl, 'http://127.0.0.1:8091', 5, {
      retryIntervalMs: 5,
    }),
    /Timed out waiting for RelayDeck management panel/,
  );

  assert.deepEqual(requestedUrls, ['http://127.0.0.1:8091/api/status']);
});

test('waitForManagementPanel retries and times out hung status requests within logical timer bounds', async () => {
  const clock = createLogicalClock();
  const requestedUrls = [];
  const fetchImpl = (url) => {
    requestedUrls.push(url);
    if (url.endsWith('/api/status')) {
      return new Promise(() => {});
    }
    return Promise.resolve(response(false));
  };

  await assert.rejects(
    settleWithLogicalTimers(
      waitForManagementPanel(fetchImpl, 'http://127.0.0.1:8091', 30, {
        now: clock.now,
        sleep: clock.sleep,
        setTimeoutImpl: clock.setTimeoutImpl,
        clearTimeoutImpl: clock.clearTimeoutImpl,
        requestTimeoutMs: 10,
        retryIntervalMs: 5,
      }),
      clock,
    ),
    /Timed out waiting for RelayDeck management panel/,
  );

  assert.deepEqual(requestedUrls, [
    'http://127.0.0.1:8091/api/status',
    'http://127.0.0.1:8091',
    'http://127.0.0.1:8091/api/status',
    'http://127.0.0.1:8091',
  ]);
});

test('waitForManagementPanel aborts a timed-out request and never exceeds its overall deadline', async () => {
  const clock = createLogicalClock();
  let aborted = 0;
  const fetchImpl = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => {
      aborted += 1;
      reject(signal.reason);
    });
  });

  await assert.rejects(
    settleWithLogicalTimers(
      waitForManagementPanel(fetchImpl, 'http://127.0.0.1:8091', 10, {
        now: clock.now,
        sleep: clock.sleep,
        setTimeoutImpl: clock.setTimeoutImpl,
        clearTimeoutImpl: clock.clearTimeoutImpl,
        requestTimeoutMs: 50,
        retryIntervalMs: 5,
      }),
      clock,
    ),
    /Timed out waiting for RelayDeck management panel/,
  );

  assert.equal(aborted, 1);
});

test('waitForManagementPanel times out when neither endpoint becomes healthy', async () => {
  await assert.rejects(
    waitForManagementPanel(async () => response(false), 'http://127.0.0.1:8091', 5, {
      retryIntervalMs: 0,
    }),
    /Timed out waiting for RelayDeck management panel/,
  );
});

test('ensureRelayDeckRunning reuses a healthy existing service without launching', async () => {
  const fs = createMemoryFs();
  let spawnCalls = 0;

  const result = await ensureRelayDeckRunning(managerDependencies({
    fs,
    fetchImpl: async () => response(true),
    spawnImpl: () => {
      spawnCalls += 1;
    },
  }));

  assert.equal(result.state, 'reused');
  assert.equal(spawnCalls, 0);
  assert.deepEqual(
    JSON.parse(fs.files.get('C:\\RelayDeckData\\runtime\\relaydeck-services.json')),
    { version: 1, Services: [] },
  );
});

test('ensureRelayDeckRunning starts missing services when the desktop stack check is incomplete', async () => {
  const fs = createMemoryFs();
  let spawnCalls = 0;

  const result = await ensureRelayDeckRunning({
    ...managerDependencies({
      fs,
      fetchImpl: async () => response(true),
      spawnImpl: () => {
        spawnCalls += 1;
        const child = new EventEmitter();
        child.pid = 4242;
        return child;
      },
    }),
    requireFullStack: true,
    isStackHealthy: async () => false,
  });

  assert.deepEqual(result, { state: 'started', url: 'http://127.0.0.1:8091' });
  assert.equal(spawnCalls, 1);
});

test('ensureRelayDeckRunning replaces a stale manifest on healthy reuse so later stop has no targets', async () => {
  const fs = createMemoryFs();
  const manifestPath = 'C:\\RelayDeckData\\runtime\\relaydeck-services.json';
  const stoppedTargets = [];
  fs.files.set(manifestPath, JSON.stringify({
    version: 1,
    Services: [{ Pid: 1234, ExecutablePath: 'C:\\stale.exe' }],
  }));

  const result = await ensureRelayDeckRunning(managerDependencies({
    fs,
    fetchImpl: async () => response(true),
    spawnImpl: () => {
      throw new Error('healthy reuse must not launch a service');
    },
  }));

  await stopElectronOwnedServices(managerDependencies({
    fs,
    spawnImpl: () => {
      const manifest = JSON.parse(fs.files.get(manifestPath));
      stoppedTargets.push(...manifest.Services);
      const child = new EventEmitter();
      child.pid = 4243;
      queueMicrotask(() => child.emit('close', 0));
      return child;
    },
  }));

  assert.equal(result.state, 'reused');
  assert.deepEqual(JSON.parse(fs.files.get(manifestPath)), { version: 1, Services: [] });
  assert.deepEqual(stoppedTargets, []);
});

test('ensureRelayDeckRunning starts after its initial health check times out', async () => {
  const clock = createLogicalClock();
  let fetchCalls = 0;
  let spawnCalls = 0;

  const result = await settleWithLogicalTimers(
    ensureRelayDeckRunning({
      ...managerDependencies({
        fetchImpl: (url) => {
          fetchCalls += 1;
          if (fetchCalls === 1) {
            return new Promise(() => {});
          }
          return Promise.resolve(response(url.endsWith('/api/status')));
        },
        spawnImpl: () => {
          spawnCalls += 1;
          const child = new EventEmitter();
          child.pid = 4242;
          return child;
        },
      }),
      now: clock.now,
      sleep: clock.sleep,
      setTimeoutImpl: clock.setTimeoutImpl,
      clearTimeoutImpl: clock.clearTimeoutImpl,
      requestTimeoutMs: 10,
      timeoutMs: 30,
    }),
    clock,
  );

  assert.deepEqual(result, { state: 'started', url: 'http://127.0.0.1:8091' });
  assert.equal(spawnCalls, 1);
  assert.equal(fetchCalls, 3);
});

test('ensureRelayDeckRunning invokes only the dedicated launcher with an absolute runtime manifest path', async () => {
  const fs = createMemoryFs();
  const launches = [];
  let healthChecks = 0;
  const spawnImpl = (...args) => {
    launches.push(args);
    const child = new EventEmitter();
    child.pid = 4242;
    return child;
  };

  const result = await ensureRelayDeckRunning(managerDependencies({
    fs,
    fetchImpl: async () => {
      healthChecks += 1;
      return response(healthChecks > 2);
    },
    spawnImpl,
  }));

  assert.equal(result.state, 'started');
  assert.deepEqual(launches, [[
    'powershell.exe',
    [
      '-NoProfile',
      '-ExecutionPolicy',
      'Bypass',
      '-WindowStyle',
      'Hidden',
      '-File',
      'C:\\RelayDeckRepo\\scripts\\start-desktop-owned.ps1',
      '-RuntimeManifestPath',
      'C:\\RelayDeckData\\runtime\\relaydeck-services.json',
    ],
    { windowsHide: true },
  ]]);
  assert.equal(fs.files.size, 0);
});

test('ensureRelayDeckRunning handles launcher spawn errors', async () => {
  const child = new EventEmitter();
  child.pid = 4242;

  await assert.rejects(
    ensureRelayDeckRunning(managerDependencies({
      fetchImpl: async () => response(false),
      spawnImpl: () => {
        queueMicrotask(() => child.emit('error', new Error('spawn failed')));
        return child;
      },
    })),
    /spawn failed/,
  );
});

test('stopElectronOwnedServices invokes only the dedicated manifest stop script', async () => {
  const fs = createMemoryFs();
  const launches = [];

  const result = await stopElectronOwnedServices(managerDependencies({
    fs,
    spawnImpl: (...args) => {
      launches.push(args);
      const child = new EventEmitter();
      child.pid = 4243;
      queueMicrotask(() => child.emit('close', 0));
      return child;
    },
  }));

  assert.equal(result.state, 'stopped');
  assert.deepEqual(launches, [[
    'powershell.exe',
    [
      '-NoProfile',
      '-ExecutionPolicy',
      'Bypass',
      '-WindowStyle',
      'Hidden',
      '-File',
      'C:\\RelayDeckRepo\\scripts\\stop-desktop-owned.ps1',
      '-RuntimeManifestPath',
      'C:\\RelayDeckData\\runtime\\relaydeck-services.json',
    ],
    { windowsHide: true },
  ]]);
});

test('desktop-owned scripts use held process handles with exact manifest identities', async () => {
  const root = path.resolve(import.meta.dirname, '..', '..');
  const [startScript, stopScript, managerSource] = await Promise.all([
    readFile(path.join(root, 'scripts', 'start-desktop-owned.ps1'), 'utf8'),
    readFile(path.join(root, 'scripts', 'stop-desktop-owned.ps1'), 'utf8'),
    readFile(path.join(root, 'desktop', 'src', 'service-manager.js'), 'utf8'),
  ]);

  assert.match(startScript, /Start-Process[\s\S]*-PassThru/);
  assert.match(startScript, /Move-Item[\s\S]*RuntimeManifestPath/);
  assert.match(startScript, /Write-DesktopManifest -Records @\(\)[\s\S]*\$services/);
  assert.deepEqual(
    [...startScript.matchAll(/Name = "([^"]+)"/g)].map(([, name]) => name),
    ['LiteLLM', 'Claude internal LiteLLM', 'Claude Desktop gateway', 'Admin panel'],
  );
  assert.match(startScript, /ExecutablePath = \$executablePath/);
  assert.match(startScript, /CommandLine = \$commandLine/);
  assert.match(startScript, /CreationTime = \$creationTime/);
  assert.match(startScript, /StartTime = \$startTime/);
  assert.match(startScript, /\$Process\.HasExited/);
  assert.match(startScript, /\$Process\.StartTime\.ToUniversalTime\(\)\.ToString\("o"\)/);
  assert.match(startScript, /\$cimProcess\.ProcessId -ne \$processId/);
  assert.match(startScript, /\$creationTime -cne \$startTime/);
  assert.match(startScript, /Get-Process -Id \$record\.Pid[\s\S]*Test-OwnedRecordMatches -Record \$record -Process \$process[\s\S]*\$process\.Kill\(\)[\s\S]*\$process\.WaitForExit\(\)/);
  assert.match(startScript, /if \(Test-PortListening -Port \$Service\.Port\) \{[\s\S]*continue/);
  assert.match(startScript, /\$temporaryStartedProcesses = @\(\)[\s\S]*\$process = Start-Process[\s\S]*\$temporaryStartedProcesses \+= \$process[\s\S]*Get-OwnedProcessRecord/);
  assert.match(startScript, /function Stop-TemporaryStartedProcesses[\s\S]*\$Process\.Refresh\(\)[\s\S]*!\$Process\.HasExited[\s\S]*\$Process\.Kill\(\)[\s\S]*\$Process\.WaitForExit\(\)/);
  assert.match(startScript, /\} catch \{[\s\S]*Stop-TemporaryStartedProcesses -Processes \$temporaryStartedProcesses[\s\S]*Stop-OwnedRecords -Records \$ownedRecords[\s\S]*Remove-Item -LiteralPath \$RuntimeManifestPath -Force/);
  assert.match(stopScript, /StartTime/);
  assert.match(stopScript, /ExecutablePath/);
  assert.match(stopScript, /CommandLine/);
  assert.match(stopScript, /CreationTime/);
  assert.match(stopScript, /Normalize-CommandLine \$cimProcess\.CommandLine\) -cne \$Record\.CommandLine/);
  assert.match(stopScript, /\$Process\.HasExited/);
  assert.match(stopScript, /\$Process\.StartTime\.ToUniversalTime\(\)\.ToString\("o"\)/);
  assert.match(stopScript, /\$cimProcess\.ProcessId -ne \$Process\.Id/);
  assert.match(stopScript, /\$cimProcess\.CreationDate\.ToUniversalTime\(\)\.ToString\("o"\) -cne \$startTime/);
  assert.match(stopScript, /Get-Process -Id \$Record\.Pid[\s\S]*Test-OwnedRecordMatches -Record \$Record -Process \$process[\s\S]*\$process\.Kill\(\)[\s\S]*\$process\.WaitForExit\(\)/);
  assert.doesNotMatch(startScript, /Stop-Process\s+-Id/i);
  assert.doesNotMatch(stopScript, /Stop-Process\s+-Id/i);
  assert.doesNotMatch(startScript, /ExpectedExecutable|CommandPattern/);
  assert.doesNotMatch(stopScript, /ExpectedExecutable|CommandPattern|\.Contains\(/);
  assert.doesNotMatch(stopScript, /Get-PortOwnerPid|Stop-ServiceProcess|stop-llm-stack/i);
  assert.doesNotMatch(managerSource, /start-all\.ps1|stop-llm-stack|processLister|captureNewRelayDeckServices/);
});
