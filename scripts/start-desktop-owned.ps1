param(
  [Parameter(Mandatory = $true)]
  [string]$RuntimeManifestPath
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")

$root = Get-ProjectRoot
$pythonExe = Get-CondaPythonExe
$litellmExe = Get-CondaToolExe -Name "litellm"
$logDir = Get-LogDir
$dataDir = "$root/data/open-webui"
$configPath = "$root/config/litellm.yaml"
$claudeConfigPath = "$root/config/litellm-claude.yaml"
$adminPanelDir = "$root/admin-panel"
$adminAppPath = "$adminPanelDir/app.py"

Load-DotEnv | Out-Null
Assert-PathExists -Path $pythonExe -Label "Python executable"
Assert-PathExists -Path $litellmExe -Label "LiteLLM executable"
Assert-PathExists -Path $configPath -Label "LiteLLM config"
Assert-PathExists -Path $claudeConfigPath -Label "Claude LiteLLM config"
Assert-PathExists -Path $adminAppPath -Label "Admin panel app"
Assert-RequiredEnvValue -Name "LITELLM_MASTER_KEY"
Assert-RequiredEnvValue -Name "OPEN_WEBUI_SECRET_KEY"
Ensure-Directory $logDir
Ensure-Directory $dataDir
Ensure-Directory (Split-Path -Parent $RuntimeManifestPath)

$litellmPort = [int](Get-EnvValueOrDefault -Name "LITELLM_PORT" -DefaultValue "4100")
$claudeGatewayPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_PORT" -DefaultValue "4101")
$claudeInternalPort = [int](Get-EnvValueOrDefault -Name "CLAUDE_LITELLM_INTERNAL_PORT" -DefaultValue "4102")
$openWebUiPort = [int](Get-EnvValueOrDefault -Name "OPEN_WEBUI_PORT" -DefaultValue "8090")
$adminPort = [int](Get-EnvValueOrDefault -Name "ADMIN_PANEL_PORT" -DefaultValue "8091")
$headers = @{ Authorization = "Bearer $env:LITELLM_MASTER_KEY" }

function Write-DesktopManifest {
  param([array]$Records)

  $temporaryPath = "$RuntimeManifestPath.$PID.tmp"
  $manifest = [ordered]@{
    Version = 1
    Services = $Records
  }
  try {
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $temporaryPath -Encoding utf8
    Move-Item -LiteralPath $temporaryPath -Destination $RuntimeManifestPath -Force
  } finally {
    if (Test-Path -LiteralPath $temporaryPath) {
      Remove-Item -LiteralPath $temporaryPath -Force
    }
  }
}

function Normalize-ExecutablePath {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
  return [System.IO.Path]::GetFullPath($Value).TrimEnd('\\').ToLowerInvariant()
}

function Normalize-CommandLine {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
  return $Value.Trim()
}

function Get-OwnedProcessRecord {
  param(
    [string]$Name,
    [System.Diagnostics.Process]$Process
  )

  try {
    $Process.Refresh()
    if ($Process.HasExited) {
      throw "Newly started $Name process $($Process.Id) exited before its identity could be captured."
    }
    $processId = $Process.Id
    $startTime = $Process.StartTime.ToUniversalTime().ToString("o")
  } catch {
    throw "Could not read the process-object identity for newly started $Name process: $($_.Exception.Message)"
  }

  $cimProcess = $null
  for ($attempt = 0; $attempt -lt 10 -and $null -eq $cimProcess; $attempt += 1) {
    $cimProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $cimProcess) { Start-Sleep -Milliseconds 100 }
  }
  if ($null -eq $cimProcess) {
    throw "Could not inspect newly started $Name process $processId."
  }

  $executablePath = Normalize-ExecutablePath -Value $cimProcess.ExecutablePath
  $commandLine = Normalize-CommandLine -Value $cimProcess.CommandLine
  $creationTime = if ($null -ne $cimProcess.CreationDate) { $cimProcess.CreationDate.ToUniversalTime().ToString("o") } else { $null }
  if ($cimProcess.ProcessId -ne $processId -or [string]::IsNullOrWhiteSpace($executablePath) -or [string]::IsNullOrWhiteSpace($commandLine) -or [string]::IsNullOrWhiteSpace($creationTime) -or [string]::IsNullOrWhiteSpace($startTime) -or $creationTime -cne $startTime) {
    throw "Could not capture a matching complete process identity for newly started $Name process $processId."
  }

  try {
    $Process.Refresh()
    if ($Process.HasExited -or $Process.Id -ne $processId -or $Process.StartTime.ToUniversalTime().ToString("o") -cne $startTime) {
      throw "Newly started $Name process $processId changed or exited during identity capture."
    }
  } catch {
    throw "Could not confirm the process-object identity for newly started $Name process ${processId}: $($_.Exception.Message)"
  }

  return [ordered]@{
    Name = $Name
    Pid = $processId
    ExecutablePath = $executablePath
    CommandLine = $commandLine
    CreationTime = $creationTime
    StartTime = $startTime
  }
}

function Test-OwnedRecordMatches {
  param(
    $Record,
    [System.Diagnostics.Process]$Process
  )
  if ($null -eq $Record -or $Record.Pid -le 0 -or [string]::IsNullOrWhiteSpace($Record.ExecutablePath) -or [string]::IsNullOrWhiteSpace($Record.CommandLine) -or [string]::IsNullOrWhiteSpace($Record.CreationTime) -or [string]::IsNullOrWhiteSpace($Record.StartTime)) {
    return $false
  }

  try {
    $Process.Refresh()
    if ($Process.HasExited -or $Process.Id -ne $Record.Pid) { return $false }
    $startTime = $Process.StartTime.ToUniversalTime().ToString("o")
  } catch {
    return $false
  }

  $cimProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($Record.Pid)" -ErrorAction SilentlyContinue
  if ($null -eq $cimProcess) { return $false }
  $creationTime = if ($null -ne $cimProcess.CreationDate) { $cimProcess.CreationDate.ToUniversalTime().ToString("o") } else { $null }

  return $cimProcess.ProcessId -eq $Process.Id -and
    (Normalize-ExecutablePath -Value $cimProcess.ExecutablePath) -ceq $Record.ExecutablePath -and
    (Normalize-CommandLine -Value $cimProcess.CommandLine) -ceq $Record.CommandLine -and
    $creationTime -ceq $Record.CreationTime -and
    $startTime -ceq $Record.StartTime -and
    $creationTime -ceq $startTime
}

function Stop-OwnedRecords {
  param([array]$Records)
  foreach ($record in $Records) {
    $process = $null
    try {
      $process = Get-Process -Id $record.Pid -ErrorAction Stop
      if (Test-OwnedRecordMatches -Record $record -Process $process) {
        $process.Kill()
        $process.WaitForExit()
      }
    } catch {} finally {
      if ($null -ne $process) { $process.Dispose() }
    }
  }
}

function Stop-TemporaryStartedProcesses {
  param([array]$Processes)
  foreach ($Process in $Processes) {
    if ($null -eq $Process) { continue }
    try {
      $Process.Refresh()
      if (!$Process.HasExited) {
        $Process.Kill()
        $Process.WaitForExit()
      }
    } catch {} finally {
      $Process.Dispose()
    }
  }
}

$env:RELAYDECK_LITELLM_DISCOVERY_PATCH = "1"
$existingPythonPath = [string]$env:PYTHONPATH
$env:PYTHONPATH = if ($existingPythonPath) { "$root;$existingPythonPath" } else { $root }

# Replacing the manifest before inspecting ports prevents a reuse-only launch from inheriting stale ownership.
Write-DesktopManifest -Records @()

$services = @(
  [ordered]@{ Name = "LiteLLM"; Port = $litellmPort; FilePath = $litellmExe; Arguments = @("--config", $configPath, "--port", "$litellmPort", "--host", "127.0.0.1"); Stdout = "$logDir/litellm.stdout.log"; Stderr = "$logDir/litellm.stderr.log"; HealthUrl = "http://127.0.0.1:$litellmPort/models"; Headers = $headers; Timeout = 60 },
  [ordered]@{ Name = "Claude internal LiteLLM"; Port = $claudeInternalPort; FilePath = $litellmExe; Arguments = @("--config", $claudeConfigPath, "--port", "$claudeInternalPort", "--host", "127.0.0.1"); Stdout = "$logDir/litellm-claude.stdout.log"; Stderr = "$logDir/litellm-claude.stderr.log"; HealthUrl = "http://127.0.0.1:$claudeInternalPort/models"; Headers = $headers; Timeout = 60 },
  [ordered]@{ Name = "Claude Desktop gateway"; Port = $claudeGatewayPort; FilePath = $pythonExe; Arguments = @("-m", "uvicorn", "claude_desktop_gateway:app", "--app-dir", $root, "--host", "127.0.0.1", "--port", "$claudeGatewayPort"); Stdout = "$logDir/claude-desktop-gateway.stdout.log"; Stderr = "$logDir/claude-desktop-gateway.stderr.log"; HealthUrl = "http://127.0.0.1:$claudeGatewayPort/v1/models"; Headers = $headers; Timeout = 60 },
  [ordered]@{ Name = "Open WebUI"; Port = $openWebUiPort; FilePath = $pythonExe; Arguments = @("-m", "uvicorn", "open_webui.main:app", "--host", "127.0.0.1", "--port", "$openWebUiPort", "--loop", "none"); Stdout = "$logDir/open-webui.stdout.log"; Stderr = "$logDir/open-webui.stderr.log"; HealthUrl = "http://127.0.0.1:$openWebUiPort"; Headers = @{}; Timeout = 90 },
  [ordered]@{ Name = "Admin panel"; Port = $adminPort; FilePath = $pythonExe; Arguments = @("-m", "uvicorn", "app:app", "--app-dir", $adminPanelDir, "--host", "127.0.0.1", "--port", "$adminPort"); Stdout = "$logDir/admin-panel.stdout.log"; Stderr = "$logDir/admin-panel.stderr.log"; HealthUrl = "http://127.0.0.1:$adminPort/api/health"; Headers = @{}; Timeout = 60 }
)

$ownedRecords = @()
$temporaryStartedProcesses = @()
try {
  foreach ($Service in $services) {
    if (Test-PortListening -Port $Service.Port) {
      Write-Output "$($Service.Name) already listening on port $($Service.Port); reusing it."
      continue
    }

    if ($Service.Name -eq "Open WebUI") {
      $env:FROM_INIT_PY = "true"
      $env:DATA_DIR = $dataDir
      $env:OPENAI_API_BASE_URL = "http://127.0.0.1:$litellmPort/v1"
      $env:OPENAI_API_KEY = $env:LITELLM_MASTER_KEY
      $env:WEBUI_SECRET_KEY = $env:OPEN_WEBUI_SECRET_KEY
      $env:RAG_OPENAI_API_BASE_URL = $env:OPENAI_API_BASE_URL
      $env:RAG_OPENAI_API_KEY = $env:OPENAI_API_KEY
      $env:USER_AGENT = Get-EnvValueOrDefault -Name "USER_AGENT" -DefaultValue "RelayDeckLocal/1.0"
      if ([string]::IsNullOrWhiteSpace($env:CORS_ALLOW_ORIGIN)) {
        $env:CORS_ALLOW_ORIGIN = "http://127.0.0.1:$openWebUiPort;http://127.0.0.1:$adminPort"
      }
    }
    if ($Service.Name -eq "Claude Desktop gateway") {
      $env:CLAUDE_LITELLM_INTERNAL_PORT = "$claudeInternalPort"
    }

    $process = Start-Process -FilePath $Service.FilePath -ArgumentList $Service.Arguments -WorkingDirectory $root -RedirectStandardOutput $Service.Stdout -RedirectStandardError $Service.Stderr -WindowStyle Hidden -PassThru
    $temporaryStartedProcesses += $process
    $ownedRecords += Get-OwnedProcessRecord -Name $Service.Name -Process $process
    Write-DesktopManifest -Records $ownedRecords

    if (!(Wait-ForHttpOk -Url $Service.HealthUrl -Headers $Service.Headers -TimeoutSeconds $Service.Timeout)) {
      throw "$($Service.Name) failed readiness check on $($Service.HealthUrl)."
    }
    Write-Output "$($Service.Name) started on port $($Service.Port) (pid=$($process.Id))."
  }
} catch {
  Stop-TemporaryStartedProcesses -Processes $temporaryStartedProcesses
  Stop-OwnedRecords -Records $ownedRecords
  if (Test-Path -LiteralPath $RuntimeManifestPath) {
    Remove-Item -LiteralPath $RuntimeManifestPath -Force
  }
  throw
}
