# Docker deployment — Celstream BD Live PoC (Windows 11 + Docker Desktop)

Status on 14 Nov 2026: **deployed and running locally** on Docker Desktop 4.x (Engine 29.9.3,
Compose v5.7.0, WSL 2) on RAGHUGN-LAP. HTTPS is served on `127.0.0.1:443` with the interim
Caddy internal CA. Remote access over the VPN through the FQDN is **not set up yet**; it waits
on the IT items in `fqdn-it-handoff.md`.

## Validation results (14 Nov 2026)

| # | Check | Result |
|---|---|---|
| 1–3 | Build images, start containers, health checks | Pass. Worker, portal and proxy all `healthy` |
| 4 | Portal locally | Pass. `https://localhost` and `https://bd-poc.localhost` → 200 |
| 5 | Worker operation | Pass. Heartbeat online; queued runs claimed within ~2 s |
| 6–10 | Controlled live run #9 (1 stored job re-fetched, re-analysed) | Pass. TheirStack 1/1 call, OpenAI 1/1 (5,223 in / 1,927 out tokens, $0.0015), Apollo 5/5 calls, HubSpot 13/13 calls. **0 CRM records created**; 1 company + 2 contacts updated on each platform; totals unchanged (Apollo 9/17, HubSpot 10/19) |
| 11 | Execution status updates | Pass. Stage and progress were updated through all 6 stages |
| 12–13 | Portal restart and persistence | Pass. Healthy again; all table counts identical; worker unaffected |
| 14 | Worker restart while idle | Pass. Graceful SIGTERM stop, healthy again in about 1 s |
| 15 | Worker killed mid-run (run #10, Apollo stage) | Pass. On restart run #10 was marked `failed: interrupted … safe to re-run`; re-queued run #11 succeeded with 0 records created |
| 16 | Restart behaviour | Pass. An unexpected worker exit was restarted by Docker automatically (restart count 1). After `docker desktop restart`, all 3 containers came back healthy within ~30 s with the data intact. Note: `docker kill`/`docker stop` count as manual stops, which `unless-stopped` deliberately does not restart |
| 17 | HTTPS locally | Pass with the interim certificate. TLS 1.3, Caddy Local Authority (12 h certificates, auto-renewed), HSTS/nosniff/DENY/no-referrer headers, `Server` header removed, unknown hostnames refused at the handshake. Streamlit 8501 is not reachable from the host. Browsers report the certificate as untrusted until the corporate certificate is installed |
| 18 | Remote VPN access | **Not done.** Instructions are in `fqdn-it-handoff.md` |

Data migration: the `bd-live-data` volume was seeded from `data\bd_live.db` (9 jobs, 9
companies, 17 contacts, 8 runs, 874 log rows; integrity `ok`). **The Docker volume is now the
system of record;** `data\bd_live.db` on the host is a stale copy. Don't run a local worker
against it.

**Docker Desktop autostart is currently OFF** (`AutoStart=false`). Turn on
*Settings > General > Start Docker Desktop when you sign in*, or the stack won't come back
after a workstation reboot.

## Architecture

```
VPN user ──HTTPS 443──▶ bd-live-proxy (Caddy) ──▶ bd-live-portal (Streamlit :8501, internal)
                                                         │  reads + queues runs
                                     bd-live-data volume ▼  (SQLite, WAL)
                                                  bd-live-worker (executes runs)
                                                         └──▶ TheirStack, OpenAI, Apollo, HubSpot
```

| Container | Role | Published | Health check |
|---|---|---|---|
| `bd-live-proxy` | HTTPS termination, FQDN routing, optional basic auth | **443 only** (default bind 127.0.0.1) | Caddy admin API |
| `bd-live-portal` | Streamlit portal; queues runs, never executes them | none (internal 8501) | `/_stcore/health` |
| `bd-live-worker` | Claims queued runs and executes the unchanged workflow; heartbeat every 10 s | none | heartbeat younger than 60 s |

**Run execution.**
1. The portal inserts a `queued` row in `processing_runs`. A queued or running run blocks
   new requests, so runs never overlap.
2. The worker claims the row inside an `IMMEDIATE` transaction, runs the workflow, and writes
   stage, progress and summary back to the database.
3. Restarting the portal does not affect the worker.
4. If the worker stops mid-run, the next worker start marks that run
   `failed: interrupted … safe to re-run`. Every stage is idempotent, so you can simply
   queue it again.
5. The portal shows **Worker offline** and **status is stale** whenever the heartbeat is
   older than 60 s.

**Persistence.** Named volumes:
- `bd-live-data`: `/data/bd_live.db`
- `bd-live-backups`
- `caddy-data` and `caddy-config`

Named volumes live in Docker Desktop's WSL 2 ext4 disk, where SQLite WAL locking works across
containers. A Windows bind mount does not support that reliably, so don't use one for the
database. Schema changes are versioned (`PRAGMA user_version`), and a backup is taken
automatically before any migration.

## Prerequisites (Windows administrator)

1. **WSL 2:** run `wsl --install` in an elevated prompt, then reboot.
2. **Docker Desktop for Windows:** install it with the WSL 2 backend. Check the licence first:
   commercial use at organisations with more than 250 employees or more than USD 10 M revenue
   needs a paid Docker subscription.
3. **Autostart:** in Docker Desktop, open **Settings > General** and enable **Start Docker
   Desktop when you sign in**.
4. Optionally, set **Resources** to at least 2 CPUs and 4 GB of memory.

## Configuration (`.env` in the project root, never copied into the image)

| Key | Purpose |
|---|---|
| `THEIRSTACK_API_KEY`, `OPENAI_API_KEY`, `APOLLO_API_KEY`, `HUBSPOT_ACCESS_TOKEN` | Provider credentials (unchanged) |
| `OPENAI_MODEL`, `OPENAI_*_USD_PER_MILLION`, `OPENAI_MAX_OUTPUT_TOKENS`, `AI_RUN_BUDGET_USD` | Model and pricing (unchanged) |
| `PORTAL_FQDN` | `bd-poc.celstream-in.com` (approved). Default `bd-poc.localhost` |
| `HTTPS_BIND` | Host IP that 443 is published on. Default `127.0.0.1` (local only). For office + VPN access: the corporate LAN IP **10.255.109.70**. `deploy.ps1` requires a validated corporate certificate for any non-loopback value |
| `PORTAL_MODE` | `read-only` (default): no run controls or API verification in the portal; operators use `scripts\start-run.ps1` on the host. `operator`: run controls for everyone who can reach the portal |
| `HTTPS_PORT` | Default `443` |
| `WORKER_POLL_SECONDS` | Queue poll interval (currently 2) |

Credentials reach the containers through `env_file` at runtime. Note: anyone with Docker
access on this workstation can read them with `docker inspect`. Values must not contain `$`,
because Compose interpolates it; the current values don't.

## Deploy

```powershell
cd E:\Internal\BD\celstream_bd_live_poc
powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1
```

`deploy.ps1` does the following:
1. Checks Docker and the required `.env` key names.
2. Picks the TLS mode: `certs\portal.crt` + `certs\portal.key` if present, otherwise the
   interim Caddy internal CA.
3. Builds the image.
4. Seeds `bd-live-data` from `data\bd_live.db` on first deployment only. It is
   integrity-checked and never overwrites a populated volume.
5. Runs `up -d` and waits for all three containers to be healthy.
6. Checks HTTPS locally.

## Everyday commands

```powershell
docker compose ps                                  # status + health
docker compose logs -f bd-live-worker              # follow worker progress
docker compose restart bd-live-portal              # restart portal (worker unaffected)
docker compose stop bd-live-worker                 # stop worker (finishes or interrupts current run)
docker compose up -d                               # start / apply changes
docker compose down                                # stop and remove containers (volumes kept)
docker compose build; docker compose up -d         # deploy code changes
docker compose exec bd-live-worker python -m bdlive.dbtools status
powershell -File scripts\check-autostart.ps1       # autostart + restart policies
```

Never run `docker compose down -v` unless you mean to delete the database volume.

Run a workflow from the portal (**Live execution > Start**). Alternatively, queue one from the
CLI; this is the same queue and creates no new CRM records:

```powershell
docker compose exec bd-live-worker python -c "from bdlive import runs; print(runs.request_run({'max_jobs':1,'reprocess_existing':True,'job_ids':['theirstack:943837084']}, source='cli'))"
```

## Restart behaviour

- **Restart policy:** all containers use `restart: unless-stopped`. They come back when
  Docker Desktop starts, and after a crash.
- **Sign-in required:** Docker Desktop starts only after a user signs in to Windows, and only
  if autostart is enabled.
- **Not always available:** the portal is down whenever the workstation is off, asleep,
  signed out, or disconnected from the VPN.

## Backup and restore

```powershell
powershell -File scripts\backup.ps1 -Label before-upgrade     # online; copy also in .\backups
powershell -File scripts\restore.ps1 -File backups\<file>.db  # stops worker+portal, safety backup, restore, restart
```

A pre-migration backup is taken automatically before each schema upgrade. The local
pre-Docker backup is `data\backups\bd_live-pre-migration-v0-to-v2-*.db`.

## Operator commands on the host

```powershell
powershell -File scripts\start-run.ps1 -MaxJobs 3 -SyncExisting   # queue a live run
powershell -File scripts\start-run.ps1 -Reprocess                  # re-check stored jobs, no new CRM records
powershell -File scripts\start-run.ps1 -Status                     # worker/run status (same data as /status.json)
```

`https://<fqdn>/status.json` publishes the worker heartbeat, run state and counts (no credentials,
contacts or CRM data) so access can be validated with `scripts\validate-access.ps1`.

## HTTPS and access

- **Shared use:** `scripts\new-cert-request.ps1 -Request` creates the key and CSR for IT; `-Install`
  or `-FromPfx` installs the returned certificate. `deploy.ps1` validates it with `bdlive.certcheck`
  (SAN, validity, key match, chain, not self-signed) before serving it, and removes the interim
  `localhost` site in corporate mode. Network, DNS, firewall and SSO steps: `fqdn-it-handoff.md`.
- **Interim TLS:** without corporate certificates, `tls internal` is used. It is for local
  validation only, because other machines won't trust it.
- **Optional login:** `scripts\set-portal-auth.ps1` adds HTTPS basic auth (bcrypt hash only).
  `-Disable` reverts to VPN-only. Corporate SSO would need an identity-aware proxy (e.g.
  oauth2-proxy with Entra ID) and an IT-registered app; it is not implemented.

## Troubleshooting

| Symptom | Check |
|---|---|
| Portal says **Worker offline** | `docker compose ps`, `docker compose logs bd-live-worker`. Queued runs start once the worker is healthy |
| Run stuck **running** with a stale warning | The worker was stopped. Start it: the run is marked interrupted; queue it again |
| "Not started: run #N is already queued/running" | Expected duplicate-run protection. Wait, or check the worker |
| `database is locked` in logs | Transient; there is a 30 s busy timeout. Persistent: make sure only one worker is running and the DB is on the named volume |
| Proxy unhealthy / 502 | `docker compose logs bd-live-proxy`. The portal must be healthy first (`depends_on`) |
| Browser certificate warning | Interim internal CA in use. Install the corporate certificate in `certs\` and redeploy |
| Remote users time out | VPN IP changed, `HTTPS_BIND` is still 127.0.0.1, firewall rule missing, or VPN policy blocks client-to-client traffic. See the IT handoff |
| A provider fails | Portal: **Executive overview > Integration status** and **Execution history > Integration log** (errors only) |
| Port 443 in use | `Get-NetTCPConnection -LocalPort 443 -State Listen`. Set `HTTPS_PORT` to e.g. 8443 and tell IT |
