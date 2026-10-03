param(
    [string]$AegisRoot = "D:\Aegis",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$authRoot = Join-Path $AegisRoot "cloudflare-auth"
$keyPath = Join-Path $authRoot "secondary-news-relay-key.txt"
New-Item -ItemType Directory -Path $authRoot -Force | Out-Null
if ((Test-Path -LiteralPath $keyPath) -and -not $Force) {
    Write-Output "Chiave gia presente: $keyPath"
    exit 0
}
$bytes = [byte[]]::new(32)
$generator = [Security.Cryptography.RandomNumberGenerator]::Create()
$generator.GetBytes($bytes)
$generator.Dispose()
$key = ([BitConverter]::ToString($bytes) -replace '-', '').ToLowerInvariant()
[IO.File]::WriteAllText($keyPath, $key + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$acl = Get-Acl -LiteralPath $keyPath
$acl.SetAccessRuleProtection($true, $false)
$acl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow'))
Set-Acl -LiteralPath $keyPath -AclObject $acl
$key = $null
$bytes = $null
Write-Output "Chiave relay generata localmente: $keyPath"
Write-Output "Copia questo file sul secondo PC come C:\AEGIS-Secondary\relay-key.txt. Non incollarne il contenuto nella chat."
