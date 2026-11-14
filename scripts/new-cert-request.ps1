<#
.SYNOPSIS
  Corporate certificate helper for https://bd-poc.celstream-in.com.

.DESCRIPTION
  -Request   Generates a 2048-bit RSA private key (certs\portal.key, stays on this machine)
             and a CSR (certs\portal.csr) with SAN = the FQDN. Send ONLY the .csr to IT.
  -Install   Takes the certificate IT returns (PEM/.cer/.crt, base64) plus optional
             intermediate/root chain files and writes certs\portal.crt (leaf + chain).
  -FromPfx   Alternative when IT returns a .pfx: extracts certs\portal.crt + portal.key
             (prompts for the PFX password; nothing is stored except the PEM files).
  Then run scripts\deploy.ps1, which validates the certificate before serving it.
  Uses the OpenSSL that ships with Git for Windows, or a throwaway alpine/openssl container.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\new-cert-request.ps1 -Request
  powershell -ExecutionPolicy Bypass -File scripts\new-cert-request.ps1 -Install -Certificate C:\Temp\bd-poc.cer -Chain C:\Temp\issuing-ca.cer
  powershell -ExecutionPolicy Bypass -File scripts\new-cert-request.ps1 -FromPfx C:\Temp\bd-poc.pfx
#>
[CmdletBinding(DefaultParameterSetName = 'Request')]
param(
    [Parameter(ParameterSetName = 'Request')][switch]$Request,
    [Parameter(ParameterSetName = 'Install', Mandatory)][switch]$Install,
    [Parameter(ParameterSetName = 'Install', Mandatory)][string]$Certificate,
    [Parameter(ParameterSetName = 'Install')][string[]]$Chain = @(),
    [Parameter(ParameterSetName = 'Pfx', Mandatory)][string]$FromPfx,
    [string]$Fqdn = 'bd-poc.celstream-in.com',
    [string]$Organization = 'Celstream Technologies Private Limited',
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$certs = Join-Path $root 'certs'
New-Item -ItemType Directory -Force $certs | Out-Null

$gitOpenSsl = Join-Path $env:ProgramFiles 'Git\usr\bin\openssl.exe'
function OpenSsl([string[]]$OpenSslArgs, [string]$Mount) {
    if (Test-Path $gitOpenSsl) {
        Push-Location $Mount
        try { & $gitOpenSsl @OpenSslArgs } finally { Pop-Location }
    } else {
        docker run --rm -v "${Mount}:/work" -w /work alpine/openssl @OpenSslArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "openssl failed: $($OpenSslArgs[0])" }
}

function ToPem([string]$Path) {
    # Accept PEM or DER (.cer) and return PEM text.
    $bytes = [IO.File]::ReadAllBytes((Resolve-Path $Path))
    $text = [Text.Encoding]::ASCII.GetString($bytes)
    if ($text -match '-----BEGIN CERTIFICATE-----') { return ($text.Trim() + "`n") }
    $b64 = [Convert]::ToBase64String($bytes, 'InsertLineBreaks')
    return "-----BEGIN CERTIFICATE-----`n$b64`n-----END CERTIFICATE-----`n"
}

switch ($PSCmdlet.ParameterSetName) {
    'Request' {
        $key = Join-Path $certs 'portal.key'
        if ((Test-Path $key) -and -not $Force) {
            throw "certs\portal.key already exists. Use -Force only if no certificate was issued for it."
        }
        OpenSsl @('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'portal.key',
                  '-out', 'portal.csr', '-subj', "/CN=$Fqdn/O=$Organization/C=IN",
                  '-addext', "subjectAltName=DNS:$Fqdn",
                  '-addext', 'extendedKeyUsage=serverAuth') $certs
        Write-Host "Created certs\portal.key (keep on this machine) and certs\portal.csr" -ForegroundColor Green
        Write-Host "Send certs\portal.csr to Celstream IT: 'Web Server' template, SAN DNS:$Fqdn, PEM + chain."
        OpenSsl @('req', '-in', 'portal.csr', '-noout', '-subject') $certs
    }
    'Install' {
        if (-not (Test-Path (Join-Path $certs 'portal.key'))) { throw 'certs\portal.key not found (generate with -Request first).' }
        $pem = ToPem $Certificate
        foreach ($c in $Chain) { $pem += ToPem $c }
        Set-Content -Encoding ascii -NoNewline (Join-Path $certs 'portal.crt') $pem
        Write-Host 'Wrote certs\portal.crt (leaf + chain). Run scripts\deploy.ps1 to validate and apply.' -ForegroundColor Green
    }
    'Pfx' {
        $secure = Read-Host 'PFX password' -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        $stage = Join-Path $certs 'import.pfx'
        Copy-Item $FromPfx $stage -Force
        $env:BD_PFX_PASS = $plain
        try {
            OpenSsl @('pkcs12', '-in', 'import.pfx', '-nokeys', '-out', 'portal.crt', '-passin', 'env:BD_PFX_PASS') $certs
            OpenSsl @('pkcs12', '-in', 'import.pfx', '-nocerts', '-nodes', '-out', 'portal.key', '-passin', 'env:BD_PFX_PASS') $certs
        } finally {
            Remove-Item Env:\BD_PFX_PASS -ErrorAction SilentlyContinue
            $plain = $null
            Remove-Item $stage -Force -ErrorAction SilentlyContinue
        }
        Write-Host 'Extracted certs\portal.crt and certs\portal.key. Run scripts\deploy.ps1 to validate and apply.' -ForegroundColor Green
    }
}
