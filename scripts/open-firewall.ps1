#Requires -RunAsAdministrator
<#
.SYNOPSIS  (Windows administrator) Allow inbound HTTPS 443 to the portal from the Celstream
           VPN address range only. Run ONLY after IT confirms the VPN client subnet(s).
.EXAMPLE   powershell -ExecutionPolicy Bypass -File scripts\open-firewall.ps1 -RemoteAddress 10.20.0.0/16
#>
param([Parameter(Mandatory)][string[]]$RemoteAddress, [int]$Port = 443)
$name = 'Celstream BD Live PoC - HTTPS (VPN only)'
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $Port -RemoteAddress $RemoteAddress -Profile Domain, Private `
    -Description 'Docker Desktop published port for bd-live-proxy; Celstream VPN clients only.'
Get-NetFirewallRule -DisplayName $name | Get-NetFirewallAddressFilter
