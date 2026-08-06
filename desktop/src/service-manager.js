const fsPromises = require('node:fs/promises');
const path = require('node:path');
const { spawn } = require('node:child_process');

const MANAGEMENT_PANEL_URL = 'http://127.0.0.1:8091';
const RUNTIME_MANIFEST_FILE_NAME = 'relaydeck-services.json';
const DEFAULT_RETRY_INTERVAL_MS = 250;

function managementUrls(url) {
  const baseUrl = url.replace(/\/+$/, '');
  return [`${baseUrl}/api/status`, baseUrl];
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function remainingRequestTimeout(options) {
  const remainingMs = options.remainingMs ? options.remainingMs() : undefined;
  if (remainingMs !== undefined && remainingMs <= 0) {
    return 0;
  }
  return remainingMs === undefined
    ? options.requestTimeoutMs
    : Math.min(options.requestTimeoutMs, remainingMs);
}

async function isManagementPanelHealthy(fetchImpl, url, options = {}) {
  const [statusUrl, rootUrl] = managementUrls(url);
  let statusUnavailable = false;

  try {
    const response = await fetchWithTimeout(fetchImpl, statusUrl, options);
    if (response && response.ok) {
      return true;
    }
    statusUnavailable = response && response.status === 404;
  } catch {
    statusUnavailable = true;
  }

  if (!statusUnavailable) {
    return false;
  }

  try {
    const response = await fetchWithTimeout(fetchImpl, rootUrl, options);
    return Boolean(response && response.ok);
  } catch {
    return false;
  }
}

function fetchWithTimeout(fetchImpl, url, options = {}) {
  const timeoutMs = remainingRequestTimeout(options);
  if (timeoutMs === undefined) {
    return fetchImpl(url);
  }

  if (timeoutMs <= 0) {
    return Promise.reject(new Error(`Timed out requesting RelayDeck management panel at ${url}`));
  }

  const setTimeoutImpl = options.setTimeoutImpl ?? setTimeout;
  const clearTimeoutImpl = options.clearTimeoutImpl ?? clearTimeout;
  const abortController = (options.createAbortController ?? (() => new AbortController()))();
  let timeoutId;
  let removeAbortListener = () => {};
  const request = Promise.resolve().then(() => fetchImpl(url, { signal: abortController.signal }));
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeoutImpl(
      () => {
        const error = new Error(`Timed out requesting RelayDeck management panel at ${url}`);
        abortController.abort(error);
        reject(error);
      },
      timeoutMs,
    );
  });

  const externalAbort = new Promise((_, reject) => {
    if (!options.signal) {
      return;
    }
    const abort = () => {
      const error = options.signal.reason ?? new Error('RelayDeck health request was cancelled');
      abortController.abort(error);
      reject(error);
    };
    if (options.signal.aborted) {
      abort();
      return;
    }
    options.signal.addEventListener('abort', abort, { once: true });
    removeAbortListener = () => options.signal.removeEventListener('abort', abort);
  });

  return Promise.race([request, timeout, externalAbort]).finally(() => {
    clearTimeoutImpl(timeoutId);
    removeAbortListener();
  });
}

async function waitForManagementPanel(fetchImpl, url, timeoutMs, options = {}) {
  const retryIntervalMs = options.retryIntervalMs ?? DEFAULT_RETRY_INTERVAL_MS;
  const sleep = options.sleep ?? delay;
  const now = options.now ?? Date.now;
  const requestTimeoutMs = options.requestTimeoutMs ?? DEFAULT_RETRY_INTERVAL_MS;
  const deadline = now() + timeoutMs;

  while (now() < deadline) {
    if (options.signal?.aborted) {
      throw options.signal.reason ?? new Error('RelayDeck health check was cancelled');
    }
    const remainingMs = deadline - now();
    if (await isManagementPanelHealthy(fetchImpl, url, {
      ...options,
      requestTimeoutMs,
      remainingMs: () => deadline - now(),
    })) {
      return url;
    }

    const retryRemainingMs = deadline - now();
    if (retryRemainingMs <= 0) {
      break;
    }
    await sleep(Math.min(retryIntervalMs, retryRemainingMs));
  }

  throw new Error(`Timed out waiting for RelayDeck management panel at ${url}`);
}

function runtimeDirectory(appDataPath) {
  return path.join(appDataPath, 'runtime');
}

function runtimeManifestPath(appDataPath) {
  return path.join(runtimeDirectory(appDataPath), RUNTIME_MANIFEST_FILE_NAME);
}

async function writeEmptyRuntimeManifest(fs, appDataPath) {
  const manifestPath = runtimeManifestPath(appDataPath);
  const temporaryPath = `${manifestPath}.${process.pid}.${Date.now()}.tmp`;
  await fs.mkdir(runtimeDirectory(appDataPath), { recursive: true });
  await fs.writeFile(temporaryPath, JSON.stringify({ version: 1, Services: [] }), 'utf8');
  await fs.rename(temporaryPath, manifestPath);
}

function powerShellInvocation(scriptPath, runtimeManifest) {
  return [
    'powershell.exe',
    [
      '-NoProfile',
      '-ExecutionPolicy',
      'Bypass',
      '-WindowStyle',
      'Hidden',
      '-File',
      scriptPath,
      '-RuntimeManifestPath',
      runtimeManifest,
    ],
    { windowsHide: true },
  ];
}

function startFixedScript(spawnImpl, scriptPath, runtimeManifest) {
  const [command, args, options] = powerShellInvocation(scriptPath, runtimeManifest);
  return spawnImpl(command, args, options);
}

function observeChildFailure(child) {
  const failure = new Promise((_, reject) => {
    child.once('error', reject);
    child.once('close', (exitCode) => {
      if (exitCode !== 0) {
        reject(new Error(`RelayDeck launcher exited with code ${exitCode}`));
      }
    });
  });
  failure.catch(() => {});
  return failure;
}

function waitForChildExit(child) {
  return new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('close', (exitCode) => {
      if (exitCode === 0) {
        resolve();
      } else {
        reject(new Error(`RelayDeck launcher exited with code ${exitCode}`));
      }
    });
  });
}

async function ensureRelayDeckRunning(options = {}) {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const spawnImpl = options.spawnImpl ?? spawn;
  const fs = options.fs ?? fsPromises;
  const url = options.url ?? MANAGEMENT_PANEL_URL;
  const appDataPath = options.appDataPath;
  const repoRoot = options.repoRoot;

  const managementHealthy = await isManagementPanelHealthy(fetchImpl, url, {
    ...options,
    requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_RETRY_INTERVAL_MS,
  });
  const stackHealthy = !options.requireFullStack
    || (typeof options.isStackHealthy === 'function' && await options.isStackHealthy());

  if (managementHealthy && stackHealthy) {
    await writeEmptyRuntimeManifest(fs, appDataPath);
    return { state: 'reused', url };
  }

  const child = startFixedScript(
    spawnImpl,
    path.join(repoRoot, 'scripts', 'start-desktop-owned.ps1'),
    runtimeManifestPath(appDataPath),
  );
  const launcherFailure = observeChildFailure(child);
  const startupAbort = new AbortController();
  launcherFailure.catch((error) => startupAbort.abort(error));
  if (!Number.isInteger(child.pid) || child.pid <= 0) {
    throw new Error('RelayDeck launcher did not provide a process identifier');
  }
  await Promise.race([
    waitForManagementPanel(fetchImpl, url, options.timeoutMs ?? 30_000, {
      ...options,
      signal: startupAbort.signal,
    }),
    launcherFailure,
  ]);
  return { state: 'started', url };
}

async function stopElectronOwnedServices(options = {}) {
  const appDataPath = options.appDataPath;
  const repoRoot = options.repoRoot;
  const spawnImpl = options.spawnImpl ?? spawn;
  const child = startFixedScript(
    spawnImpl,
    path.join(repoRoot, 'scripts', 'stop-desktop-owned.ps1'),
    runtimeManifestPath(appDataPath),
  );
  await waitForChildExit(child);
  return { state: 'stopped' };
}

module.exports = {
  MANAGEMENT_PANEL_URL,
  ensureRelayDeckRunning,
  isManagementPanelHealthy,
  stopElectronOwnedServices,
  waitForManagementPanel,
};
