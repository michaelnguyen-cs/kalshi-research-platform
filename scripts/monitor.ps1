<#
.SYNOPSIS
    Watchdog-of-the-watchdog: every 10 minutes (run by the kbot-monitor
    scheduled task), checks that "kbot paper" is actually running, and
    restarts scripts\watchdog.ps1 if not.

.DESCRIPTION
    scripts\install-task.ps1 registers kbot-watchdog with an AtLogOn
    trigger plus a 5-minute repeating trigger so it survives sleep/
    hibernate without a full logout/login. This script exists in case
    THAT task itself fails to fire or fails to start its child process
    (Task Scheduler service hiccup, a corrupted task, etc.) -- it doesn't
    care why kbot paper isn't running, it just checks the fact, and if
    it's not running (and no watchdog.ps1 is already mid-restart-cooldown
    for it), starts watchdog.ps1 directly.

    Idempotent / safe to run every 10 minutes indefinitely: if kbot paper
    (or an active watchdog.ps1 about to restart it) is already running,
    this is a silent no-op.

.PARAMETER ProjectDir
    Root of the kbot project. Defaults to the parent of this script's directory.
#>
param(
    [string]$ProjectDir = ""
)

if (-not $ProjectDir) {
    $ProjectDir = Split-Path -Parent $PSScriptRoot
}

$LogDir = Join-Path $ProjectDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "monitor.log"

function Write-MonitorLog([string]$msg) {
    $ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "$ts  $msg"
}

function Send-Toast([string]$title, [string]$body) {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $n = New-Object System.Windows.Forms.NotifyIcon
        $n.Icon            = [System.Drawing.SystemIcons]::Warning
        $n.BalloonTipTitle = $title
        $n.BalloonTipText  = $body
        $n.Visible         = $true
        $n.ShowBalloonTip(8000)
        Start-Sleep -Seconds 1
        $n.Dispose()
    } catch {
        Write-MonitorLog "WARN: toast notification failed: $_"
    }
}

# Get-Process doesn't expose CommandLine -- Win32_Process via CIM does.
# kbot is installed as a console_scripts entry point, so the actual running
# process is named kbot.exe (a generated launcher stub), NOT python.exe --
# verified directly against the live process (PID 19288: Name=kbot.exe).
# Checking both names is cheap insurance against a differently-installed
# environment (e.g. `python -m kbot` would show up as python.exe instead).
$paperRunning = Get-CimInstance Win32_Process -Filter "Name = 'kbot.exe' OR Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*paper*" }

if ($paperRunning) {
    exit 0  # healthy -- silent no-op, don't spam the log every 10 minutes
}

# kbot paper isn't up. Before restarting, check whether a watchdog.ps1
# process is already alive -- if so it's likely just mid-cooldown between
# a crash and its own restart (watchdog.ps1's RestartCooldownSeconds), and
# starting a second one here would race it into launching two "kbot paper"
# instances at once.
$watchdogAlive = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*watchdog.ps1*" }

if ($watchdogAlive) {
    Write-MonitorLog "kbot paper not found, but watchdog.ps1 is already running (PID $($watchdogAlive[0].ProcessId)) -- likely mid-restart, not intervening."
    exit 0
}

Write-MonitorLog "ALERT: no 'kbot paper' process and no active watchdog.ps1 found -- starting watchdog.ps1"
Send-Toast "kbot-monitor" "No kbot paper process found -- restarting watchdog."

$watchdogScript = Join-Path $ProjectDir "scripts\watchdog.ps1"
if (-not (Test-Path $watchdogScript)) {
    Write-MonitorLog "ERROR: watchdog.ps1 not found at $watchdogScript -- cannot recover."
    exit 1
}

Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File `"$watchdogScript`"" `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Hidden

Write-MonitorLog "Started watchdog.ps1."
