<#
.SYNOPSIS
    Watchdog for kbot paper trading -- auto-restarts on crash, alerts on stale heartbeat.

.DESCRIPTION
    Run this once in a terminal and leave it. It:
      * Restarts kbot paper whenever it exits (30s cool-down between restarts).
      * Sends a Windows toast notification on each crash/restart.
      * Alerts (toast + log) if the heartbeat file goes stale (> 10 min without update),
        which means the strategy loop has frozen without crashing.
      * Rotates log files by date so the logs directory stays manageable.

.PARAMETER Strategy
    Strategy slug passed to kbot paper (default: kxbtc15m).

.PARAMETER Series
    Market series passed to kbot paper --series (default: KXBTC15M).

.PARAMETER ProjectDir
    Root of the kbot project. Defaults to the parent of this script's directory.

.PARAMETER HeartbeatMaxAgeSeconds
    Alert threshold for stale heartbeat. Default 600 (10 minutes).

.PARAMETER RestartCooldownSeconds
    Seconds to wait between crash and restart. Default 30.

.EXAMPLE
    # Run from the kbot project root:
    powershell -ExecutionPolicy Bypass -File scripts\watchdog.ps1

    # Or with custom params:
    powershell -ExecutionPolicy Bypass -File scripts\watchdog.ps1 -Series KXBTC15M
#>
param(
    [string]$Strategy              = "kxbtc15m",
    [string]$Series                = "KXBTC15M",
    [string]$ProjectDir            = "",
    [int]   $HeartbeatMaxAgeSeconds = 600,
    [int]   $RestartCooldownSeconds = 30
)

# -- Setup -------------------------------------------------------------------

if (-not $ProjectDir) {
    $ProjectDir = Split-Path -Parent $PSScriptRoot
}
Set-Location $ProjectDir

$LogDir = Join-Path $ProjectDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$HeartbeatFile = Join-Path $ProjectDir "kbot_heartbeat.json"
$kbot          = (Get-Command kbot -ErrorAction Stop).Source

function Get-LogFile {
    $date = Get-Date -Format "yyyyMMdd"
    Join-Path $LogDir "watchdog_${date}.log"
}

function Write-WatchdogLog($msg) {
    $ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts  $msg"
    Write-Host $line
    Add-Content -Path (Get-LogFile) -Value $line
}

function Send-Toast($title, $body) {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon             = [System.Drawing.SystemIcons]::Warning
        $n.BalloonTipTitle  = $title
        $n.BalloonTipText   = $body
        $n.Visible          = $true
        $n.ShowBalloonTip(8000)
        Start-Sleep -Seconds 1
        $n.Dispose()
    } catch {
        Write-WatchdogLog "WARN: toast notification failed: $_"
    }
}

function Get-HeartbeatAge {
    if (-not (Test-Path $HeartbeatFile)) { return [int]::MaxValue }
    try {
        $hb  = Get-Content $HeartbeatFile | ConvertFrom-Json
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        return $now - [int]$hb.ts
    } catch {
        return [int]::MaxValue
    }
}

# -- Duplicate-instance guard (2026-07) ---------------------------------------
# schtasks's MultipleInstancesPolicy=IgnoreNew (see install-task.ps1) only
# stops the SAME scheduled task overlapping itself -- it has no idea a
# manually-started watchdog.ps1 (or one launched by kbot-monitor, see
# monitor.ps1) is already running outside Task Scheduler's own bookkeeping.
# Without this check, every AtLogon/5-min trigger firing while a manual
# watchdog is already active would spawn a second, fully independent
# "kbot paper" process trading the same account concurrently. Checked once
# at startup, before entering the restart loop -- exits immediately (does
# NOT touch the existing instance) if either the trading process or another
# watchdog.ps1 is already alive.
function Test-AlreadyRunning {
    $existingKbot = Get-CimInstance Win32_Process -Filter "Name = 'kbot.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*paper*" }
    if ($existingKbot) {
        Write-WatchdogLog "Already running: kbot.exe PID $($existingKbot[0].ProcessId) is trading -- exiting without starting a duplicate."
        return $true
    }

    $existingWatchdog = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*watchdog.ps1*" -and $_.ProcessId -ne $PID }
    if ($existingWatchdog) {
        Write-WatchdogLog "Already running: watchdog.ps1 PID $($existingWatchdog[0].ProcessId) is active -- exiting without starting a duplicate."
        return $true
    }

    return $false
}

if (Test-AlreadyRunning) {
    exit 0
}

# -- Main restart loop -------------------------------------------------------

Write-WatchdogLog "=== watchdog starting  strategy=$Strategy  series=$Series ==="
$run = 0

while ($true) {
    $run++
    $ts      = Get-Date -Format "yyyyMMdd_HHmmss"
    $outFile = Join-Path $LogDir "paper_${ts}.log"
    $errFile = Join-Path $LogDir "paper_err_${ts}.log"

    Write-WatchdogLog "Starting kbot paper (run #$run)  log=$outFile"

    $proc = Start-Process `
        -FilePath $kbot `
        -ArgumentList "--verbose paper --strategy $Strategy --series $Series" `
        -WorkingDirectory $ProjectDir `
        -RedirectStandardOutput $outFile `
        -RedirectStandardError  $errFile `
        -PassThru -NoNewWindow

    Write-WatchdogLog "PID $($proc.Id) started"

    # Poll while the process runs: check heartbeat staleness every 2 minutes.
    $lastHeartbeatAlert = 0
    while (-not $proc.HasExited) {
        Start-Sleep -Seconds 120

        if ($proc.HasExited) { break }

        $age = Get-HeartbeatAge
        if ($age -gt $HeartbeatMaxAgeSeconds) {
            $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            # Throttle alerts to once per 10 minutes.
            if (($now - $lastHeartbeatAlert) -gt 600) {
                $msg = "kbot heartbeat is ${age}s old -- strategy loop may be frozen (PID $($proc.Id))"
                Write-WatchdogLog "STALE HEARTBEAT: $msg"
                Send-Toast "kbot heartbeat stale" $msg
                $lastHeartbeatAlert = $now
            }
        }
    }

    $exitCode = $proc.ExitCode
    Write-WatchdogLog "PID $($proc.Id) exited with code $exitCode"

    # Exit code 0 = clean shutdown (Ctrl-C). Don't restart, exit the watchdog too.
    if ($exitCode -eq 0) {
        Write-WatchdogLog "Clean exit -- watchdog stopping."
        break
    }

    $msg = "kbot paper crashed (exit $exitCode) -- restarting in ${RestartCooldownSeconds}s"
    Write-WatchdogLog $msg
    Send-Toast "kbot paper crashed" $msg

    Start-Sleep -Seconds $RestartCooldownSeconds
}

Write-WatchdogLog "=== watchdog stopped ==="
