<#
.SYNOPSIS  Restore the live database from a backup file in .\backups.
           Stops the worker, takes a safety backup, restores, restarts.
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\restore.ps1 -File backups\bd_live-manual-20261114T080000Z.db
#>
param([Parameter(Mandatory)][string]$File)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path $File)) { throw "Not found: $File" }
$name = Split-Path $File -Leaf
docker compose stop bd-live-worker bd-live-portal
docker compose run --rm --no-deps -v "${PWD}\backups:/restore:ro" bd-live-worker `
    python -m bdlive.dbtools restore "/restore/$name" --force
if ($LASTEXITCODE -ne 0) { throw 'restore failed (live database unchanged or safety backup taken)' }
docker compose up -d
docker compose exec -T bd-live-worker python -m bdlive.dbtools status
