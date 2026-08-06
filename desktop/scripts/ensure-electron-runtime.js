const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { downloadArtifact } = require('@electron/get');

const electronDirectory = path.resolve(__dirname, '..', 'node_modules', 'electron');
const distDirectory = path.join(electronDirectory, 'dist');
const executablePath = path.join(distDirectory, 'electron.exe');
const pathFile = path.join(electronDirectory, 'path.txt');

function hasElectronRuntime() {
  return fs.existsSync(executablePath)
    && fs.existsSync(pathFile)
    && fs.readFileSync(pathFile, 'utf8').trim() === 'electron.exe';
}

function electronCacheRoot() {
  if (process.env.electron_config_cache) {
    return process.env.electron_config_cache;
  }
  return process.env.LOCALAPPDATA
    ? path.join(process.env.LOCALAPPDATA, 'electron', 'Cache')
    : undefined;
}

function findCachedArtifact(version) {
  const cacheRoot = electronCacheRoot();
  if (!cacheRoot || !fs.existsSync(cacheRoot)) {
    return undefined;
  }

  const archiveName = 'electron-v' + version + '-win32-' + process.arch + '.zip';
  for (const entry of fs.readdirSync(cacheRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) {
      continue;
    }
    const candidate = path.join(cacheRoot, entry.name, archiveName);
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return undefined;
}

async function ensureElectronRuntime() {
  if (process.platform !== 'win32' || hasElectronRuntime()) {
    return;
  }

  const { version } = require(path.join(electronDirectory, 'package.json'));
  const archivePath = findCachedArtifact(version) ?? await downloadArtifact({
    version,
    artifactName: 'electron',
    platform: 'win32',
    arch: process.arch,
    cacheRoot: electronCacheRoot(),
  });

  fs.rmSync(distDirectory, { recursive: true, force: true });
  fs.mkdirSync(distDirectory, { recursive: true });
  execFileSync('tar.exe', ['-xf', archivePath, '-C', distDirectory], {
    stdio: 'inherit',
    windowsHide: true,
  });
  fs.writeFileSync(pathFile, 'electron.exe', 'utf8');

  if (!fs.existsSync(executablePath)) {
    throw new Error('Electron runtime repair did not produce electron.exe');
  }
}

ensureElectronRuntime().catch((error) => {
  console.error('Electron runtime repair failed: ' + error.message);
  process.exitCode = 1;
});
