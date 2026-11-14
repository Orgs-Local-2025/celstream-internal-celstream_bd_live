<#
.SYNOPSIS  Online backup of the live SQLite database to the bd-live-backups volume and to
           .\backups on the workstation (safe while the portal/worker are running).
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 [-Label before-upgrade]
#>
param([string]$Label = 'manual')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$out = docker compose exec -T bd-live-worker python -m bdlive.dbtools backup $Label | Out-String
if ($LASTEXITCODE -ne 0) { throw "backup failed: $out" }
Write-Host $out
$path = ($out | ConvertFrom-Json).backup
New-Item -ItemType Directory -Force backups | Out-Null
docker compose cp "bd-live-worker:$path" "backups\$(Split-Path $path -Leaf)"
if ($LASTEXITCODE -ne 0) { throw 'copy to host failed' }
Write-Host "Host copy: backups\$(Split-Path $path -Leaf)" -ForegroundColor Green
