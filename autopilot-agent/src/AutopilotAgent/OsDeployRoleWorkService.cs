using System.Diagnostics;
using System.Text.Json;

namespace AutopilotAgent;

public sealed class OsDeployRoleWorkService(AgentFileLog log)
{
    public async Task<Dictionary<string, object?>> ProcessAsync(
        string kind,
        IReadOnlyDictionary<string, JsonElement> parameters,
        CancellationToken cancellationToken)
    {
        var script = kind switch
        {
            "join_domain_role" => BuildJoinDomainScript(parameters),
            "configure_file_server_role" => BuildFileServerScript(parameters),
            "configure_isolated_domain_controller_role" => BuildIsolatedDomainControllerScript(parameters),
            "verify_isolated_domain_controller_role" => BuildIsolatedDomainControllerVerifyScript(parameters),
            "configure_mecm_prereq_role" => BuildMecmPrereqScript(parameters),
            _ => throw new InvalidOperationException($"Unsupported OSDeploy role step kind: {kind}"),
        };
        var output = await RunPowerShellAsync(script, TimeSpan.FromHours(2), cancellationToken);
        return new Dictionary<string, object?>
        {
            ["kind"] = kind,
            ["stdout"] = output.Stdout,
            ["stderr"] = output.Stderr,
        };
    }

    public static string BuildJoinDomainScript(IReadOnlyDictionary<string, JsonElement> parameters)
    {
        var domainFqdn = RequiredString(parameters, "domain_fqdn");
        var username = RequiredString(parameters, "domain_join_username");
        var password = RequiredString(parameters, "domain_join_password");
        var credentialDomain = ReadOptionalString(parameters, "credential_domain") ?? "";
        var domainControllerIpv4 = ReadOptionalString(parameters, "domain_controller_ipv4") ?? "";
        return $$"""
$ErrorActionPreference = 'Stop'
$domainFqdn = {{PsString(domainFqdn)}}
$username = {{PsString(username)}}
$password = {{PsString(password)}}
$credentialDomain = {{PsString(credentialDomain)}}
$domainControllerIpv4 = {{PsString(domainControllerIpv4)}}
if ([string]::IsNullOrWhiteSpace($domainFqdn) -or [string]::IsNullOrWhiteSpace($username) -or [string]::IsNullOrWhiteSpace($password)) {
  throw 'domain join role step requires domain_fqdn, domain_join_username, and domain_join_password.'
}
$securePassword = ConvertTo-SecureString $password -AsPlainText -Force
$credential = [System.Management.Automation.PSCredential]::new($username, $securePassword)
$current = Get-CimInstance Win32_ComputerSystem
if ($current.PartOfDomain -and $current.Domain -ieq $domainFqdn) {
  @{ role = 'domain_join'; domain_fqdn = $domainFqdn; already_joined = $true; reboot_required = $false } | ConvertTo-Json -Compress
  exit 0
}
if ($current.PartOfDomain -and $current.Domain -ine $domainFqdn) {
  throw "Computer is already joined to domain '$($current.Domain)' and will not be moved by OSDeploy lab automation."
}
if (-not [string]::IsNullOrWhiteSpace($domainControllerIpv4)) {
  $upAdapters = Get-NetAdapter | Where-Object { $_.Status -eq 'Up' }
  foreach ($adapter in $upAdapters) {
    Set-DnsClientServerAddress -InterfaceIndex $adapter.InterfaceIndex -ServerAddresses $domainControllerIpv4 -ErrorAction Stop
  }
  Clear-DnsClientCache
}
Add-Computer -DomainName $domainFqdn -Credential $credential -Force -ErrorAction Stop
@{ role = 'domain_join'; domain_fqdn = $domainFqdn; credential_domain = $credentialDomain; dns_server = $domainControllerIpv4; reboot_required = $true } | ConvertTo-Json -Compress
""";
    }

    public static string BuildFileServerScript(IReadOnlyDictionary<string, JsonElement> parameters)
    {
        var shareName = RequiredString(parameters, "share_name");
        var sharePath = RequiredString(parameters, "share_path");
        var full = ReadStringArray(parameters, "full_access_principals");
        var change = ReadStringArray(parameters, "change_access_principals");
        var read = ReadStringArray(parameters, "read_access_principals");
        if (full.Length == 0)
        {
            throw new InvalidOperationException("full_access_principals is required.");
        }
        return $$"""
$ErrorActionPreference = 'Stop'
Install-WindowsFeature -Name FS-FileServer -IncludeManagementTools | Out-Null
$shareName = {{PsString(shareName)}}
$sharePath = {{PsString(sharePath)}}
$full = @({{PsArray(full)}})
$change = @({{PsArray(change)}})
$read = @({{PsArray(read)}})
New-Item -ItemType Directory -Force -Path $sharePath | Out-Null
foreach ($principal in ($full + $change + $read | Where-Object { $_ })) {
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($principal, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
  $acl = Get-Acl -Path $sharePath
  $acl.SetAccessRule($rule)
  Set-Acl -Path $sharePath -AclObject $acl
}
$existing = Get-SmbShare -Name $shareName -ErrorAction SilentlyContinue
if ($existing) {
  Set-SmbShare -Name $shareName -FolderEnumerationMode AccessBased -Force | Out-Null
} else {
  $shareArgs = @{
    Name = $shareName
    Path = $sharePath
    FolderEnumerationMode = 'AccessBased'
  }
  if ($full.Count -gt 0) { $shareArgs.FullAccess = $full }
  if ($change.Count -gt 0) { $shareArgs.ChangeAccess = $change }
  if ($read.Count -gt 0) { $shareArgs.ReadAccess = $read }
  New-SmbShare @shareArgs | Out-Null
}
@{ role = 'file_server'; share_name = $shareName; share_path = $sharePath } | ConvertTo-Json -Compress
""";
    }

    public static string BuildIsolatedDomainControllerScript(IReadOnlyDictionary<string, JsonElement> parameters)
    {
        var forestFqdn = RequiredString(parameters, "forest_fqdn");
        var netbiosName = RequiredString(parameters, "netbios_name");
        var forestAdminUsername = ReadOptionalString(parameters, "forest_admin_username") ?? "Administrator";
        var forestAdminPassword = ReadOptionalString(parameters, "forest_admin_password") ?? "";
        var dsrmPassword = ReadOptionalString(parameters, "dsrm_password") ?? "";
        return $$"""
$ErrorActionPreference = 'Stop'
Install-WindowsFeature -Name AD-Domain-Services,DNS -IncludeManagementTools | Out-Null
$forestFqdn = {{PsString(forestFqdn)}}
$netbiosName = {{PsString(netbiosName)}}
$forestAdminUsername = {{PsString(forestAdminUsername)}}
$forestAdminPassword = {{PsString(forestAdminPassword)}}
$dsrmPassword = {{PsString(dsrmPassword)}}
if ([string]::IsNullOrWhiteSpace($forestAdminPassword)) {
  throw 'forest_admin_password was not provided to the isolated domain controller role step.'
}
if ([string]::IsNullOrWhiteSpace($dsrmPassword)) {
  throw 'dsrm_password was not provided to the isolated domain controller role step.'
}
$localAdministratorName = $forestAdminUsername
if ($localAdministratorName.Contains('\')) {
  $localAdministratorName = $localAdministratorName.Split('\')[-1]
}
if ($localAdministratorName -ine 'Administrator') {
  throw 'isolated domain controller forest_admin_credential_id must reference the local Administrator account for new forest promotion.'
}
$localAdministrator = [ADSI]'WinNT://./Administrator,user'
$localAdministrator.SetPassword($forestAdminPassword)
$localAdministrator.SetInfo()
& net.exe user Administrator /active:yes | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "Failed to enable local Administrator account. net.exe exit code: $LASTEXITCODE"
}
$secureDsrm = ConvertTo-SecureString $dsrmPassword -AsPlainText -Force
Install-ADDSForest -DomainName $forestFqdn -DomainNetbiosName $netbiosName -SafeModeAdministratorPassword $secureDsrm -InstallDns -Force -NoRebootOnCompletion
@{ role = 'isolated_domain_controller'; forest_fqdn = $forestFqdn; netbios_name = $netbiosName; reboot_required = $true } | ConvertTo-Json -Compress
""";
    }

    public static string BuildIsolatedDomainControllerVerifyScript(IReadOnlyDictionary<string, JsonElement> parameters)
    {
        var forestFqdn = RequiredString(parameters, "forest_fqdn");
        return $$"""
$ErrorActionPreference = 'Stop'
$forestFqdn = {{PsString(forestFqdn)}}
$domain = Get-ADDomain -Identity $forestFqdn
$sysvol = Test-Path "$env:SystemRoot\SYSVOL\sysvol"
$netlogon = Get-SmbShare -Name NETLOGON -ErrorAction SilentlyContinue
if (-not $domain -or -not $sysvol -or -not $netlogon) {
  throw 'Isolated domain controller verification failed.'
}
@{ role = 'isolated_domain_controller'; forest_fqdn = $forestFqdn; sysvol = $sysvol; netlogon = $true } | ConvertTo-Json -Compress
""";
    }

    public static string BuildMecmPrereqScript(IReadOnlyDictionary<string, JsonElement> parameters)
    {
        var profile = RequiredString(parameters, "prereq_profile");
        var contentRoot = RequiredString(parameters, "content_root");
        if (!string.Equals(profile, "site_server_foundation", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException($"Unsupported MECM prereq profile: {profile}");
        }
        return $$"""
$ErrorActionPreference = 'Stop'
$contentRoot = {{PsString(contentRoot)}}
New-Item -ItemType Directory -Force -Path $contentRoot | Out-Null
$features = @(
  'Web-Server',
  'Web-Windows-Auth',
  'Web-Asp-Net45',
  'Web-Metabase',
  'Web-WMI',
  'BITS',
  'RDC'
)
Install-WindowsFeature -Name $features -IncludeManagementTools | Out-Null
@{ role = 'mecm_prereq'; prereq_profile = 'site_server_foundation'; content_root = $contentRoot; features = $features } | ConvertTo-Json -Compress
""";
    }

    private async Task<ProcessOutput> RunPowerShellAsync(
        string script,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        var scriptPath = Path.Combine(
            Path.GetTempPath(),
            $"autopilot-osdeploy-role-{Guid.NewGuid():N}.ps1");
        await File.WriteAllTextAsync(scriptPath, script, cancellationToken);
        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            startInfo.ArgumentList.Add("-NoProfile");
            startInfo.ArgumentList.Add("-ExecutionPolicy");
            startInfo.ArgumentList.Add("Bypass");
            startInfo.ArgumentList.Add("-File");
            startInfo.ArgumentList.Add(scriptPath);
            using var process = Process.Start(startInfo)
                ?? throw new InvalidOperationException("Failed to start powershell.exe.");
            using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            timeoutCts.CancelAfter(timeout);
            var stdoutTask = process.StandardOutput.ReadToEndAsync(timeoutCts.Token);
            var stderrTask = process.StandardError.ReadToEndAsync(timeoutCts.Token);
            await process.WaitForExitAsync(timeoutCts.Token);
            var output = new ProcessOutput(
                await stdoutTask,
                await stderrTask,
                process.ExitCode);
            if (output.ExitCode != 0)
            {
                throw new InvalidOperationException(
                    $"PowerShell failed with exit {output.ExitCode}: {output.Stderr} {output.Stdout}".Trim());
            }
            log.Info($"OSDeploy role PowerShell step completed: {Path.GetFileName(scriptPath)}");
            return output;
        }
        finally
        {
            try
            {
                File.Delete(scriptPath);
            }
            catch
            {
                // Best effort cleanup.
            }
        }
    }

    private static string RequiredString(IReadOnlyDictionary<string, JsonElement> parameters, string name)
    {
        var value = ReadOptionalString(parameters, name);
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new InvalidOperationException($"{name} is required.");
        }
        return value;
    }

    private static string? ReadOptionalString(IReadOnlyDictionary<string, JsonElement> parameters, string name)
    {
        return parameters.TryGetValue(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;
    }

    private static string[] ReadStringArray(IReadOnlyDictionary<string, JsonElement> parameters, string name)
    {
        if (!parameters.TryGetValue(name, out var value) || value.ValueKind != JsonValueKind.Array)
        {
            return [];
        }
        return value.EnumerateArray()
            .Where(item => item.ValueKind == JsonValueKind.String)
            .Select(item => item.GetString())
            .Where(item => !string.IsNullOrWhiteSpace(item))
            .Select(item => item!)
            .ToArray();
    }

    private static string PsArray(IEnumerable<string> values) =>
        string.Join(", ", values.Select(PsString));

    private static string PsString(string value) =>
        $"'{value.Replace("'", "''", StringComparison.Ordinal)}'";

    private sealed record ProcessOutput(string Stdout, string Stderr, int ExitCode);
}
