# start_bench.ps1 - launch the robot bench services in two persistent PowerShell windows.
#
#   1. robotics/arm_server.py            drives the follower, writes robotics/joint_state.json
#   2. console/bridge/stream_joints.py   pumps joint_state.json to Convex robot.pushTelemetry
#
# Each window auto-restarts its process if it exits (10 s backoff). Close a window to stop that
# service. Nothing in Claude Code or this script babysits them after launch.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File console\bridge\start_bench.ps1

$ErrorActionPreference = 'Stop'
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path          # ...\console\bridge
$laminar = (Resolve-Path (Join-Path $here '..\..')).Path            # ...\laminar
$venv    = Join-Path $laminar '.venv\Scripts\python.exe'

if (-not (Test-Path $venv)) {
    Write-Error "venv python not found at $venv - run 'python -m venv .venv' from $laminar first"
    exit 1
}

function Start-Loop {
    param(
        [string]$Title,       # window title + banner
        [string]$Cwd,         # working directory for the python process
        [string]$Script       # script path (relative to $Cwd) to run
    )

    # The child script runs in a persistent `powershell -NoExit` window and loops forever,
    # restarting the python process whenever it exits. Backticks (`$) escape vars so they
    # are evaluated inside the child window, not here.
    $cmd = @"
`$Host.UI.RawUI.WindowTitle = '$Title'
Set-Location '$Cwd'
while (`$true) {
    Write-Host '===== $Title : starting =====' -ForegroundColor Cyan
    & '$venv' -u '$Script'
    `$code = `$LASTEXITCODE
    Write-Host "===== $Title : exited with code `$code, restarting in 10 s (Ctrl+C to stop) =====" -ForegroundColor Yellow
    Start-Sleep -Seconds 10
}
"@

    Start-Process powershell.exe -ArgumentList '-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $cmd | Out-Null
}

Start-Loop -Title 'laminar: arm_server'    -Cwd (Join-Path $laminar 'robotics')       -Script 'arm_server.py'
Start-Sleep -Seconds 2                                                     # let arm_server print 'ready' before the pump starts polling
Start-Loop -Title 'laminar: stream_joints' -Cwd (Join-Path $laminar 'console\bridge') -Script 'stream_joints.py'

Write-Host "Two bench windows opened:"
Write-Host "  1. 'laminar: arm_server'    -> $laminar\robotics\arm_server.py"
Write-Host "  2. 'laminar: stream_joints' -> $laminar\console\bridge\stream_joints.py"
Write-Host "Close a window to stop that service."
