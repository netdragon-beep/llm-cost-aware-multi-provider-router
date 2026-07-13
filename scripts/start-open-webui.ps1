$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
$openWebUiExe = Get-CondaToolExe -Name "open-webui"
$logDir = Get-LogDir
$stdoutLogPath = "$logDir/open-webui.stdout.log"
$stderrLogPath = "$logDir/open-webui.stderr.log"
$dataDir = "$root/data/open-webui"

Assert-PathExists -Path $openWebUiExe -Label "Open WebUI executable"
Load-DotEnv | Out-Null
Assert-RequiredEnvValue -Name "LITELLM_MASTER_KEY"
Assert-RequiredEnvValue -Name "OPEN_WEBUI_SECRET_KEY"
Ensure-Directory $logDir
Ensure-Directory $dataDir

$port = [int](Get-EnvValueOrDefault -Name "OPEN_WEBUI_PORT" -DefaultValue "8090")
$litellmPort = [int](Get-EnvValueOrDefault -Name "LITELLM_PORT" -DefaultValue "4100")
$adminPort = [int](Get-EnvValueOrDefault -Name "ADMIN_PANEL_PORT" -DefaultValue "8091")
$existingPid = Get-PortOwnerPid -Port $port
if ($existingPid) {
  if (Test-ProcessMatches -ProcessId $existingPid -ExpectedPath $openWebUiExe -CommandPattern "open-webui") {
    Write-PidFile -ServiceName "open-webui" -ProcessId $existingPid
    Write-Output "Open WebUI already running on port $port (pid=$existingPid)"
    exit 0
  }
  throw "Port $port is already occupied by pid $existingPid, not a managed Open WebUI process."
}

$env:DATA_DIR = $dataDir
$env:OPENAI_API_BASE_URL = "http://127.0.0.1:$litellmPort/v1"
$env:OPENAI_API_KEY = $env:LITELLM_MASTER_KEY
$env:WEBUI_SECRET_KEY = $env:OPEN_WEBUI_SECRET_KEY
$env:RAG_OPENAI_API_BASE_URL = $env:OPENAI_API_BASE_URL
$env:RAG_OPENAI_API_KEY = $env:OPENAI_API_KEY
$env:USER_AGENT = Get-EnvValueOrDefault -Name "USER_AGENT" -DefaultValue "RelayDeckLocal/1.0"
if ([string]::IsNullOrWhiteSpace($env:CORS_ALLOW_ORIGIN)) {
  $env:CORS_ALLOW_ORIGIN = "http://127.0.0.1:$port;http://127.0.0.1:$adminPort"
}

$process = Start-Process -FilePath $openWebUiExe `
  -ArgumentList @("serve", "--host", "127.0.0.1", "--port", "$port") `
  -RedirectStandardOutput $stdoutLogPath `
  -RedirectStandardError $stderrLogPath `
  -WindowStyle Hidden `
  -PassThru

Write-PidFile -ServiceName "open-webui" -ProcessId $process.Id

if (!(Wait-ForHttpOk -Url "http://127.0.0.1:$port" -TimeoutSeconds 90)) {
  Stop-ServiceProcess -ServiceName "open-webui" -ExpectedPath $openWebUiExe -CommandPattern "open-webui" -Port $port | Out-Null
  throw "Open WebUI failed readiness check on http://127.0.0.1:$port"
}

Write-Output "Open WebUI started on port $port (pid=$($process.Id))"
