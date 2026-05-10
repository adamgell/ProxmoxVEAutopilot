wpeinit
powercfg /setacvalueindex scheme_current sub_processor PROCTHROTTLEMIN 100
powercfg /setactive scheme_current
powershell.exe -NoProfile -ExecutionPolicy Bypass -File X:\autopilot\Invoke-CloudOSDBridge.ps1
