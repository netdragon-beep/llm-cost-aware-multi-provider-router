import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
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
    processInspector: async () => null,
    processLister: async () => [],
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

test('ensureRelayDeckRunning reuses a healthy existing service without creating a ledger', async () => {
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
  assert.equal(fs.files.size, 0);
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

test('ensureRelayDeckRunning starts with the fixed hidden PowerShell launcher', async () => {
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
      'C:\\RelayDeckRepo\\scripts\\start-all.ps1',
    ],
    { windowsHide: true },
  ]]);
  assert.equal(fs.files.size, 0);
});

test('ensureRelayDeckRunning records only new RelayDeck service PIDs', async () => {
  const fs = createMemoryFs();
  fs.files.set('C:\\RelayDeckRepo\\run\\litellm.pid', '100');
  let healthChecks = 0;
  const inspected = new Map([
    [100, { pid: 100, executable: 'C:\\env\\Scripts\\litellm.exe', command: 'litellm --port 4100' }],
    [200, { pid: 200, executable: 'C:\\env\\Scripts\\python.exe', command: 'python -m uvicorn app:app --app-dir C:\\RelayDeckRepo\\admin-panel' }],
  ]);
  const snapshots = [
    [inspected.get(100)],
    [inspected.get(100), inspected.get(200)],
  ];

  await ensureRelayDeckRunning({
    ...managerDependencies({
      fs,
      fetchImpl: async () => response(++healthChecks > 1),
      spawnImpl: () => {
        fs.files.set('C:\\RelayDeckRepo\\run\\admin-panel.pid', '200');
        const child = new EventEmitter();
        child.pid = 4242;
        return child;
      },
    }),
    processLister: async () => snapshots.shift(),
    processInspector: async (pid) => inspected.get(pid) ?? null,
  });

  const ledger = JSON.parse(fs.files.get('C:\\RelayDeckData\\runtime\\relaydeck-services.json'));
  assert.deepEqual(ledger, {
    version: 2,
    servicePids: [inspected.get(200)],
  });
});

test('ensureRelayDeckRunning does not claim a pre-existing user service with a stale pre-launch PID file', async () => {
  const fs = createMemoryFs();
  fs.files.set('C:\\RelayDeckRepo\\run\\litellm.pid', '999');
  let healthChecks = 0;
  const userProcess = { pid: 100, executable: 'C:\\env\\Scripts\\litellm.exe', command: 'litellm --port 4100' };
  const electronProcess = { pid: 200, executable: 'C:\\env\\Scripts\\python.exe', command: 'python -m uvicorn app:app --app-dir C:\\RelayDeckRepo\\admin-panel' };
  const snapshots = [
    [userProcess],
    [userProcess, electronProcess],
  ];
  const terminated = [];

  await ensureRelayDeckRunning({
    ...managerDependencies({
      fs,
      fetchImpl: async () => response(++healthChecks > 1),
      spawnImpl: () => {
        fs.files.set('C:\\RelayDeckRepo\\run\\litellm.pid', '100');
        fs.files.set('C:\\RelayDeckRepo\\run\\admin-panel.pid', '200');
        const child = new EventEmitter();
        child.pid = 4242;
        return child;
      },
    }),
    processLister: async () => snapshots.shift(),
    processInspector: async (pid) => (pid === userProcess.pid ? userProcess : electronProcess),
  });

  const ledger = JSON.parse(fs.files.get('C:\\RelayDeckData\\runtime\\relaydeck-services.json'));
  assert.deepEqual(ledger.servicePids, [electronProcess]);

  await stopElectronOwnedServices({
    ...managerDependencies({ fs }),
    processInspector: async (pid) => (pid === userProcess.pid ? userProcess : electronProcess),
    terminateProcess: async (pid) => { terminated.push(pid); },
  });

  assert.deepEqual(terminated, [electronProcess.pid]);
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

test('stopElectronOwnedServices does not stop an empty ledger', async () => {
  const fs = createMemoryFs();
  fs.files.set(
    'C:\\RelayDeckData\\runtime\\relaydeck-services.json',
    JSON.stringify({ version: 1, launchPids: [] }),
  );
  let spawnCalls = 0;

  await stopElectronOwnedServices(managerDependencies({
    fs,
    spawnImpl: () => { spawnCalls += 1; },
  }));

  assert.equal(spawnCalls, 0);
  assert.equal(fs.files.size, 1);
});

test('stopElectronOwnedServices never invokes the broad stop script and excludes pre-existing user services', async () => {
  const fs = createMemoryFs();
  const ledgerPath = 'C:\\RelayDeckData\\runtime\\relaydeck-services.json';
  const userProcess = { pid: 100, executable: 'C:\\env\\Scripts\\litellm.exe', command: 'litellm --port 4100' };
  const electronProcess = { pid: 200, executable: 'C:\\env\\Scripts\\python.exe', command: 'python -m uvicorn app:app --app-dir C:\\RelayDeckRepo\\admin-panel' };
  fs.files.set(ledgerPath, JSON.stringify({ version: 2, servicePids: [electronProcess] }));
  const terminated = [];

  const result = await stopElectronOwnedServices({
    ...managerDependencies({ fs, spawnImpl: () => { throw new Error('must not spawn a stop script'); } }),
    processInspector: async (pid) => (pid === 100 ? userProcess : electronProcess),
    terminateProcess: async (pid) => { terminated.push(pid); },
  });

  assert.equal(result.state, 'stopped');
  assert.deepEqual(terminated, [200]);
  assert.ok(!terminated.includes(100));
  assert.equal(fs.files.size, 0);
});

test('stopElectronOwnedServices refuses a PID whose command identity changed', async () => {
  const fs = createMemoryFs();
  const ledgerPath = 'C:\\RelayDeckData\\runtime\\relaydeck-services.json';
  fs.files.set(ledgerPath, JSON.stringify({
    version: 2,
    servicePids: [{ pid: 200, executable: 'C:\\env\\Scripts\\litellm.exe', command: 'litellm --port 4100' }],
  }));
  const terminated = [];

  const result = await stopElectronOwnedServices({
    ...managerDependencies({ fs }),
    processInspector: async () => ({ pid: 200, executable: 'C:\\Windows\\System32\\notepad.exe', command: 'notepad.exe notes.txt' }),
    terminateProcess: async (pid) => { terminated.push(pid); },
  });

  assert.equal(result.state, 'stopped');
  assert.deepEqual(terminated, []);
  assert.equal(fs.files.size, 0);
});
