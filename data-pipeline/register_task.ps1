# Register the Adventure Map auto-import as a Windows Scheduled Task.
# Run this ONCE in PowerShell (no admin needed for a user-level task):
#     powershell -ExecutionPolicy Bypass -File register_task.ps1
#
# Re-running updates the existing task. Default: every day at 20:00 (8 PM).
# For weekly instead, change $Trigger below (see the commented example).
# setup.ps1 calls this with -Python pointing at the pipeline venv.

param([string]$Python = 'C:\uv\venvs\adventure-map\Scripts\python.exe')

$ErrorActionPreference = 'Stop'

$TaskName = 'Adventure Map Import'
$PipeDir  = $PSScriptRoot                      # .../data-pipeline
if (-not (Test-Path $Python)) { throw "Python not found at $Python - run setup.ps1 first" }
$Script   = Join-Path $PipeDir 'auto_import.py'
$LogDir   = Join-Path $PipeDir 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# A wrapper .cmd so the task logs each run to its own timestamped file.
$Wrapper = Join-Path $PipeDir 'run_auto_import.cmd'
@"
@echo off
cd /d "$PipeDir"
rem wmic is gone from current Windows 11 builds; ask PowerShell for the timestamp
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%I
set PYTHONUTF8=1
"$Python" "$Script" %* > "$LogDir\import_%STAMP%.log" 2>&1
"@ | Set-Content -Path $Wrapper -Encoding ASCII

# --- Trigger: daily at 20:00 (8 PM) ------------------------------------------
$Trigger = New-ScheduledTaskTrigger -Daily -At 8:00pm
# Weekly instead (e.g. Sundays 02:00):
# $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 2:00am

$Action = New-ScheduledTaskAction -Execute $Wrapper -WorkingDirectory $PipeDir

# Run whether or not the user is logged on is nice, but needs a stored password.
# Simpler + reliable for a personal desktop: run only when this user is logged on,
# wake the machine if asleep, and don't stop on AC/battery transitions.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
$Settings.DisallowStartIfOnBatteries = $false
$Settings.StopIfGoingOnBatteries = $false

Register-ScheduledTask -TaskName $TaskName -Trigger $Trigger -Action $Action `
    -Settings $Settings -Description 'Scan Camera Roll, import new geotagged photos, rebuild tiles, deploy.' `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' (daily 20:00)." -ForegroundColor Green
Write-Host "Run it now to test:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Logs land in:        $LogDir"
Write-Host "Change cadence in Task Scheduler, or edit the `$Trigger line and re-run this script."
