$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
$pythonExe = Get-CondaPythonExe
$litellmExe = Get-CondaToolExe -Name "litellm"
$configPath = "$root/config/litellm.yaml"
$claudeConfigPath = "$root/config/litellm-claude.yaml"
$logDir = Get-LogDir
$stdoutLogPath = "$logDir/litellm.stdout.log"
$stderrLogPath = "$logDir/litellm.stderr.log"
$claudeStdoutLogPath = "$logDir/litellm-claude.stdout.log"
$claudeStderrLogPath = "$logDir/litellm-claude.stderr.log"
$claudeGatewayStdoutLogPath = "$logDir/claude-desktop-gateway.stdout.log"
$claudeGatewayStderrLogPath = "$logDir/claude-desktop-gateway.stderr.log"

Assert-PathExists -Path $litellmExe -Label "LiteLLM executable"
Assert-PathExists -Path $configPath -Label "LiteLLM config"
Assert-PathExists -Path $claudeConfigPath -Label "Claude LiteLLM config"
Load-DotEnv | Out-Null
Assert-RequiredEnvValue -Name "LITELLM_MASTER_KEY"
Ensure-Directory $logDir

$env:RELAYDECK_LITELLM_DISCOVERY_PATCH = "1"
$existingPythonPath = [string]($env:PYTHONPATH)
$env:PYTHONPATH = if ($existingPythonPath) { "$root;$existingPythonPath" } else { $root }
$litellmPort = [int](Get-EnvValueOrDefault -Name "LITELLM_PORT" -DefaultValue "4100")
$claudeGatewayPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_PORT" -DefaultValue "4101")
$claudeInternalPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_INTERNAL_PORT" -DefaultValue "4102")
$headers = @{ Authorization = "Bearer $env:LITELLM_MASTER_KEY" }

function Start-LiteLlmIfNeeded {
  param([int]$Port, [string]$Config, [string]$ServiceName, [string]$Stdout, [string]$Stderr, [string]$Label)
  $existingPid = Get-PortOwnerPid -Port $Port
  if ($existingPid) {
    if (!(Test-ProcessMatches -ProcessId $existingPid -ExpectedPath $litellmExe -CommandPattern "litellm")) {
      throw "$Label port $Port is already occupied by pid $existingPid."
    }
    Write-PidFile -ServiceName $ServiceName -ProcessId $existingPid
    Write-Output "$Label already running on port $Port (pid=$existingPid)"
    return
  }
  $process = Start-Process -FilePath $litellmExe -ArgumentList @("--config", $Config, "--port", "$Port", "--host", "127.0.0.1") -WorkingDirectory $root -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr -WindowStyle Hidden -PassThru
  Write-PidFile -ServiceName $ServiceName -ProcessId $process.Id
  if (!(Wait-ForHttpOk -Url "http://127.0.0.1:$Port/models" -Headers $headers -TimeoutSeconds 60)) {
    Stop-ServiceProcess -ServiceName $ServiceName -ExpectedPath $litellmExe -CommandPattern "litellm" -Port $Port | Out-Null
    throw "$Label failed readiness check on port $Port"
  }
  Write-Output "$Label started on port $Port (pid=$($process.Id))"
}

Start-LiteLlmIfNeeded -Port $litellmPort -Config $configPath -ServiceName "litellm" -Stdout $stdoutLogPath -Stderr $stderrLogPath -Label "LiteLLM"
Start-LiteLlmIfNeeded -Port $claudeInternalPort -Config $claudeConfigPath -ServiceName "litellm-claude-internal" -Stdout $claudeStdoutLogPath -Stderr $claudeStderrLogPath -Label "Claude internal LiteLLM"

$legacyPid = Get-PortOwnerPid -Port $claudeGatewayPort
if ($legacyPid -and (Test-ProcessMatches -ProcessId $legacyPid -ExpectedPath $litellmExe -CommandPattern "litellm")) {
  Stop-ServiceProcess -ServiceName "litellm-claude" -ExpectedPath $litellmExe -CommandPattern "litellm" -Port $claudeGatewayPort | Out-Null
  Start-Sleep -Milliseconds 500
}

$gatewayPid = Get-PortOwnerPid -Port $claudeGatewayPort
if ($gatewayPid) {
  if (Test-ProcessMatches -ProcessId $gatewayPid -ExpectedPath $pythonExe -CommandPattern "claude_desktop_gateway:app") {
    Write-PidFile -ServiceName "claude-desktop-gateway" -ProcessId $gatewayPid
    Write-Output "Claude Desktop gateway already running on port $claudeGatewayPort (pid=$gatewayPid)"
    exit 0
  }
  throw "Claude Desktop gateway port $claudeGatewayPort is already occupied by pid $gatewayPid."
}

$env:CLAUDE_LITELLM_INTERNAL_PORT = "$claudeInternalPort"
$gatewayProcess = Start-Process -FilePath $pythonExe -ArgumentList @("-m", "uvicorn", "claude_desktop_gateway:app", "--app-dir", $root, "--host", "127.0.0.1", "--port", "$claudeGatewayPort") -WorkingDirectory $root -RedirectStandardOutput $claudeGatewayStdoutLogPath -RedirectStandardError $claudeGatewayStderrLogPath -WindowStyle Hidden -PassThru
Write-PidFile -ServiceName "claude-desktop-gateway" -ProcessId $gatewayProcess.Id
if (!(Wait-ForHttpOk -Url "http://127.0.0.1:$claudeGatewayPort/v1/models" -Headers $headers -TimeoutSeconds 60)) {
  Stop-ServiceProcess -ServiceName "claude-desktop-gateway" -ExpectedPath $pythonExe -CommandPattern "claude_desktop_gateway:app" -Port $claudeGatewayPort | Out-Null
  throw "Claude Desktop gateway failed readiness check on port $claudeGatewayPort"
}
Write-Output "Claude Desktop gateway started on port $claudeGatewayPort (pid=$($gatewayProcess.Id))"
