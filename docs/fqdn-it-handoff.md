# bd-poc.celstream-in.com — internal network deployment handoff

Updated 14 Nov 2026. **Status: NOT COMPLETE.** Neither an office user nor a VPN user can reach
`https://bd-poc.celstream-in.com` yet. The DNS record, corporate certificate, firewall rule and
VPN DNS settings below are still outstanding.

## Target

| Item | Value |
|---|---|
| URL (office and VPN, same name) | `https://bd-poc.celstream-in.com` |
| Hosting workstation | Windows 11 + Docker Desktop, corporate LAN IP **10.255.109.70** (subnet 10.255.109.0/24) |
| Primary DNS destination | **10.255.109.70**, the corporate LAN IP, never a VPN-assigned address |
| Exposed service | TCP **443** only (Caddy proxy `bd-live-proxy`, bound to 10.255.109.70) |
| Never exposed | Streamlit 8501, `bd-live-worker`, SQLite / Docker volumes |
| Certificate | Issued by the corporate internal CA, SAN `DNS:bd-poc.celstream-in.com`. The interim Caddy local CA is refused by `deploy.ps1` for any shared (non-loopback) binding |

```
Office PC ──(office LAN)────────────────┐
                                         ├─ TCP 443 ─▶ 10.255.109.70 ─▶ bd-live-proxy (Caddy, corporate cert)
VPN PC ──(FortiClient SSL-VPN tunnel)───┘                    └─▶ bd-live-portal :8501 (internal only)
        DNS: bd-poc.celstream-in.com A 10.255.109.70             bd-live-worker  (internal only)
```

## Facts verified so far (from VPN client RAGHUGN-LAP, 14 Nov 2026)

| Check | Result |
|---|---|
| VPN client | FortiClient SSL-VPN, client IP 192.168.10.1/32, tunnel next-hop 192.168.10.2 |
| VPN split-tunnel routes | 10.255.10/11/12/106/108/**109**/110/122.0/24, so **10.255.109.0/24 is already routed** to VPN clients |
| Ping 10.255.109.70 over VPN | **OK** |
| TCP 443 over VPN | TCP connects, but **every TLS handshake is reset** (SNI `bd-poc.celstream-in.com`, `localhost`, `bd-poc.localhost`). No HTTPS is served to remote clients yet. The cause must be confirmed on the host; the expected cause is that the stack is still bound to 127.0.0.1 until the corporate certificate is installed |
| TCP 8501 (Streamlit) over VPN | Closed (correct) |
| TCP 80 / 3389 over VPN | 80 accepts but never responds; 3389 (RDP) is open. Neither is part of this application. The host owner and IT should confirm they are intended |
| **VPN DNS** | **The VPN pushes no corporate DNS.** The client keeps using its home router (192.168.1.1), and `_ldap._tcp.dc._msdcs.celstream-in.com` does not resolve. **VPN users therefore cannot resolve any internal name today**, including the new record |
| `bd-poc.celstream-in.com` | Does not exist yet |

Not yet verified (they must be checked on the host or confirmed by IT):
- whether 10.255.109.70 is static or DHCP-reserved
- the host's own DNS servers
- office subnets and inter-VLAN filtering
- FortiGate SSL-VPN → LAN policy for 443
- the host firewall

To collect the host-side facts, run `scripts\network-report.ps1` on 10.255.109.70.

---

## A. Requires Celstream IT / network / security approval

| # | Request | Detail / acceptance |
|---|---|---|
| IT-1 | **Reserve the host IP** | 10.255.109.70 for the hosting workstation: a DHCP reservation (preferred) or an approved static address, so the DNS record never goes stale |
| IT-2 | **Internal DNS A record** | `bd-poc.celstream-in.com  A  10.255.109.70` in the internal `celstream-in.com` zone only, never public DNS. TTL 300–3600 |
| IT-3 | **VPN DNS** | Configure the FortiClient SSL-VPN portal to push the corporate DNS servers (and split-DNS for `celstream-in.com`). Acceptance: `Resolve-DnsName bd-poc.celstream-in.com` returns 10.255.109.70 on a VPN client |
| IT-4 | **Office routing / filtering** | Office user subnets → 10.255.109.70 TCP 443 allowed (inter-VLAN ACLs or FortiGate policies, if any) |
| IT-5 | **VPN policy** | FortiGate SSL-VPN policy: VPN client pool (e.g. 192.168.10.0/24, IT to confirm) → 10.255.109.70 TCP 443. The route already exists; this is the policy permission |
| IT-6 | **No public exposure** | No VIP, port-forward or public DNS for this host or name |
| IT-7 | **Subnets for the host firewall** | The authoritative list of office LAN subnets and the VPN client pool, used by `open-firewall.ps1` |
| IT-8 | **Certificate** | Sign `certs\portal.csr` (generated on the host by `scripts\new-cert-request.ps1 -Request`): Web Server template, SAN `DNS:bd-poc.celstream-in.com`. Return a PEM/CER plus the issuing-CA chain (or a PFX). The corporate root must already be trusted on office and VPN PCs (GPO/Intune). Renewal: IT re-issues before expiry |
| IT-9 | **Access policy decision** | Confirm the interim model (network-restricted, read-only portal) until SSO is ready; see section D |
| IT-10 | **Entra ID SSO** (optional, next phase) | App registration and security groups; see section D |
| IT-11 | **Docker Desktop licence** | Confirm a Docker subscription covers this commercial use |

## B. Requires Windows administrator rights on 10.255.109.70

| # | Task | Command |
|---|---|---|
| WA-1 | Inbound firewall rule: 443 on the LAN IP only, approved subnets only | `powershell -ExecutionPolicy Bypass -File scripts\open-firewall.ps1 -LanIp 10.255.109.70 -OfficeSubnets <IT-7 office list> -VpnSubnets <IT-7 VPN pool>` |
| WA-2 | Keep the host available | Docker Desktop: *Start when you sign in* = on. Power plan: never sleep on AC. The host must stay signed in, powered on and on the LAN |
| WA-3 | Review the extra open ports | 80 and 3389 are reachable from the VPN. Not required by this app |

## C. Implementation (Claude): done in the repository; apply on the host

| Item | State |
|---|---|
| `HTTPS_BIND` support: publish 443 on the LAN IP only. `deploy.ps1` refuses if the IP isn't an active address on the host | Done |
| `deploy.ps1` refuses shared binding without a **validated corporate certificate** (`bdlive.certcheck`: SAN, validity, key match, not self-signed, chain present). In corporate mode the interim `localhost` / local-CA site is removed entirely | Done (checker tested with test certificates) |
| `scripts\new-cert-request.ps1`: private key + CSR (`-Request`), install the returned certificate and chain (`-Install`), or extract from a PFX (`-FromPfx`). The key never leaves the host | Done |
| `scripts\open-firewall.ps1`: scoped by local IP and remote subnets, all profiles, 443 only | Done |
| Read-only portal by default (`PORTAL_MODE=read-only`): no run controls and no API verification for general users. Operators queue runs on the host with `scripts\start-run.ps1` | Done |
| `/status.json`: worker heartbeat, run state and counts. No credentials, contact details or CRM data. Used for remote validation | Done |
| `scripts\validate-access.ps1`: standalone client test (DNS, TCP, **trusted** certificate, health, heartbeat, data, monitoring) with a JSON evidence file | Done |
| `scripts\network-report.ps1`: host facts for IT | Done |
| Business logic, integrations, containers and existing volumes/data | Unchanged; redeploying keeps `bd-live-data` |

### Apply on 10.255.109.70 (host operator)

```powershell
cd <project folder>; git pull
powershell -ExecutionPolicy Bypass -File scripts\backup.ps1 -Label before-lan
powershell -ExecutionPolicy Bypass -File scripts\network-report.ps1
powershell -ExecutionPolicy Bypass -File scripts\new-cert-request.ps1 -Request      # send certs\portal.csr to IT (IT-8)
# when IT returns the certificate:
powershell -ExecutionPolicy Bypass -File scripts\new-cert-request.ps1 -Install -Certificate <bd-poc.cer> -Chain <issuing-ca.cer>
# .env:  PORTAL_FQDN=bd-poc.celstream-in.com   HTTPS_BIND=10.255.109.70   HTTPS_PORT=443   PORTAL_MODE=read-only
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1                        # refuses without a valid corporate cert
```

Until the certificate arrives, keep `HTTPS_BIND=127.0.0.1` (local only). `deploy.ps1` keeps the
existing database volume; the seed step is skipped because the volume is already populated.

## D. Authentication: Entra ID / SSO recommendation

**Recommended design:** keep Caddy in front, and add **oauth2-proxy** (OIDC against Microsoft
Entra ID) as a `forward_auth` gate. Roles come from Entra security groups. oauth2-proxy passes
the user and groups to the portal in request headers (`X-Auth-Request-Email`,
`X-Auth-Request-Groups`).

| Role | Entra group (proposed) | Permissions |
|---|---|---|
| General employee | `BD-Portal-Readers` (or all Celstream staff) | Read-only: overview, opportunities, Apollo/HubSpot views, live monitoring, history |
| BD operator | `BD-Portal-Operators` | Plus: start or reprocess workflow runs, API read-back verification |
| Administrator | `BD-Portal-Admins` | Plus: maintenance, i.e. backups, restore, redeploy, configuration. Keep these on the host (CLI and scripts); they are not exposed through the web portal |

**What IT provides:**
- the tenant ID
- an app registration (client ID plus a client secret or certificate) with redirect URI
  `https://bd-poc.celstream-in.com/oauth2/callback`
- the `groups` claim (or app roles) and the three group object IDs
- admin consent
- outbound HTTPS from the host to `login.microsoftonline.com`

**Implementation once IT has provided these** (about half a day): add the oauth2-proxy
container, add a `forward_auth` block in `proxy/auth/active.caddy`, and add a `PORTAL_MODE=sso`
role check in `app.py`. Secrets go in `.env` only.

**Interim, until SSO (implemented):** the portal is reachable only from the approved office and
VPN subnets, and it is read-only for everyone. Runs are started by operators on the host
(`scripts\start-run.ps1`). An optional shared login is available via `scripts\set-portal-auth.ps1`.

**Secrets:**
- The dashboard and `/status.json` never show API keys or tokens. Credentials exist only in
  `.env`, which is injected at runtime and excluded from the image and from git.
- Error text written to logs or shown in the UI is scrubbed of credential values.
- The host's Docker administrators can still read the credentials with `docker inspect`, so
  limit who administers the host.

## E. Validation (completion criteria)

Run `scripts\validate-access.ps1` (standalone; copy it to each machine) on each machine below,
then open the URL in a browser:

| Where | Command | Must pass |
|---|---|---|
| Hosting workstation 10.255.109.70 | `validate-access.ps1 -Location Host -ExpectedIp 10.255.109.70` | All 8 checks |
| Second office workstation | `validate-access.ps1 -Location Office -ExpectedIp 10.255.109.70` | All 8 checks + browser: no certificate warning, "Worker online" |
| Separate PC on the Celstream VPN | `validate-access.ps1 -Location VPN -ExpectedIp 10.255.109.70` | All 8 checks + browser |

The checks are: DNS resolution, TCP 443, trusted certificate, portal health, worker heartbeat,
current data, execution monitoring, and portal page.

**The deployment is complete only when the Office and VPN rows both pass.** Keep the JSON
evidence files the script writes.

Current state: Host not run; Office not run; VPN partial (routing, ping and TCP 443 OK; DNS
absent; TLS reset).
