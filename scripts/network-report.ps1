<#
.SYNOPSIS
  Collect the network facts IT needs for the bd-poc DNS record and firewall/VPN policy from
  the HOSTING workstation. Read-only; prints no credentials. Paste the output into the handoff.
#>
$ErrorActionPreference = 'SilentlyContinue'
$cs = Get-CimInstance Win32_ComputerSystem
"Host        : $env:COMPUTERNAME  domain=$($cs.Domain)  joined=$($cs.PartOfDomain)"
"Chassis     : $((Get-CimInstance Win32_SystemEnclosure).ChassisTypes -join ',')  (8-14 = portable/laptop)"
"Collected   : $((Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm')) UTC"
"`n--- Physical adapters"
Get-NetAdapter -Physical | ForEach-Object {
    $ip = Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '169.254.*' }
    $cfg = Get-NetIPConfiguration -InterfaceIndex $_.ifIndex
    $dhcp = (Get-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4).Dhcp
    "{0,-10} {1,-12} ip={2} dhcp={3} gw={4} dns={5} profile={6} mac={7}" -f $_.Name, $_.Status,
        (($ip | ForEach-Object { "$($_.IPAddress)/$($_.PrefixLength)[$($_.AddressState)]" }) -join ','), $dhcp,
        ($cfg.IPv4DefaultGateway.NextHop -join ','), ($cfg.DNSServer | Where-Object AddressFamily -eq 2 |
        ForEach-Object { $_.ServerAddresses -join ',' }), $cfg.NetProfile.NetworkCategory, $_.MacAddress
}
"`n--- VPN adapters"
Get-NetAdapter | Where-Object { $_.InterfaceDescription -match 'Fortinet|PANGP|GlobalProtect|AnyConnect' } | ForEach-Object {
    $ip = (Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress -join ','
    $routes = (Get-NetRoute -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.NextHop -ne '0.0.0.0' } |
        ForEach-Object DestinationPrefix) -join ','
    $dns = (Get-DnsClientServerAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses -join ','
    "{0}: {1} ip={2} dns={3} routes={4}" -f $_.InterfaceDescription, $_.Status, $ip, $dns, $routes
}
"`n--- Corporate DNS reachability"
foreach ($n in '_ldap._tcp.dc._msdcs.celstream-in.com', 'bd-poc.celstream-in.com') {
    $r = Resolve-DnsName $n -ErrorAction SilentlyContinue
    "{0}: {1}" -f $n, $(if ($r) { ($r | ForEach-Object { $_.NameTarget + $_.IPAddress }) -join ',' } else { 'NOT RESOLVED' })
}
"`n--- Docker / portal binding"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $env:Path = (Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin') + ';' + $env:Path
}
Push-Location (Split-Path -Parent $PSScriptRoot)
docker compose ps --format '{{.Service}} {{.Status}} {{.Ports}}' 2>$null
Get-NetTCPConnection -State Listen -LocalPort 443 -ErrorAction SilentlyContinue | ForEach-Object { "listening 443 on $($_.LocalAddress)" }
Get-NetFirewallRule -DisplayName 'Celstream BD Live PoC*' -ErrorAction SilentlyContinue | ForEach-Object {
    $a = $_ | Get-NetFirewallAddressFilter; "firewall: $($_.DisplayName) local=$($a.LocalAddress) remote=$($a.RemoteAddress -join ',')" }
Pop-Location
