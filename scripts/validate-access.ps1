<#
.SYNOPSIS
  Validate access to the Celstream BD portal from THIS machine (hosting workstation, an office
  PC or a VPN-connected PC). Standalone: needs only Windows PowerShell 5.1; no Docker or repo.

.DESCRIPTION
  Checks, in order: DNS resolution, TCP 443, TLS certificate trusted by this machine (no
  bypass), portal health, worker heartbeat and current data (status.json), portal page.
  Writes a JSON evidence file next to the script (or -OutDir). Exit code 0 = all passed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File validate-access.ps1 -Location Office -ExpectedIp 10.255.110.250
  powershell -ExecutionPolicy Bypass -File validate-access.ps1 -Location VPN
#>
param(
    [string]$Fqdn = 'bd-poc.celstream-in.com',
    [ValidateSet('Host', 'Office', 'VPN', 'Other')][string]$Location = 'Other',
    [string]$ExpectedIp = '',
    [int]$Port = 443,
    [string]$OutDir = $PSScriptRoot
)
$ErrorActionPreference = 'Continue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor 12288 } catch {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 }

$results = New-Object System.Collections.Generic.List[object]
function Record($check, $ok, $detail) {
    $results.Add([pscustomobject]@{ check = $check; result = $(if ($ok) { 'PASS' } else { 'FAIL' }); detail = $detail })
    $color = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("[{0}] {1,-22} {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $check, $detail) -ForegroundColor $color
}
$base = "https://${Fqdn}$(if ($Port -ne 443) {":$Port"})"
$vpnUp = [bool](Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object {
    $_.Status -eq 'Up' -and $_.InterfaceDescription -match 'Fortinet|PANGP|GlobalProtect|AnyConnect' })
Write-Host "Validating $base from $env:COMPUTERNAME (location: $Location, VPN adapter up: $vpnUp)`n"

# 1. DNS
$ip = $null
try {
    $answers = Resolve-DnsName $Fqdn -Type A -ErrorAction Stop | Where-Object { $_.Type -eq 'A' }
    $ip = ($answers | Select-Object -First 1).IPAddress
    $ok = [bool]$ip -and (-not $ExpectedIp -or $ip -eq $ExpectedIp)
    Record 'DNS resolution' $ok ("$Fqdn -> $(($answers.IPAddress) -join ', ')" + $(if ($ExpectedIp) { " (expected $ExpectedIp)" }))
} catch { Record 'DNS resolution' $false $_.Exception.Message }

# 2. TCP
if ($ip) {
    $tcp = Test-NetConnection -ComputerName $ip -Port $Port -WarningAction SilentlyContinue
    Record 'TCP connectivity' $tcp.TcpTestSucceeded "$ip`:$Port via $($tcp.InterfaceAlias) (source $($tcp.SourceAddress.IPAddress))"
} else { Record 'TCP connectivity' $false 'skipped: no DNS answer' }

# 3. TLS certificate trusted by this machine's store (no bypass)
$tlsOk = $false
if ($ip) {
    try {
        $client = New-Object Net.Sockets.TcpClient($ip, $Port)
        $script:policy = $null
        $ssl = New-Object Net.Security.SslStream($client.GetStream(), $false,
            [Net.Security.RemoteCertificateValidationCallback] { param($s, $c, $ch, $e) $script:policy = $e; $true })
        $ssl.AuthenticateAsClient($Fqdn)
        $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
        $tlsOk = ($script:policy -eq [Net.Security.SslPolicyErrors]::None)
        Record 'Trusted certificate' $tlsOk ("issuer '$($cert.Issuer)', expires $($cert.NotAfter.ToString('yyyy-MM-dd')), " +
            "$($ssl.SslProtocol), policy errors: $($script:policy)")
        $ssl.Dispose(); $client.Dispose()
    } catch { Record 'Trusted certificate' $false $_.Exception.Message }
} else { Record 'Trusted certificate' $false 'skipped: no DNS answer' }

# 4. Portal health (normal certificate validation)
try {
    $r = Invoke-WebRequest "$base/_stcore/health" -UseBasicParsing -TimeoutSec 15
    Record 'Portal health' ($r.StatusCode -eq 200) "HTTP $($r.StatusCode) '$($r.Content.Trim())'"
} catch { Record 'Portal health' $false $_.Exception.Message }

# 5. Worker heartbeat + current data
try {
    $st = Invoke-RestMethod "$base/status.json" -TimeoutSec 15
    $age = ((Get-Date).ToUniversalTime() - [datetime]::Parse($st.generated_at).ToUniversalTime()).TotalSeconds
    Record 'Worker heartbeat' ($st.worker.online -and $age -lt 90) ("worker '$($st.worker.id)' $($st.worker.state), " +
        "online=$($st.worker.online), status age $([int]$age)s")
    $c = $st.counts
    Record 'Current data' ($c.jobs -gt 0) ("jobs $($c.jobs), analysed $($c.jobs_analysed), companies $($c.companies), " +
        "contacts $($c.contacts), high $($c.high_priority), medium $($c.medium_priority)")
    $run = if ($st.active_run) { "run #$($st.active_run.id) $($st.active_run.status): $($st.active_run.stage)" }
           else { "idle; last run #$($st.last_run.id) $($st.last_run.status) at $($st.last_run.finished_at)" }
    Record 'Execution monitoring' $true $run
} catch { Record 'Worker heartbeat' $false $_.Exception.Message }

# 6. Portal page
try {
    $p = Invoke-WebRequest "$base/" -UseBasicParsing -TimeoutSec 15
    Record 'Portal page' ($p.StatusCode -eq 200 -and $p.Content -match 'streamlit') "HTTP $($p.StatusCode), $($p.Content.Length) bytes"
} catch { Record 'Portal page' $false $_.Exception.Message }

$failed = @($results | Where-Object result -eq 'FAIL').Count
$evidence = [pscustomobject]@{
    url = $base; client = $env:COMPUTERNAME; user = $env:USERNAME; location = $Location; vpn_adapter_up = $vpnUp
    checked_at = (Get-Date).ToUniversalTime().ToString('o'); passed = ($failed -eq 0); results = $results
}
$file = Join-Path $OutDir ("validate-access-{0}-{1}-{2}.json" -f $Location, $env:COMPUTERNAME, (Get-Date -Format 'yyyyMMdd-HHmmss'))
$evidence | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 $file
Write-Host "`n$(if ($failed) {"$failed check(s) FAILED"} else {'ALL CHECKS PASSED'}). Evidence: $file" -ForegroundColor $(if ($failed) {'Red'} else {'Green'})
Write-Host 'Manual check: open the URL in a browser - no certificate warning, banner shows "Worker online".'
exit $(if ($failed) { 1 } else { 0 })
