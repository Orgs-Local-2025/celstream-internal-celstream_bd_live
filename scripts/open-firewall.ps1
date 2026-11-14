#Requires -RunAsAdministrator
<#
.SYNOPSIS
  (Windows administrator) Allow inbound HTTPS to the BD portal ONLY on the workstation's
  corporate LAN IP and ONLY from approved office LAN and VPN client subnets.

.DESCRIPTION
  Applies to all network profiles but is scoped by LocalAddress + RemoteAddress, so it still
  holds if Windows classifies the LAN as Public (e.g. when the domain is not detected).
  No other port is opened: Streamlit (8501), the worker and SQLite are never published.
  Re-running replaces the rule. -Remove deletes it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\open-firewall.ps1 -LanIp 10.255.110.250 `
      -OfficeSubnets 10.255.110.0/24,10.255.10.0/24 -VpnSubnets 192.168.10.0/24
#>
param(
    [Parameter(Mandatory, ParameterSetName = 'Add')][string]$LanIp,
    [Parameter(Mandatory, ParameterSetName = 'Add')][string[]]$OfficeSubnets,
    [Parameter(Mandatory, ParameterSetName = 'Add')][string[]]$VpnSubnets,
    [Parameter(ParameterSetName = 'Add')][int]$Port = 443,
    [Parameter(Mandatory, ParameterSetName = 'Remove')][switch]$Remove
)
$ErrorActionPreference = 'Stop'
$name = 'Celstream BD Live PoC - HTTPS (office LAN + VPN only)'
Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallRule -DisplayName 'Celstream BD Live PoC - HTTPS (VPN only)' -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule   # rule name used by an earlier version of this script
if ($Remove) { Write-Host "Removed '$name'."; return }

$remote = @($OfficeSubnets) + @($VpnSubnets)
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $Port -LocalAddress $LanIp -RemoteAddress $remote -Profile Any `
    -Description 'bd-live-proxy (Docker Desktop published port). Approved Celstream office and VPN subnets only.' |
    Out-Null
$rule = Get-NetFirewallRule -DisplayName $name
$addr = $rule | Get-NetFirewallAddressFilter
$ports = $rule | Get-NetFirewallPortFilter
"Rule      : $name"
"Local     : $($addr.LocalAddress -join ', ') TCP $($ports.LocalPort)"
"Allowed   : $($addr.RemoteAddress -join ', ')"
"Profiles  : $($rule.Profile)"
