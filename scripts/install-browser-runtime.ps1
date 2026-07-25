$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
$pythonExe = Get-CondaPythonExe
$requirementsPath = Join-Path $root "requirements.txt"

Assert-PathExists -Path $pythonExe -Label "Python executable"
Assert-PathExists -Path $requirementsPath -Label "Requirements file"

$playwrightRequirement = Get-Content -LiteralPath $requirementsPath |
  Where-Object { $_ -match '^playwright==' } |
  Select-Object -First 1

if ([string]::IsNullOrWhiteSpace($playwrightRequirement)) {
  throw "requirements.txt does not contain a pinned Playwright package."
}

Write-Output "Installing $playwrightRequirement into the llm-stack-local Conda environment..."
& $pythonExe -m pip install $playwrightRequirement
if ($LASTEXITCODE -ne 0) {
  throw "Playwright package installation failed with exit code $LASTEXITCODE."
}

Write-Output "Installing the isolated Chromium browser runtime..."
& $pythonExe -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
  throw "Chromium installation failed with exit code $LASTEXITCODE."
}

Write-Output "RelayDeck browser SSO runtime is installed."
