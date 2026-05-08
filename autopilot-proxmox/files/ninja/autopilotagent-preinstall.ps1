[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ServerUrl,

    [string]$LogRoot = "$env:ProgramData\ProxmoxVEAutopilot\AutopilotAgent\install"
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogPath = Join-Path $LogRoot "preinstall.log"

function Write-InstallLog {
    param([string]$Message)
    $line = "{0:o} {1}" -f (Get-Date), $Message
    Add-Content -Path $LogPath -Value $line
}

Write-InstallLog "Starting AutopilotAgent preinstall."

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "AutopilotAgent install requires administrative context."
}

$os = Get-CimInstance Win32_OperatingSystem
Write-InstallLog "OSArchitecture=$($os.OSArchitecture) Version=$($os.Version)"
if ($os.ProductType -ne 1) {
    Write-InstallLog "Warning: ProductType=$($os.ProductType); expected workstation."
}
if ($os.OSArchitecture -notmatch "64") {
    throw "AutopilotAgent requires a 64-bit Windows OS."
}

$service = Get-Service -Name AutopilotAgent -ErrorAction SilentlyContinue
if ($service) {
    Write-InstallLog "Existing AutopilotAgent service state=$($service.Status)."
} else {
    Write-InstallLog "AutopilotAgent service is not installed."
}

$healthUrl = ($ServerUrl.TrimEnd("/") + "/healthz")
Write-InstallLog "Checking reachability: $healthUrl"
$response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 15
Write-InstallLog "Reachability status=$($response.StatusCode)."

Write-InstallLog "AutopilotAgent preinstall complete."
