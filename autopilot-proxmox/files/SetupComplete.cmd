@echo off
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%WINDIR%\Setup\Scripts\FixRecoveryPartition.ps1" >> "%WINDIR%\Setup\Scripts\SetupComplete.log" 2>&1
exit /b 0
