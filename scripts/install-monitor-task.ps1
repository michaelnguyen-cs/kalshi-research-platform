<#
.SYNOPSIS
    Registers kbot-monitor as a Windows Task Scheduler task -- the
    watchdog-of-the-watchdog (2026-07).

.DESCRIPTION
    Creates (or overwrites) a per-user scheduled task named "kbot-monitor"
    that runs scripts\monitor.ps1 every 10 minutes, indefinitely, starting
    at registration time. monitor.ps1 checks whether a "kbot paper" process
    (kbot.exe, the console_scripts launcher -- see monitor.ps1 for why not
    python.exe) is actually running and, if not (and no watchdog.ps1 is
    already mid-restart), starts scripts\watchdog.ps1 directly and logs to
    logs\monitor.log + sends a Windows toast.

    This exists because kbot-watchdog (scripts\install-task.ps1) is itself
    a single scheduled task -- if the Task Scheduler service, the task
    registration, or the AtLogOn/5-min triggers all somehow fail to fire
    kbot-watchdog, nothing would notice. kbot-monitor is a second,
    independent task with its own 10-minute trigger, so a kbot-watchdog
    failure gets caught and repaired within 10 minutes even if the reason
    kbot-watchdog itself failed would have also stopped it recovering.

    Registered via `schtasks /create /xml` for the same reason as
    install-task.ps1: an omitted <Duration> in the <Repetition> block is
    the literal Task Scheduler XML schema's "repeat forever".

    Requirements:
      * Run as the user who will own the task (no admin needed).

    To remove the task later:
      Unregister-ScheduledTask -TaskName "kbot-monitor" -Confirm:$false
#>
param(
    [string]$TaskName   = "kbot-monitor",
    [string]$ProjectDir = ""
)

if (-not $ProjectDir) {
    $ProjectDir = Split-Path -Parent $PSScriptRoot
}

$scriptPath = Join-Path $ProjectDir "scripts\monitor.ps1"

if (-not (Test-Path $scriptPath)) {
    Write-Error "monitor.ps1 not found at: $scriptPath"
    exit 1
}

$userId = "$env:USERDOMAIN\$env:USERNAME"
$psArgs = "-ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File `"$scriptPath`""

function ConvertTo-XmlText([string]$text) {
    return $text.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace('"', "&quot;")
}

$startBoundary = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")

$taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>kbot-monitor -- watchdog-of-the-watchdog. Every 10 min, checks that a "kbot paper" process is running and restarts watchdog.ps1 if not.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT10M</Interval>
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
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
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

$xmlPath = Join-Path ([System.IO.Path]::GetTempPath()) "kbot-monitor-task.xml"
# See install-task.ps1 for why: schtasks's XML parser wants UTF-16LE with BOM,
# not UTF-8 (with or without BOM).
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
Write-Host "  Trigger   : every 10 min, indefinite"
Write-Host "  Action    : powershell.exe $psArgs"
Write-Host "  WorkingDir: $ProjectDir"
Write-Host ""
Write-Host "To start it now: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To inspect it:   schtasks /query /tn '$TaskName' /fo LIST /v"
Write-Host "Logs:            logs\monitor.log"
Write-Host "To remove it:    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
