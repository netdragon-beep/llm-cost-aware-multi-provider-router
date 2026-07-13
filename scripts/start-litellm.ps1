$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
$litellmExe = Get-CondaToolExe -Name "litellm"
$configPath = "$root/config/litellm.yaml"
$logDir = Get-LogDir
$stdoutLogPath = "$logDir/litellm.stdout.log"
$stderrLogPath = "$logDir/litellm.stderr.log"

Assert-PathExists -Path $litellmExe -Label "LiteLLM executable"
Assert-PathExists -Path $configPath -Label "LiteLLM config"
Load-DotEnv | Out-Null
Assert-RequiredEnvValue -Name "LITELLM_MASTER_KEY"
Ensure-Directory $logDir

$litellmPort = [int](Get-EnvValueOrDefault -Name "LITELLM_PORT" -DefaultValue "4100")
$existingPid = Get-PortOwnerPid -Port $litellmPort
if ($existingPid) {
  if (Test-ProcessMatches -ProcessId $existingPid -ExpectedPath $litellmExe -CommandPattern "litellm") {
    Write-PidFile -ServiceName "litellm" -ProcessId $existingPid
    Write-Output "LiteLLM already running on port $litellmPort (pid=$existingPid)"
    exit 0
  }
  throw "Port $litellmPort is already occupied by pid $existingPid, not a managed LiteLLM process."
}

$process = Start-Process -FilePath $litellmExe `
  -ArgumentList @("--config", $configPath, "--port", "$litellmPort", "--host", "127.0.0.1") `
  -RedirectStandardOutput $stdoutLogPath `
  -RedirectStandardError $stderrLogPath `
  -WindowStyle Hidden `
  -PassThru

Write-PidFile -ServiceName "litellm" -ProcessId $process.Id

$headers = @{ Authorization = "Bearer $env:LITELLM_MASTER_KEY" }
if (!(Wait-ForHttpOk -Url "http://127.0.0.1:$litellmPort/models" -Headers $headers -TimeoutSeconds 60)) {
  Stop-ServiceProcess -ServiceName "litellm" -ExpectedPath $litellmExe -CommandPattern "litellm" -Port $litellmPort | Out-Null
  throw "LiteLLM failed readiness check on http://127.0.0.1:$litellmPort/models"
}

Write-Output "LiteLLM started on port $litellmPort (pid=$($process.Id))"
