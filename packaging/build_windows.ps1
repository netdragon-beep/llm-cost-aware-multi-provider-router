param(
  [string]$OutputDir = "",
  [string]$Version = "0.0.0-dev",
  [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$buildRoot = Join-Path $PSScriptRoot "build/windows"
$payloadRoot = Join-Path $buildRoot "payload"
$runtimeRoot = Join-Path $payloadRoot "runtime"
$buildEnvRoot = Join-Path $buildRoot "build-env"
$output = if ($OutputDir) { [System.IO.Path]::GetFullPath($OutputDir) } else { Join-Path $root "dist" }

Remove-Item -LiteralPath $buildRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $payloadRoot, $output | Out-Null

& $PythonExe "$PSScriptRoot/build_payload.py" --from-git-head $root $payloadRoot
if ($LASTEXITCODE -ne 0) { throw "Payload staging failed." }
& $PythonExe -m venv $runtimeRoot
if ($LASTEXITCODE -ne 0) { throw "Runtime virtual environment creation failed." }
& $PythonExe -m venv $buildEnvRoot
if ($LASTEXITCODE -ne 0) { throw "Build virtual environment creation failed." }

$runtimePython = Join-Path $runtimeRoot "Scripts/python.exe"
$buildPython = Join-Path $buildEnvRoot "Scripts/python.exe"
& $runtimePython -m pip install --disable-pip-version-check --no-cache-dir -r "$payloadRoot/requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "Runtime dependency installation failed." }
& $buildPython -m pip install --disable-pip-version-check --no-cache-dir -r "$PSScriptRoot/requirements-packaging.txt"
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }
& $buildPython -m PyInstaller --noconfirm --onefile --name RelayDeck --distpath $payloadRoot --workpath (Join-Path $buildRoot "pyinstaller-work") --specpath $buildRoot "$PSScriptRoot/launcher.py"
if ($LASTEXITCODE -ne 0) { throw "Windows launcher build failed." }

$env:RELAYDECK_VERSION = $Version
$iscc = Get-Command iscc -ErrorAction Stop
& $iscc.Source "/O$output" "/FRelayDeck-Setup-x64" "$PSScriptRoot/windows/RelayDeck.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
