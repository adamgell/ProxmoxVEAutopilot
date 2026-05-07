@echo off
set OSDROOT=%ProgramData%\ProxmoxVEAutopilot\OSD
if not exist "%WINDIR%\Setup\Scripts" mkdir "%WINDIR%\Setup\Scripts" >nul 2>&1
echo [%DATE% %TIME%] ProxmoxVEAutopilot SetupComplete starting >> "%WINDIR%\Setup\Scripts\SetupComplete.log"
if exist "%OSDROOT%\OsdClient.ps1" (
  powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%OSDROOT%\OsdClient.ps1" >> "%WINDIR%\Setup\Scripts\SetupComplete.log" 2>&1
) else if exist "%WINDIR%\Setup\Scripts\FixRecoveryPartition.ps1" (
  echo [%DATE% %TIME%] OSD client missing; running legacy recovery fix >> "%WINDIR%\Setup\Scripts\SetupComplete.log"
  powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%WINDIR%\Setup\Scripts\FixRecoveryPartition.ps1" >> "%WINDIR%\Setup\Scripts\SetupComplete.log" 2>&1
) else (
  echo [%DATE% %TIME%] No OSD client or legacy recovery fix found >> "%WINDIR%\Setup\Scripts\SetupComplete.log"
)
echo [%DATE% %TIME%] ProxmoxVEAutopilot SetupComplete finished with %ERRORLEVEL% >> "%WINDIR%\Setup\Scripts\SetupComplete.log"
exit /b 0
