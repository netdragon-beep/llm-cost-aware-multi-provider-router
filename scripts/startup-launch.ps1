$ErrorActionPreference = "SilentlyContinue"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptRoot

powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot "scripts/start-all.ps1")
