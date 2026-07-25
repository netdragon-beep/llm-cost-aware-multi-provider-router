$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
$pythonExe = Get-CondaPythonExe
$litellmExe = Get-CondaToolExe -Name "litellm"
$openWebUiExe = Get-CondaToolExe -Name "open-webui"

Load-DotEnv | Out-Null
$litellmPort = [int](Get-EnvValueOrDefault -Name "LITELLM_PORT" -DefaultValue "4100")
$claudeLitellmPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_PORT" -DefaultValue "4101")
$claudeInternalPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_INTERNAL_PORT" -DefaultValue "4102")
$openWebUiPort = [int](Get-EnvValueOrDefault -Name "OPEN_WEBUI_PORT" -DefaultValue "8090")
$adminPort = [int](Get-EnvValueOrDefault -Name "ADMIN_PANEL_PORT" -DefaultValue "8091")

$results = @()
$adminPanelDir = "$root/admin-panel"
$results += [pscustomobject]@{
  Name = "admin-panel"
  Stopped = (Stop-ServiceProcess -ServiceName "admin-panel" -ExpectedPath $pythonExe -CommandPattern $adminPanelDir -Port $adminPort)
}
$results += [pscustomobject]@{
  Name = "open-webui"
  Stopped = (Stop-ServiceProcess -ServiceName "open-webui" -ExpectedPath $openWebUiExe -CommandPattern "open-webui" -Port $openWebUiPort)
}
$results += [pscustomobject]@{
  Name = "litellm"
  Stopped = (Stop-ServiceProcess -ServiceName "litellm" -ExpectedPath $litellmExe -CommandPattern "litellm" -Port $litellmPort)
}
$results += [pscustomobject]@{
  Name = "claude-desktop-gateway"
  Stopped = (Stop-ServiceProcess -ServiceName "claude-desktop-gateway" -ExpectedPath $pythonExe -CommandPattern "claude_desktop_gateway:app" -Port $claudeLitellmPort)
}
$results += [pscustomobject]@{
  Name = "litellm-claude-internal"
  Stopped = (Stop-ServiceProcess -ServiceName "litellm-claude-internal" -ExpectedPath $litellmExe -CommandPattern "litellm" -Port $claudeInternalPort)
}
$results += [pscustomobject]@{
  Name = "litellm-claude-legacy"
  Stopped = (Stop-ServiceProcess -ServiceName "litellm-claude" -ExpectedPath $litellmExe -CommandPattern "litellm")
}

$summary = $results | ForEach-Object {
  if ($_.Stopped) { "$($_.Name)=stopped" } else { "$($_.Name)=not-running" }
}

Write-Output ("RelayDeck Local stop complete: " + ($summary -join ", "))
