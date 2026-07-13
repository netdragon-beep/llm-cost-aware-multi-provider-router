$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
$pythonExe = Get-CondaPythonExe
$appPath = "$root/admin-panel/app.py"
$logDir = Get-LogDir
$stdoutLogPath = "$logDir/admin-panel.stdout.log"
$stderrLogPath = "$logDir/admin-panel.stderr.log"

Assert-PathExists -Path $pythonExe -Label "Python executable"
Assert-PathExists -Path $appPath -Label "Admin panel app"
Load-DotEnv | Out-Null
Ensure-Directory $logDir

$adminPort = [int](Get-EnvValueOrDefault -Name "ADMIN_PANEL_PORT" -DefaultValue "8091")
$adminPanelDir = "$root/admin-panel"
$existingPid = Get-PortOwnerPid -Port $adminPort
if ($existingPid) {
  if (Test-ProcessMatches -ProcessId $existingPid -ExpectedPath $pythonExe -CommandPattern $adminPanelDir) {
    Write-PidFile -ServiceName "admin-panel" -ProcessId $existingPid
    Write-Output "Admin panel already running on port $adminPort (pid=$existingPid)"
    exit 0
  }
  throw "Port $adminPort is already occupied by pid $existingPid, not a managed admin panel process."
}

$process = Start-Process -FilePath $pythonExe `
  -ArgumentList @("-m", "uvicorn", "app:app", "--app-dir", $adminPanelDir, "--host", "127.0.0.1", "--port", "$adminPort") `
  -RedirectStandardOutput $stdoutLogPath `
  -RedirectStandardError $stderrLogPath `
  -WindowStyle Hidden `
  -PassThru

Write-PidFile -ServiceName "admin-panel" -ProcessId $process.Id

if (!(Wait-ForHttpOk -Url "http://127.0.0.1:$adminPort/api/health" -TimeoutSeconds 60)) {
  Stop-ServiceProcess -ServiceName "admin-panel" -ExpectedPath $pythonExe -CommandPattern $adminPanelDir -Port $adminPort | Out-Null
  throw "Admin panel failed readiness check on http://127.0.0.1:$adminPort/api/health"
}

Write-Output "Admin panel started on port $adminPort (pid=$($process.Id))"
