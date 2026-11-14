<#
.SYNOPSIS  Report whether the stack will come back after a workstation restart.
           Containers use restart: unless-stopped, so they start when Docker Desktop starts;
           Docker Desktop starts only after a user signs in, if "Start Docker Desktop when you
           sign in" is enabled (Settings > General).
#>
$settings = @("$env:APPDATA\Docker\settings-store.json", "$env:APPDATA\Docker\settings.json") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if ($settings) {
    $s = Get-Content $settings -Raw | ConvertFrom-Json
    $auto = if ($null -ne $s.AutoStart) { $s.AutoStart } else { $s.autoStart }
    Write-Host "Docker Desktop AutoStart at sign-in: $auto  ($settings)"
} else { Write-Host 'Docker Desktop settings file not found (is Docker Desktop installed?)' }
Set-Location (Split-Path -Parent $PSScriptRoot)
docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Health}}'
docker inspect -f '{{.Name}} restart={{.HostConfig.RestartPolicy.Name}}' bd-live-worker bd-live-portal bd-live-proxy
