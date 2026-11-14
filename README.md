# Celstream BD Live PoC

Fully automated live pipeline:

```
TheirStack jobs → normalize/dedupe → OpenAI capability analysis → scoring
   → Apollo (account + decision-maker contacts) → HubSpot (company + contacts + associations)
   → SQLite → Streamlit dashboard
```

## Docker deployment

See `docs/docker-deployment.md` (deploy, commands, backup/restore, troubleshooting) and
`docs/fqdn-it-handoff.md` (DNS, certificate, firewall and VPN items for IT).
`powershell -ExecutionPolicy Bypass -File scripts\deploy.ps1` builds and starts
bd-live-worker, bd-live-portal and bd-live-proxy.

## Run locally (without Docker)

The portal only queues runs; a worker process executes them:

```bash
.venv/Scripts/python.exe -m bdlive.worker
```

## Run (portal)

```bash
.venv/Scripts/streamlit.exe run app.py --server.port 8510
```

Click **Execution → ▶ Start full live workflow**. Or from the CLI:

```bash
.venv/Scripts/python.exe run_workflow.py 3
```

`scripts/probe.py` checks connectivity for all four providers.
`python run_workflow.py --reprocess-existing` re-fetches the stored jobs by ID and re-runs every
stage (idempotency check). `python -m bdlive.hubspot_schema` creates any missing `celstream_*`
HubSpot properties (also done automatically by each run). `python -m pytest -q` runs the smoke
tests. See `docs/final-execution-summary.md` for the latest verified live results.

Records rejected on review (`apollo_status = 'rejected'`) are excluded from counts, retries and syncs;
the corresponding external Apollo/HubSpot records are never deleted automatically.

## Reuse from `celstream_bd_poc`

Copied unchanged: `.env` credentials and OpenAI model/pricing, `config/capabilities.yaml`,
`config/scoring.yaml`, `config/live_search.yaml`, `config/apollo.yaml`, `config/hubspot.yaml`
and `prompts/hiring_analysis.txt` (prompt v3). Endpoints, auth headers, payloads, Apollo
role titles, the evidence-must-appear-verbatim rule and the scoring formula are ported from
the reference project. `config/live_run.yaml` holds the run-size and extra live filters.

## Layout

| Module | Role |
|---|---|
| `bdlive/theirstack.py` | Job search, pagination, normalization, contact-detail scrubbing |
| `bdlive/analysis.py` | OpenAI Responses API, strict JSON schema, evidence validation, cost |
| `bdlive/scoring.py` | Company opportunity score (scoring.yaml) |
| `bdlive/apollo.py` | Org enrich, people search/match, account/contact create/update, labels |
| `bdlive/hubspot.py` | Company/contact upsert, v4 default association |
| `bdlive/workflow.py` | Orchestrator, per-integration failure isolation, run status |
| `bdlive/http.py` | Timeouts, bounded retries, 429 handling, integration log, secret scrubbing |
| `bdlive/db.py` | SQLite schema (`data/bd_live.db`) |
| `bdlive/verify.py` | Read-back of destination records from Apollo/HubSpot APIs |

## Behaviour notes

- Dedupe: TheirStack job ID (`job_id_not` excludes already-seen jobs, so credits are only spent
  on new postings); companies by web domain; Apollo accounts by domain, contacts by Apollo
  person/contact ID; HubSpot companies by `domain`, contacts by `email` then `celstream_apollo_id`.
- HubSpot standard fields are only filled when empty; `celstream_*` fields are app-owned and
  refreshed. Properties missing from the portal are skipped (no schema changes are made).
- Apollo has no score custom fields, so score/priority go on Apollo labels
  (`Celstream BD`, `Celstream BD Priority: <band>`).
- Apollo enrichment never requests personal emails or phone numbers. Only work emails at the
  target domain are kept. The Apollo API returns only the last-name initial on the current plan.
- Run status: `success` only when every stage succeeded; otherwise `partial` or `failed`.
  Apollo failures never block HubSpot.
