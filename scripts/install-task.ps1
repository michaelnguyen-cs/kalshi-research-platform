<#
.SYNOPSIS
    Registers kbot-watchdog as a Windows Task Scheduler task.

.DESCRIPTION
    Creates (or overwrites) a per-user scheduled task named "kbot-watchdog"
    that launches scripts\watchdog.ps1, hidden, with no timeout.

    2026-07 fix: the task previously had a single "At logon" trigger, which
    does NOT re-fire after sleep/hibernate (only after a full logout/login)
    -- so a laptop that slept overnight silently lost its watchdog until
    someone logged out and back in. This registers TWO triggers:
      1. AtLogOn        -- starts the watchdog on interactive logon (unchanged).
      2. TimeTrigger     -- repeats every 5 minutes, indefinitely, starting at
                            registration time. MultipleInstancesPolicy=IgnoreNew
                            means this is a no-op re-check while the watchdog
                            is already running, and a real recovery within 5
                            minutes of it not being (e.g. after a sleep/wake
                            cycle that didn't trigger AtLogOn).

    Registered via `schtasks /create /xml` (not the ScheduledTask cmdlets)
    specifically so the TimeTrigger's <Repetition> block can omit <Duration>
    -- that is the literal Task Scheduler XML schema's "repeat forever"
    representation; New-ScheduledTaskTrigger's -RepetitionDuration parameter
    has no clean way to express "indefinite".

    Requirements:
      * Run as the user who will own the task (no admin needed).
      * kbot must be reachable on the user's PATH in an interactive session.

    To remove the task later:
      Unregister-ScheduledTask -TaskName "kbot-watchdog" -Confirm:$false
#>
param(
    [string]$TaskName   = "kbot-watchdog",
    [string]$ProjectDir = "",
    # 2026-07: default live strategy is kxbtc15m (BTC 15-minute) -- btc_hourly
    # retired (no discriminating signal on 60-minute contracts). Passed
    # explicitly to watchdog.ps1 so the scheduled task pins the series rather
    # than silently inheriting watchdog.ps1's own defaults.
    [string]$Strategy   = "kxbtc15m",
    [string]$Series     = "KXBTC15M"
)

if (-not $ProjectDir) {
    $ProjectDir = Split-Path -Parent $PSScriptRoot
}

$scriptPath = Join-Path $ProjectDir "scripts\watchdog.ps1"

if (-not (Test-Path $scriptPath)) {
    Write-Error "watchdog.ps1 not found at: $scriptPath"
    exit 1
}

$userId = "$env:USERDOMAIN\$env:USERNAME"

# -- Action arguments ----------------------------------------------------------
# -WindowStyle Hidden  : no console window on login
# -NonInteractive      : suppress any prompts
# -ExecutionPolicy Bypass : run unsigned local scripts
$psArgs = "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File `"$scriptPath`" -Strategy $Strategy -Series $Series"

# XML special characters in paths/args are escaped defensively even though
# none of these values are expected to contain them.
function ConvertTo-XmlText([string]$text) {
    return $text.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
}

$startBoundary = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>kbot paper trading watchdog -- auto-restarts kbot on crash, alerts on stale heartbeat. Re-checks every 5 min so it recovers from sleep/hibernate without needing a full logout/login.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$(ConvertTo-XmlText $userId)</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>$startBoundary</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$(ConvertTo-XmlText $userId)</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>$(ConvertTo-XmlText $psArgs)</Arguments>
      <WorkingDirectory>$(ConvertTo-XmlText $ProjectDir)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$xmlPath = Join-Path ([System.IO.Path]::GetTempPath()) "kbot-watchdog-task.xml"
# schtasks's XML parser is picky: it rejects a UTF-8 BOM outright ("incorrect
# document syntax") and rejects UTF-8 with no BOM too ("unable to switch the
# encoding") since it can't confirm the declared encoding without one. UTF-16LE
# with BOM (Task Scheduler's own native export format) is what it actually wants.
$utf16 = New-Object System.Text.UnicodeEncoding($false, $true)  # little-endian, emit BOM
[System.IO.File]::WriteAllText($xmlPath, $taskXml, $utf16)

$result = schtasks /create /tn "$TaskName" /xml "$xmlPath" /f
if ($LASTEXITCODE -ne 0) {
    Write-Error "schtasks /create failed (exit $LASTEXITCODE):`n$result"
    Remove-Item $xmlPath -ErrorAction SilentlyContinue
    exit 1
}
Remove-Item $xmlPath -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Task registered: $TaskName"
Write-Host "  Run as    : $userId"
Write-Host "  Triggers  : AtLogOn + every 5 min (indefinite, survives sleep/hibernate)"
Write-Host "  Action    : powershell.exe $psArgs"
Write-Host "  WorkingDir: $ProjectDir"
Write-Host ""
Write-Host "The task will start automatically on next login, and re-check every 5 minutes thereafter."
Write-Host "To start it now without logging out:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To inspect it:"
Write-Host "  schtasks /query /tn '$TaskName' /fo LIST /v"
Write-Host "To remove it:"
Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
