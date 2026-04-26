function Get-PeIdentity {
    [CmdletBinding()]
    [OutputType([pscustomobject])]
    param()

    $product = Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop
    return [pscustomobject]@{
        Uuid   = $product.UUID
        Vendor = $product.Vendor
        Name   = $product.Name
    }
}
