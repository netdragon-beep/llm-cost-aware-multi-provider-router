const fsPromises = require('node:fs/promises');
const path = require('node:path');
const { execFile, spawn } = require('node:child_process');

const MANAGEMENT_PANEL_URL = 'http://127.0.0.1:8091';
const LEDGER_FILE_NAME = 'relaydeck-services.json';
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

function ledgerPath(appDataPath) {
  return path.join(runtimeDirectory(appDataPath), LEDGER_FILE_NAME);
}

async function readLedger(fs, appDataPath) {
  try {
    const ledger = JSON.parse(await fs.readFile(ledgerPath(appDataPath), 'utf8'));
    return ledger.version === 2 && Array.isArray(ledger.servicePids) && ledger.servicePids.length > 0
      ? ledger
      : null;
  } catch (error) {
    if (error.code === 'ENOENT' || error instanceof SyntaxError) {
      return null;
    }
    throw error;
  }
}

async function writeLedger(fs, appDataPath, servicePids) {
  await fs.mkdir(runtimeDirectory(appDataPath), { recursive: true });
  await fs.writeFile(
    ledgerPath(appDataPath),
    JSON.stringify({ version: 2, servicePids }),
    'utf8',
  );
}

function powerShellInvocation(scriptPath) {
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
    ],
    { windowsHide: true },
  ];
}

function startFixedScript(spawnImpl, scriptPath) {
  const [command, args, options] = powerShellInvocation(scriptPath);
  return spawnImpl(command, args, options);
}

function observeChildError(child) {
  const failure = new Promise((_, reject) => child.once('error', reject));
  failure.catch(() => {});
  return failure;
}

function normalizeIdentityValue(value) {
  return String(value ?? '').replace(/\\/g, '/').toLowerCase();
}

function isExpectedRelayDeckService(processInfo, repoRoot) {
  if (!processInfo || !Number.isInteger(processInfo.pid) || processInfo.pid <= 0) {
    return false;
  }

  if (normalizeIdentityValue(processInfo.executable).endsWith('/powershell.exe')) {
    return false;
  }

  const command = `${processInfo.executable ?? ''} ${processInfo.command ?? ''}`.toLowerCase();
  const adminPanelPath = normalizeIdentityValue(path.join(repoRoot, 'admin-panel'));
  return command.includes('litellm')
    || command.includes('open-webui')
    || command.includes('claude_desktop_gateway:app')
    || (command.includes('app:app') && normalizeIdentityValue(command).includes(adminPanelPath));
}

function sameProcessIdentity(recorded, current) {
  return recorded.pid === current.pid
    && normalizeIdentityValue(recorded.executable) === normalizeIdentityValue(current.executable)
    && normalizeIdentityValue(recorded.command) === normalizeIdentityValue(current.command);
}

async function inspectWindowsProcess(pid) {
  return new Promise((resolve, reject) => {
    const query = `Get-CimInstance Win32_Process -Filter \"ProcessId = ${pid}\" | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress`;
    execFile('powershell.exe', ['-NoProfile', '-Command', query], { windowsHide: true }, (error, stdout) => {
      if (error) {
        reject(error);
        return;
      }
      if (!stdout.trim()) {
        resolve(null);
        return;
      }
      const processInfo = JSON.parse(stdout);
      resolve({
        pid: processInfo.ProcessId,
        executable: processInfo.ExecutablePath,
        command: processInfo.CommandLine,
      });
    });
  });
}

async function listWindowsProcesses() {
  return new Promise((resolve, reject) => {
    const query = 'Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress';
    execFile('powershell.exe', ['-NoProfile', '-Command', query], { windowsHide: true }, (error, stdout) => {
      if (error) {
        reject(error);
        return;
      }
      if (!stdout.trim()) {
        resolve([]);
        return;
      }
      const processInfos = JSON.parse(stdout);
      resolve((Array.isArray(processInfos) ? processInfos : [processInfos]).map((processInfo) => ({
        pid: processInfo.ProcessId,
        executable: processInfo.ExecutablePath,
        command: processInfo.CommandLine,
      })));
    });
  });
}

async function listExpectedRelayDeckServices(processLister, repoRoot) {
  const processes = await processLister();
  return processes.filter((processInfo) => isExpectedRelayDeckService(processInfo, repoRoot));
}

async function captureNewRelayDeckServices(repoRoot, processLister, previousServices) {
  const currentServices = await listExpectedRelayDeckServices(processLister, repoRoot);
  return currentServices.filter((current) => !previousServices.some(
    (previous) => sameProcessIdentity(previous, current),
  ));
}

async function ensureRelayDeckRunning(options = {}) {
  const fetchImpl = options.fetchImpl ?? globalThis.fetch;
  const spawnImpl = options.spawnImpl ?? spawn;
  const fs = options.fs ?? fsPromises;
  const url = options.url ?? MANAGEMENT_PANEL_URL;
  const appDataPath = options.appDataPath;
  const repoRoot = options.repoRoot;
  const processLister = options.processLister ?? listWindowsProcesses;

  if (await isManagementPanelHealthy(fetchImpl, url, {
    ...options,
    requestTimeoutMs: options.requestTimeoutMs ?? DEFAULT_RETRY_INTERVAL_MS,
  })) {
    return { state: 'reused', url };
  }

  const previousServices = await listExpectedRelayDeckServices(processLister, repoRoot);
  const child = startFixedScript(spawnImpl, path.join(repoRoot, 'scripts', 'start-all.ps1'));
  const launcherFailure = observeChildError(child);
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
  const servicePids = await captureNewRelayDeckServices(repoRoot, processLister, previousServices);
  if (servicePids.length > 0) {
    await writeLedger(fs, appDataPath, servicePids);
  }
  return { state: 'started', url };
}

async function stopElectronOwnedServices(options = {}) {
  const fs = options.fs ?? fsPromises;
  const appDataPath = options.appDataPath;
  const repoRoot = options.repoRoot;
  const processInspector = options.processInspector ?? inspectWindowsProcess;
  const terminateProcess = options.terminateProcess ?? ((pid) => process.kill(pid, 'SIGTERM'));
  const ledger = await readLedger(fs, appDataPath);

  if (!ledger) {
    return { state: 'not-owned' };
  }

  for (const recorded of ledger.servicePids) {
    const current = await processInspector(recorded.pid);
    if (isExpectedRelayDeckService(current, repoRoot) && sameProcessIdentity(recorded, current)) {
      await terminateProcess(recorded.pid);
    }
  }
  await fs.unlink(ledgerPath(appDataPath));
  return { state: 'stopped' };
}

module.exports = {
  MANAGEMENT_PANEL_URL,
  ensureRelayDeckRunning,
  isManagementPanelHealthy,
  stopElectronOwnedServices,
  waitForManagementPanel,
};
