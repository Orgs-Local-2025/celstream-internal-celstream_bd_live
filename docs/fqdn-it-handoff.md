# FQDN, certificate and access handoff — Celstream BD Live PoC

Prepared 14 Nov 2026 from an actual inspection of workstation RAGHUGN-LAP. **Not complete:**
DNS resolution and remote access over the VPN have not been verified, and cannot be until the
items below are done.

## Inspection findings

| Item | Finding |
|---|---|
| OS | Windows 11 Enterprise 10.0.26200, joined to AD domain `celstream-in.com` |
| DNS suffix search list | `celstream-in.com` |
| VPN clients installed | FortiClient VPN 7.4.3, GlobalProtect 6.3.3 |
| VPN state at inspection | **Not connected.** Both virtual adapters were disconnected or disabled, so **no VPN IPv4 address is available yet** |
| Active network | Home Wi-Fi 192.168.1.55/24 (DHCP), Windows profile **Public** |
| Other adapter | Wired Ethernet with static 10.255.110.250/24 and DNS 8.8.8.8, unplugged |
| Docker | Initially not installed. Docker Desktop (Engine 29.9.3, Compose v5.7.0, WSL 2) was installed later on 14 Nov; the stack is now deployed locally |
| Privileges | User is in local Administrators but runs non-elevated (UAC) |
| Firewall | Enabled on all profiles; no inbound rule for 443 |
| Port 443 | Free (port 80 is held by Windows `System`/http.sys) |

## Request to Celstream IT / network administrator

| Item | Requested value |
|---|---|
| FQDN | `bd-poc.celstream-in.com` (proposed: `celstream-in.com` is the AD domain seen on this machine). IT to confirm the approved internal zone and name |
| DNS record | **A record**, internal DNS only (never public) → workstation's **VPN-assigned IPv4** |
| VPN IP stability | The VPN IP is usually assigned per session. Need a **static or reserved VPN IP** for this workstation and user (FortiClient/GlobalProtect address reservation), or an alternative such as a CNAME to an auto-registered host record |
| Port | TCP **443** (HTTPS) only. No other port is exposed |
| Certificate | Server certificate from the **corporate internal CA**: SAN `bd-poc.celstream-in.com`, PEM full chain + unencrypted key, delivered as `portal.crt` / `portal.key`. The internal root CA must already be trusted on VPN clients. Renewal: IT re-issues before expiry and the key goes into `certs\`, then redeploy (Caddy cannot auto-renew from an internal CA without an ACME endpoint; if the CA offers ACME, it can be configured) |
| VPN policy | Allow **VPN client → VPN client** (or VPN pool → this host) TCP 443. Many FortiGate/GlobalProtect policies block client-to-client traffic by default |
| Source restriction | Celstream VPN client subnet(s) only: needed for the Windows firewall rule |
| Access policy decision | Is VPN-only access sufficient under Celstream policy? If not, choose: (a) basic auth via `set-portal-auth.ps1`, or (b) Entra ID/SSO via an identity-aware proxy (requires an IT app registration; not implemented) |
| Docker Desktop licence | Confirm the Docker subscription is allowed or provided (required for commercial use above 250 staff / USD 10 M revenue) |

## Who does what

| Work | Owner | State |
|---|---|---|
| Worker/portal split, run queue, heartbeat, crash recovery, DB migration + backup | Claude | **Done.** Validated live with local processes |
| Dockerfile, compose, Caddy, `.dockerignore`, health checks, restart policies, volumes, scripts, docs | Claude | **Done.** Built, deployed and validated locally (see `docker-deployment.md`) |
| Install WSL 2 + Docker Desktop | Windows administrator | **Done** |
| Enable *Start Docker Desktop when you sign in* | Workstation user | **Pending** (currently off) |
| `scripts\deploy.ps1` (build, seed volume, start, health, local HTTPS) | Claude | **Done.** All containers healthy; local HTTPS verified |
| Firewall rule: `scripts\open-firewall.ps1 -RemoteAddress <VPN subnet>` | Windows administrator | Pending (needs VPN subnet from IT) |
| Set `HTTPS_BIND=<VPN IP>` and `PORTAL_FQDN` in `.env`; place `certs\portal.*`; redeploy | Workstation user | Pending (needs IT items) |
| DNS A record, VPN IP reservation, client-to-client policy, certificate, access-policy decision | Celstream IT / network / security | Pending |

## Remote access verification (to run once IT items are done)

On the workstation (VPN connected):
```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object InterfaceAlias -match 'Forti|PANGP|Ethernet [234]'
docker compose ps                                   # all healthy
curl.exe -sI https://bd-poc.celstream-in.com --resolve bd-poc.celstream-in.com:443:<VPN-IP>
```
From a different VPN-connected machine:
```powershell
Resolve-DnsName bd-poc.celstream-in.com             # returns the workstation VPN IP
Test-NetConnection bd-poc.celstream-in.com -Port 443  # TcpTestSucceeded : True
curl.exe -sI https://bd-poc.celstream-in.com        # HTTP/1.1 200 or 401 (if basic auth); no TLS error
```
Then open `https://bd-poc.celstream-in.com` in a browser. The certificate must be trusted
without warnings, and the banner must read **Worker online**. FQDN setup counts as complete
only when all of these pass from a second machine.
