@echo off
echo [Autopilot] Launching Bootstrap.ps1 ...
"X:\Program Files\PowerShell\7\pwsh.exe" -NoProfile -ExecutionPolicy Bypass -File X:\autopilot\Bootstrap.ps1
echo [Autopilot] Bootstrap exited with code %ERRORLEVEL%
