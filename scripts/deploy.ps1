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
$fqdn = (Get-Content .env | Where-Object { $_ -match '^\s*PORTAL_FQDN\s*=' } |
    Select-Object -Last 1) -replace '^\s*PORTAL_FQDN\s*=\s*', ''
if (-not $fqdn) { $fqdn = 'bd-poc.localhost'; Write-Host 'PORTAL_FQDN not set; using bd-poc.localhost (local only).' }
else { Write-Host "PORTAL_FQDN = $fqdn" }

$authFile = Join-Path $root 'proxy\auth\active.caddy'
if (-not (Test-Path $authFile)) {
    # Git-ignored (may hold a basic_auth hash); default to VPN-only access.
    New-Item -ItemType Directory -Force (Split-Path $authFile) | Out-Null
    Set-Content -Encoding ascii $authFile '# No application-level authentication: VPN-only access.'
}

Step 'Selecting TLS mode'
$tlsFile = Join-Path $root 'proxy\tls\active.caddy'
if ((Test-Path certs\portal.crt) -and (Test-Path certs\portal.key)) {
    Set-Content -Encoding ascii $tlsFile @(
        '# Corporate CA certificate (shared use).',
        'tls /certs/portal.crt /certs/portal.key')
    Write-Host 'Using corporate certificate certs\portal.crt' -ForegroundColor Green
} else {
    Set-Content -Encoding ascii $tlsFile @(
        '# Interim TLS: Caddy local CA. NOT trusted by other machines - local validation only.',
        'tls internal')
    Write-Host 'No corporate certificate in certs\ -> interim Caddy internal CA (local validation only).' -ForegroundColor Yellow
}

if (-not $SkipBuild) {
    Step 'Building images'
    docker compose build
    if ($LASTEXITCODE -ne 0) { Fail 'docker compose build failed.' }
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

Step 'Local HTTPS check'
$code = curl.exe -s -o NUL -w '%{http_code}' -k --resolve "${fqdn}:443:127.0.0.1" "https://${fqdn}/_stcore/health"
Write-Host "https://$fqdn/_stcore/health via 127.0.0.1 -> HTTP $code"
docker compose exec -T bd-live-worker python -m bdlive.dbtools status

Write-Host "`nDeployment complete. Local: https://localhost  (FQDN: https://$fqdn once DNS exists)" -ForegroundColor Green
