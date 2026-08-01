function ConvertTo-AutopilotAgentMsiVersion {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Version
    )

    if ($Version -match '^(?<major>\d{1,3})\.(?<minor>\d{1,3})\.(?<build>\d{1,5})$') {
        $major = [int]$Matches.major
        $minor = [int]$Matches.minor
        $build = [int]$Matches.build
        if ($major -lt 256 -and $minor -lt 256 -and $build -lt 65536) {
            return $Version
        }
    }

    if ($Version -notmatch '^(?<year>20\d{2})\.(?<month>\d{1,2})\.(?<sequence>\d{1,5})$') {
        throw "Autopilot Agent version '$Version' is neither a supported legacy MSI version nor CalVer YYYY.M.SEQ."
    }

    $year = [int]$Matches.year
    $month = [int]$Matches.month
    $sequence = [int]$Matches.sequence
    if ($year -lt 2000 -or $year -gt 2055) {
        throw "CalVer year '$year' must be between 2000 through 2055 for the Windows Installer mapping."
    }
    if ($month -lt 1 -or $month -gt 12) {
        throw "CalVer month '$month' must be between 1 and 12."
    }
    if ($sequence -gt 65535) {
        throw "CalVer sequence '$sequence' must be between 0 and 65535."
    }

    return "$(200 + ($year - 2000)).$month.$sequence"
}

Export-ModuleMember -Function ConvertTo-AutopilotAgentMsiVersion
