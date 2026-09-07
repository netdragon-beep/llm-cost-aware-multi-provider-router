$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
Load-DotEnv | Out-Null

$litellmPort = [int](Get-EnvValueOrDefault -Name "LITELLM_PORT" -DefaultValue "4100")
$claudeLitellmPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_PORT" -DefaultValue "4101")
$adminPort = [int](Get-EnvValueOrDefault -Name "ADMIN_PANEL_PORT" -DefaultValue "8091")

powershell -NoProfile -ExecutionPolicy Bypass -File "$root/scripts/start-litellm.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "$root/scripts/start-admin-panel.ps1"

Write-Output "RelayDeck Local ready:"
Write-Output "  LiteLLM:      http://127.0.0.1:$litellmPort"
Write-Output "  Claude Code:  http://127.0.0.1:$claudeLitellmPort"
Write-Output "  Admin Panel:  http://127.0.0.1:$adminPort"
