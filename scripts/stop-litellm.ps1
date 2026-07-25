$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$pythonExe = Get-CondaPythonExe
$litellmExe = Get-CondaToolExe -Name "litellm"
Load-DotEnv | Out-Null
$litellmPort = [int](Get-EnvValueOrDefault -Name "LITELLM_PORT" -DefaultValue "4100")
$claudeGatewayPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_PORT" -DefaultValue "4101")
$claudeInternalPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_INTERNAL_PORT" -DefaultValue "4102")

$results = @()
$results += [pscustomobject]@{ Name = "litellm"; Stopped = (Stop-ServiceProcess -ServiceName "litellm" -ExpectedPath $litellmExe -CommandPattern "litellm" -Port $litellmPort) }
$results += [pscustomobject]@{ Name = "claude-desktop-gateway"; Stopped = (Stop-ServiceProcess -ServiceName "claude-desktop-gateway" -ExpectedPath $pythonExe -CommandPattern "claude_desktop_gateway:app" -Port $claudeGatewayPort) }
$results += [pscustomobject]@{ Name = "litellm-claude-internal"; Stopped = (Stop-ServiceProcess -ServiceName "litellm-claude-internal" -ExpectedPath $litellmExe -CommandPattern "litellm" -Port $claudeInternalPort) }
# Stop the former direct gateway when migrating an existing installation.
$results += [pscustomobject]@{ Name = "litellm-claude-legacy"; Stopped = (Stop-ServiceProcess -ServiceName "litellm-claude" -ExpectedPath $litellmExe -CommandPattern "litellm") }

$summary = $results | ForEach-Object { if ($_.Stopped) { "$($_.Name)=stopped" } else { "$($_.Name)=not-running" } }
Write-Output ("LiteLLM gateway stop complete: " + ($summary -join ", "))
