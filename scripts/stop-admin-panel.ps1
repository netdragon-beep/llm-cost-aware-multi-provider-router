$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
$pythonExe = Get-CondaPythonExe
$adminPort = [int](Get-EnvValueOrDefault -Name "ADMIN_PANEL_PORT" -DefaultValue "8091")
$adminPanelDir = "$root/admin-panel"

$stopped = Stop-ServiceProcess `
  -ServiceName "admin-panel" `
  -ExpectedPath $pythonExe `
  -CommandPattern $adminPanelDir `
  -Port $adminPort

if ($stopped) {
  Write-Output "Stopped admin panel"
} else {
  Write-Output "Admin panel was not running"
}
