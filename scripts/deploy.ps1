<#
.SYNOPSIS
  Build and start the Celstream BD Live PoC on Docker Desktop (Windows 11).

.DESCRIPTION
  1. Checks Docker Desktop / Compose and the presence (not values) of required .env keys.
  2. Selects TLS mode: corporate certificate if certs\portal.crt + certs\portal.key exist,
     otherwise Caddy's internal CA (local validation only - not trusted by other machines).
  3. Seeds the bd-live-data volume from data\bd_live.db on first deployment (SQLite backup
     API, integrity-checked; never overwrites a populated volume).
  4. docker compose build + up -d, then waits for all health checks.
  Safe to re-run. Never prints secret values.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$NoSeed
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

Step 'Checking Docker Desktop'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail 'docker CLI not found. Install Docker Desktop (WSL 2 backend) and start it first.'
}
docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Engine is not running. Start Docker Desktop and retry.' }
docker compose version
if ($LASTEXITCODE -ne 0) { Fail 'docker compose v2 is required.' }

Step 'Checking .env (key names only)'
if (-not (Test-Path .env)) { Fail '.env not found in the project root.' }
$envKeys = Get-Content .env | Where-Object { $_ -match '^\s*([A-Z0-9_]+)\s*=\s*\S' } |
    ForEach-Object { ($_ -split '=', 2)[0].Trim() }
$required = 'THEIRSTACK_API_KEY', 'OPENAI_API_KEY', 'OPENAI_MODEL', 'APOLLO_API_KEY', 'HUBSPOT_ACCESS_TOKEN'
$missing = $required | Where-Object { $_ -notin $envKeys }
if ($missing) { Fail "Missing .env keys: $($missing -join ', ')" }
Write-Host "Required keys present: $($required -join ', ')"
function EnvValue($name, $default) {
    $line = Get-Content .env | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -Last 1
    $value = if ($line) { ($line -split '=', 2)[1].Trim() } else { '' }
    if ($value) { $value } else { $default }
}
$fqdn = EnvValue 'PORTAL_FQDN' 'bd-poc.localhost'
$bind = EnvValue 'HTTPS_BIND' '127.0.0.1'
$port = EnvValue 'HTTPS_PORT' '443'
$shared = $bind -notin @('127.0.0.1', 'localhost')
Write-Host "PORTAL_FQDN = $fqdn  HTTPS_BIND = ${bind}:$port  ($(if ($shared) {'SHARED: office/VPN access'} else {'local only'}))"

if ($shared) {
    Step "Checking that $bind is an active address on this workstation"
    $addr = Get-NetIPAddress -IPAddress $bind -ErrorAction SilentlyContinue
    if (-not $addr -or $addr.AddressState -ne 'Preferred') {
        Fail ("$bind is not an active IPv4 address here (state: $(if ($addr) {$addr.AddressState} else {'absent'})). " +
              'Connect the workstation to the corporate LAN (wired adapter) or set HTTPS_BIND=127.0.0.1 for local-only use.')
    }
    Write-Host "OK: $bind on '$($addr.InterfaceAlias)'"
}

$authFile = Join-Path $root 'proxy\auth\active.caddy'
if (-not (Test-Path $authFile)) {
    # Git-ignored (may hold a basic_auth hash); default to network-restricted access only.
    New-Item -ItemType Directory -Force (Split-Path $authFile) | Out-Null
    Set-Content -Encoding ascii $authFile '# No application-level authentication: LAN/VPN-restricted access.'
}

if (-not $SkipBuild) {
    Step 'Building images'
    docker compose build
    if ($LASTEXITCODE -ne 0) { Fail 'docker compose build failed.' }
}

Step 'Selecting TLS mode'
$tlsFile = Join-Path $root 'proxy\tls\active.caddy'
$sitesFile = Join-Path $root 'proxy\tls\extra-sites.caddy'
if ((Test-Path certs\portal.crt) -and (Test-Path certs\portal.key)) {
    docker compose run --rm --no-deps -v "${root}\certs:/certs:ro" bd-live-worker `
        python -m bdlive.certcheck /certs/portal.crt /certs/portal.key $fqdn
    if ($LASTEXITCODE -ne 0) { Fail "certs\portal.crt / portal.key failed validation for $fqdn (see above)." }
    Set-Content -Encoding ascii $tlsFile @(
        '# Corporate CA certificate (shared access). Written by scripts/deploy.ps1.',
        'tls /certs/portal.crt /certs/portal.key')
    # The interim localhost site (Caddy local CA) is removed so it can never be served.
    Set-Content -Encoding ascii $sitesFile '# Corporate mode: no additional sites.'
    Write-Host 'Using the validated corporate certificate certs\portal.crt' -ForegroundColor Green
} else {
    if ($shared) {
        Fail ("No corporate certificate in certs\ (portal.crt + portal.key). The interim local CA must not be " +
              'used for shared access. Obtain the certificate (scripts\new-cert-request.ps1) or set HTTPS_BIND=127.0.0.1.')
    }
    Set-Content -Encoding ascii $tlsFile @(
        '# Interim TLS: Caddy local CA. NOT trusted by other machines - local validation only.',
        'tls internal')
    Set-Content -Encoding ascii $sitesFile @(
        '# Interim mode: local validation site using the Caddy local CA (never shared).',
        'localhost {', "`ttls internal", "`timport portal", '}')
    Write-Host 'No corporate certificate -> interim Caddy internal CA, local access only.' -ForegroundColor Yellow
}

if (-not $NoSeed -and (Test-Path data\bd_live.db)) {
    Step 'Seeding the bd-live-data volume from data\bd_live.db (first deployment only)'
    docker compose run --rm --no-deps -v "${root}\data:/import:ro" bd-live-worker `
        python -m bdlive.dbtools import /import/bd_live.db
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Seed skipped (volume already populated, or see message above).' -ForegroundColor Yellow
    }
}

Step 'Starting containers'
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail 'docker compose up failed.' }

Step 'Waiting for health checks (up to 3 minutes)'
$deadline = (Get-Date).AddMinutes(3)
do {
    Start-Sleep -Seconds 5
    $states = docker compose ps --format '{{.Service}}={{.Health}}'
    Write-Host ($states -join '  ')
    $unhealthy = $states | Where-Object { $_ -notmatch '=healthy$' }
} while ($unhealthy -and (Get-Date) -lt $deadline)
if ($unhealthy) { Fail "Not healthy: $($unhealthy -join ', '). Check: docker compose logs" }

Step 'HTTPS check on this workstation'
$target = if ($shared) { $bind } else { '127.0.0.1' }
$code = curl.exe -s -o NUL -w '%{http_code}' -k --resolve "${fqdn}:${port}:$target" "https://${fqdn}:$port/_stcore/health"
Write-Host "https://${fqdn}:$port/_stcore/health via $target -> HTTP $code"
$status = curl.exe -s -k --resolve "${fqdn}:${port}:$target" "https://${fqdn}:$port/status.json"
Write-Host "status.json: $status"
docker compose exec -T bd-live-worker python -m bdlive.dbtools status

Write-Host "`nDeployment complete: https://$fqdn$(if ($port -ne '443') {":$port"})" -ForegroundColor Green
if ($shared) { Write-Host 'Next: run scripts\validate-access.ps1 from an office PC and a VPN PC (see docs\fqdn-it-handoff.md).' }
else { Write-Host 'Local only. Open https://localhost (interim certificate warning is expected).' }
