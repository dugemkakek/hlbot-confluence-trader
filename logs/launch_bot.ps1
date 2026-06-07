# HLBot detached launcher with equity carry-over (v0.2.6+).
#
# Behavior:
#   1. Reads data/bot_equity.json (written by the running bot each cycle).
#   2. If valid, sets HL_EXECUTOR__INITIAL_BALANCE so the new process
#      starts with the prior session's cash equity.
#   3. Falls back to config/dev.yaml's initial_balance if the file is
#      missing or corrupt (e.g. first-ever start).
#   4. Spawns uvicorn detached via cmd /c with stdout+stderr redirected
#      to logs/bot_<date>.log, so the bot survives the launching shell
#      exiting.
#
# Usage:
#   powershell -File logs\launch_bot.ps1
#   (or)
#   Start-Process -FilePath logs\launch_bot.ps1 -WindowStyle Hidden

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$StateFile   = Join-Path $ProjectRoot 'data\bot_equity.json'
$LogDir      = Join-Path $ProjectRoot 'logs'
$LogFile     = Join-Path $LogDir ("bot_{0}.log" -f (Get-Date -Format 'yyyy-MM-dd'))
$CmdExe      = (Get-Command cmd.exe).Source
$PyLauncher  = (Get-Command py).Source
$PyArgs      = "-3.14 -X utf8 -m uvicorn src.api.main:create_app --factory --host 0.0.0.0 --port 8001"

# 1. Resolve initial_balance from state file (or fall back to YAML).
$InitialBalance = $null
if (Test-Path $StateFile) {
    try {
        $state = Get-Content $StateFile -Raw -Encoding utf8 | ConvertFrom-Json
        if ($state.PSObject.Properties.Name -contains 'last_equity' -and $state.last_equity -gt 0) {
            $InitialBalance = [math]::Round([double]$state.last_equity, 2)
            Write-Host "[launch] Carry-over equity from $StateFile : $InitialBalance"
            Write-Host "         (last update: $($state.last_update_utc), v$($state.bot_version))"
        }
    } catch {
        Write-Host "[launch] State file present but unparseable, falling back to config: $_"
    }
}
if ($null -eq $InitialBalance) {
    Write-Host "[launch] No state file. Using config/dev.yaml initial_balance."
}

# 2. Build the cmd /c line. Set HL_EXECUTOR_INITIAL_BALANCE in the
#    cmd's environment (inherited by py) so the config loader picks
#    it up via the HL_{SECTION}_{REST_OF_KEY}=value env-var path
#    (see src/utils/config.py: split on first underscore only,
#    so the section is "executor" and the subkey is the rest).
$envPrefix = ""
if ($null -ne $InitialBalance) {
    $bal = $InitialBalance.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    $envPrefix = "set HL_EXECUTOR_INITIAL_BALANCE=$bal&&"
}
$cmdLine = "cd /d `"$ProjectRoot`" && $envPrefix `"$PyLauncher`" $PyArgs >> `"$LogFile`" 2>&1"
Write-Host "[launch] cmd /c : $cmdLine"

# 3. Spawn detached. cmd /c waits for the inner command and exits;
#    the inner python process is detached and keeps running.
$proc = Start-Process -FilePath $CmdExe `
                       -ArgumentList "/c", $cmdLine `
                       -WindowStyle Hidden `
                       -PassThru
Write-Host "[launch] cmd.exe launcher PID: $($proc.Id)"

# 4. Wait briefly and check that python came up.
Start-Sleep -Seconds 3
$pyProcs = Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.Modules.FileName -like "*Python314*" -and $_.StartTime -gt (Get-Date).AddSeconds(-10) }
if ($pyProcs) {
    $pyProcs | Sort-Object StartTime -Descending | Select-Object -First 1 |
        ForEach-Object {
            Write-Host "[launch] bot python PID: $($_.Id), CPU=$($_.CPU)s, WS=$([math]::Round($_.WorkingSet64/1MB,1))MB"
            Set-Content -Path (Join-Path $LogDir 'bot.pid') -Value $_.Id -Encoding utf8
        }
} else {
    Write-Host "[launch] WARN: no Python 3.14 process detected within 3s. Check $LogFile"
}
