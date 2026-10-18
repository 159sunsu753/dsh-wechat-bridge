# watchdog.ps1 - keep the WeChat <-> DSH bridge alive on the interactive desktop.
# Started hidden by start-bridge.cmd (and optionally at logon by install-autostart.cmd).
$ErrorActionPreference = 'SilentlyContinue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$state = Join-Path $here 'state'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$pidFile = Join-Path $state 'watchdog.pid'
$logFile = Join-Path $state 'watchdog.log'
$python = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if (!(Test-Path $python)) { $python = 'python' }

function Log($msg) {
  Add-Content -LiteralPath $logFile -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) -Encoding UTF8
}

# Single instance: if the recorded watchdog PID is alive, do nothing.
if (Test-Path $pidFile) {
  $old = (Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  if ($old -and ($old -as [int]) -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) {
    Log "another watchdog alive pid=$old; exit"
    exit 0
  }
}
Set-Content -LiteralPath $pidFile -Value $PID -Encoding ASCII
Log "watchdog start pid=$PID python=$python"

while ($true) {
  $bridge = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*bridge.py*' } |
    Select-Object -First 1
  if (-not $bridge) {
    Log 'bridge missing -> start'
    Start-Process -FilePath $python -ArgumentList 'bridge.py' -WorkingDirectory $here -WindowStyle Hidden
    Start-Sleep -Seconds 5
    $new = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -and $_.CommandLine -like '*bridge.py*' } |
      Select-Object -First 1
    if ($new) { Log "bridge started pid=$($new.ProcessId)" } else { Log 'bridge start failed' }
  }
  Start-Sleep -Seconds 15
}
