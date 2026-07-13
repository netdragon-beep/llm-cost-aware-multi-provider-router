$ErrorActionPreference = "Stop"

function Get-ProjectRoot {
  return (Split-Path -Parent $PSScriptRoot)
}

function Get-CondaEnvRoot {
  return "D:/conda/envs/llm-stack-local"
}

function Get-CondaPythonExe {
  return (Join-Path (Get-CondaEnvRoot) "python.exe")
}

function Get-CondaScriptsDir {
  return (Join-Path (Get-CondaEnvRoot) "Scripts")
}

function Get-CondaToolExe {
  param([string]$Name)
  return (Join-Path (Get-CondaScriptsDir) "$Name.exe")
}

function Get-LogDir {
  return (Join-Path (Get-ProjectRoot) "logs")
}

function Get-RunDir {
  return (Join-Path (Get-ProjectRoot) "run")
}

function Ensure-Directory {
  param([string]$Path)
  if (!(Test-Path $Path)) {
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
  }
}

function Load-DotEnv {
  param([string]$Path = (Join-Path (Get-ProjectRoot) ".env"))

  if (!(Test-Path $Path)) {
    throw "Missing env file: $Path"
  }

  $envMap = @{}
  Get-Content $Path | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
    $parts = $_ -split '=', 2
    if ($parts.Length -ne 2) { return }
    $name = $parts[0].Trim()
    $value = $parts[1]
    $envMap[$name] = $value
    [System.Environment]::SetEnvironmentVariable($name, $value)
    Set-Item -Path ("Env:" + $name) -Value $value
  }

  return $envMap
}

function Get-EnvValueOrDefault {
  param(
    [string]$Name,
    [string]$DefaultValue = ""
  )

  $value = [System.Environment]::GetEnvironmentVariable($Name)
  if ([string]::IsNullOrWhiteSpace($value)) {
    return $DefaultValue
  }
  return $value
}

function Assert-PathExists {
  param(
    [string]$Path,
    [string]$Label
  )

  if (!(Test-Path $Path)) {
    throw "$Label not found: $Path"
  }
}

function Assert-RequiredEnvValue {
  param([string]$Name)
  $value = [System.Environment]::GetEnvironmentVariable($Name)
  if ([string]::IsNullOrWhiteSpace($value)) {
    throw "Missing required environment variable: $Name"
  }
}

function Get-PidFilePath {
  param([string]$ServiceName)
  Ensure-Directory (Get-RunDir)
  return (Join-Path (Get-RunDir) "$ServiceName.pid")
}

function Write-PidFile {
  param(
    [string]$ServiceName,
    [int]$ProcessId
  )
  Set-Content -Path (Get-PidFilePath $ServiceName) -Value "$ProcessId" -Encoding ascii
}

function Read-PidFile {
  param([string]$ServiceName)
  $path = Get-PidFilePath $ServiceName
  if (!(Test-Path $path)) {
    return $null
  }
  $text = (Get-Content -Path $path -Raw).Trim()
  if ($text -notmatch '^\d+$') {
    return $null
  }
  return [int]$text
}

function Remove-PidFile {
  param([string]$ServiceName)
  $path = Get-PidFilePath $ServiceName
  if (Test-Path $path) {
    Remove-Item -LiteralPath $path -Force
  }
}

function Test-PortListening {
  param([int]$Port)
  return [bool](Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq $Port } | Select-Object -First 1)
}

function Get-PortOwnerPid {
  param([int]$Port)
  $match = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq $Port } | Select-Object -First 1
  if ($null -eq $match) {
    return $null
  }
  return $match.OwningProcess
}

function Get-ProcessCommandLine {
  param([int]$ProcessId)
  try {
    return (Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId").CommandLine
  } catch {
    return ""
  }
}

function Test-ProcessMatches {
  param(
    [int]$ProcessId,
    [string]$ExpectedPath = "",
    [string]$CommandPattern = ""
  )

  try {
    $proc = Get-Process -Id $ProcessId -ErrorAction Stop
    if ($ExpectedPath) {
      try {
        if ($proc.Path -and ([System.IO.Path]::GetFullPath($proc.Path) -eq [System.IO.Path]::GetFullPath($ExpectedPath))) {
          return $true
        }
      } catch {
      }
    }

    if ($CommandPattern) {
      $cmd = Get-ProcessCommandLine -ProcessId $ProcessId
      if ($cmd -and $cmd.ToLowerInvariant().Contains($CommandPattern.ToLowerInvariant())) {
        return $true
      }
    }
  } catch {
    return $false
  }

  return $false
}

function Stop-ServiceProcess {
  param(
    [string]$ServiceName,
    [string]$ExpectedPath = "",
    [string]$CommandPattern = "",
    [int]$Port = 0
  )

  $stopped = $false
  $servicePid = Read-PidFile $ServiceName
  if ($servicePid -and (Test-ProcessMatches -ProcessId $servicePid -ExpectedPath $ExpectedPath -CommandPattern $CommandPattern)) {
    try {
      Stop-Process -Id $servicePid -Force -ErrorAction Stop
      $stopped = $true
    } catch {
    }
  }

  if ($Port -gt 0) {
    $portPid = Get-PortOwnerPid -Port $Port
    if ($portPid -and (Test-ProcessMatches -ProcessId $portPid -ExpectedPath $ExpectedPath -CommandPattern $CommandPattern)) {
      try {
        Stop-Process -Id $portPid -Force -ErrorAction Stop
        $stopped = $true
      } catch {
      }
    }
  }

  Remove-PidFile $ServiceName
  return $stopped
}

function Wait-ForListeningPort {
  param(
    [int]$Port,
    [int]$TimeoutSeconds = 30
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-PortListening -Port $Port) {
      return $true
    }
    Start-Sleep -Milliseconds 500
  }

  return $false
}

function Wait-ForHttpOk {
  param(
    [string]$Url,
    [hashtable]$Headers = @{},
    [int]$TimeoutSeconds = 60
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    try {
      $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -Headers $Headers -TimeoutSec 10
      if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
        return $true
      }
    } catch {
    }
    Start-Sleep -Milliseconds 750
  }

  return $false
}
