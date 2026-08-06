param(
  [Parameter(Mandatory = $true)]
  [string]$RuntimeManifestPath
)

$ErrorActionPreference = "Stop"

function Normalize-PathValue {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
  return [System.IO.Path]::GetFullPath($Value).TrimEnd('\\').ToLowerInvariant()
}

function Normalize-CommandLine {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
  return $Value.Trim()
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
  if ($null -eq $cimProcess -or (Normalize-PathValue $cimProcess.ExecutablePath) -cne $Record.ExecutablePath) {
    return $false
  }
  if ((Normalize-CommandLine $cimProcess.CommandLine) -cne $Record.CommandLine) {
    return $false
  }
  if ($null -eq $cimProcess.CreationDate -or $cimProcess.CreationDate.ToUniversalTime().ToString("o") -cne $Record.CreationTime) {
    return $false
  }
  if ($cimProcess.ProcessId -ne $Process.Id -or $startTime -cne $Record.StartTime -or $cimProcess.CreationDate.ToUniversalTime().ToString("o") -cne $startTime) {
    return $false
  }
  return $true
}

if (!(Test-Path -LiteralPath $RuntimeManifestPath)) {
  exit 0
}

try {
  $manifest = Get-Content -LiteralPath $RuntimeManifestPath -Raw | ConvertFrom-Json
  if ($manifest.Version -eq 1 -and $null -ne $manifest.Services) {
    foreach ($Record in $manifest.Services) {
      $process = $null
      try {
        $process = Get-Process -Id $Record.Pid -ErrorAction Stop
        if (Test-OwnedRecordMatches -Record $Record -Process $process) {
          $process.Kill()
          $process.WaitForExit()
        }
      } catch {} finally {
        if ($null -ne $process) { $process.Dispose() }
      }
    }
  }
} finally {
  Remove-Item -LiteralPath $RuntimeManifestPath -Force -ErrorAction SilentlyContinue
}
