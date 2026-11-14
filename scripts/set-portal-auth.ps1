<#
.SYNOPSIS  Optional application login in front of the portal (Caddy basic_auth over HTTPS).
           Use only if Celstream policy requires more than VPN-only access and SSO is not
           available. The password is prompted securely and stored only as a bcrypt hash in
           proxy\auth\active.caddy (git-ignored). -Disable reverts to VPN-only.
#>
param([string]$User = 'celstream-bd', [switch]$Disable)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$file = 'proxy\auth\active.caddy'
if ($Disable) {
    Set-Content -Encoding ascii $file '# No application-level authentication: VPN-only access.'
} else {
    $secure = Read-Host "Password for $User" -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $hash = ($plain | docker run --rm -i caddy:2.10.2-alpine caddy hash-password) | Select-Object -Last 1
    $plain = $null
    if (-not $hash -or $hash -notmatch '^\$2') { throw 'hashing failed' }
    Set-Content -Encoding ascii $file @('basic_auth {', "    $User $hash", '}')
}
docker compose restart bd-live-proxy
Write-Host "Portal authentication: $(if ($Disable) {'disabled (VPN-only)'} else {"enabled for $User"})"
