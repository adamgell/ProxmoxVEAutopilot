@echo off
wpeinit
if exist X:\autopilot\drivers (
    drvload X:\autopilot\drivers\*\*.inf
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ". X:\autopilot\Invoke-AutopilotWinPE.ps1; Start-AutopilotWinPE"
