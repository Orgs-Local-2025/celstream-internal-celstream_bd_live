"""Celstream BD Live PoC portal.  Run:  streamlit run app.py

The portal never executes the workflow itself: it queues a run (bdlive.runs) and the
bd-live-worker process executes it. Status panels refresh as Streamlit fragments, so only
the live parts of the page rerun.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from functools import lru_cache

import pandas as pd
import streamlit as st

from bdlive import db, runs
from bdlive.settings import settings
from bdlive.theirstack import run_config

st.set_page_config(page_title="Celstream BD Live PoC", page_icon="📈", layout="wide")
db.init()

STATUS_ICON = {"success": "🟢 success", "failed": "🔴 failed", "partial": "🟠 partial",
               "pending": "⚪ pending", "running": "🔵 running", "queued": "🟣 queued",
               "skipped": "⚫ skipped", "rejected": "⛔ rejected", "interrupted": "🔴 interrupted"}
VALID_CONTACT = "apollo_status != 'rejected'"
VALID_COMPANY = "apollo_status != 'rejected'"
LIVE_REFRESH = "5s"
# read-only (default for the shared deployment): no run controls, no API verification.
# operator: run controls enabled for everyone who can reach the portal.
PORTAL_MODE = os.environ.get("PORTAL_MODE", "operator").strip().lower()
READ_ONLY = PORTAL_MODE != "operator"
OVERVIEW_REFRESH = "30s"


def badge(value):
    return STATUS_ICON.get(value or "pending", value or "—")


def fmt_time(value):
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        return value


def fmt_age(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def elapsed(start, end=None):
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end) if end else datetime.now(UTC)
        return (b - a).total_seconds()
    except (TypeError, ValueError):
        return None


@lru_cache
def hubspot_portal() -> str | None:
    try:
        from bdlive.hubspot import HubSpotClient

        client = HubSpotClient()
        try:
            return str(client.request("GET", "/account-info/v3/details", "account_info")
                       .get("portalId"))
        finally:
            client.close()
    except Exception:  # noqa: BLE001
        return None


def hs_link(kind: str, object_id):
    portal = hubspot_portal()
    if not object_id or not portal:
        return None
    type_id = "0-2" if kind == "company" else "0-1"
    return f"https://app.hubspot.com/contacts/{portal}/record/{type_id}/{object_id}"


def apollo_link(kind: str, object_id):
    if not object_id:
        return None
    return f"https://app.apollo.io/#/{'accounts' if kind == 'account' else 'contacts'}/{object_id}"


def df(rows, columns=None):
    frame = pd.DataFrame(rows)
    if columns and not frame.empty:
        frame = frame[[c for c in columns if c in frame.columns]]
    return frame


def table(rows, columns=None, fit=False, **kwargs):
    height = 36 * (len(rows) + 1) + 4 if fit and rows else "auto"
    st.dataframe(df(rows, columns), width="stretch", hide_index=True, height=height, **kwargs)


def integration_status(summary: dict, provider: str) -> str:
    """Per-run integration outcome derived from the persisted run summary."""
    errors = [e for e in summary.get("errors", []) if e.lower().startswith(provider)]
    ok = summary.get(f"{provider}_accounts" if provider == "apollo" else
                     f"{provider}_companies", 0)
    failed = summary.get(f"{provider}_failed", 0)
    if not ok and not failed and not errors:
        return "—"
    if failed and not ok:
        return "failed"
    return "partial" if failed or errors else "success"


def company_overall(apollo_status, hubspot_status):
    statuses = {apollo_status, hubspot_status}
    if "rejected" in statuses:
        return "rejected"
    if statuses == {"success"}:
        return "success"
    if statuses == {"failed"}:
        return "failed"
    if statuses <= {"pending"}:
        return "pending"
    if statuses == {"skipped"}:
        return "skipped"
    return "partial"


# ------------------------------------------------------------------ header + worker banner
st.title("Celstream BD — Live Automated PoC")
st.caption("TheirStack → OpenAI capability analysis → scoring → Apollo → HubSpot. "
           "All figures are read live from the shared run database; no demo values."
           + (" · Read-only view" if READ_ONLY else " · Operator mode"))
missing = settings().missing_credentials()
if missing:
    st.error(f"Missing configuration: {', '.join(missing)}")


@st.fragment(run_every="10s")
def worker_banner():
    w = runs.worker_state()
    active = runs.active_run()
    if not w["online"]:
        st.error(f"⚠ Worker offline — last heartbeat {fmt_age(w['age'])} ago "
                 f"({fmt_time(w.get('heartbeat_at'))}). Queued runs will not start and any "
                 "'running' status below may be stale.")
    elif active:
        st.info(f"Worker online · run #{active['id']} {active['status']} · {active['stage']}")
    else:
        st.success(f"Worker online · idle · heartbeat {fmt_age(w['age'])} ago")


worker_banner()

tabs = st.tabs(["Executive overview", "Live execution", "Opportunity intelligence",
                "AI analysis", "Jobs", "Apollo", "HubSpot", "Execution history"])


# ------------------------------------------------------------------ A. executive overview
@st.fragment(run_every=OVERVIEW_REFRESH)
def executive_overview():
    m = db.one(f"""SELECT
        (SELECT COUNT(*) FROM jobs) jobs,
        (SELECT COUNT(*) FROM capability_analysis WHERE status='success') analysed,
        (SELECT COUNT(*) FROM capability_analysis WHERE status='failed') analysis_failed,
        (SELECT COUNT(*) FROM companies WHERE {VALID_COMPANY}) companies,
        (SELECT COUNT(*) FROM companies WHERE NOT ({VALID_COMPANY})) rejected_companies,
        (SELECT COUNT(*) FROM contacts WHERE {VALID_CONTACT}) contacts,
        (SELECT COUNT(*) FROM contacts WHERE NOT ({VALID_CONTACT})) rejected_contacts,
        (SELECT COUNT(*) FROM scoring_results s JOIN companies c ON c.key=s.company_key
            WHERE s.band='High' AND c.{VALID_COMPANY}) high,
        (SELECT COUNT(*) FROM scoring_results s JOIN companies c ON c.key=s.company_key
            WHERE s.band='Medium' AND c.{VALID_COMPANY}) medium,
        (SELECT COUNT(*) FROM companies WHERE apollo_account_id IS NOT NULL AND
            {VALID_COMPANY}) apollo_accounts,
        (SELECT COUNT(*) FROM contacts WHERE apollo_contact_id IS NOT NULL AND
            {VALID_CONTACT}) apollo_contacts,
        (SELECT COUNT(*) FROM companies WHERE hubspot_id IS NOT NULL AND
            {VALID_COMPANY}) hs_companies,
        (SELECT COUNT(*) FROM contacts WHERE hubspot_id IS NOT NULL AND
            {VALID_CONTACT}) hs_contacts,
        (SELECT COUNT(*) FROM contacts WHERE hubspot_associated=1 AND {VALID_CONTACT}) hs_assoc,
        (SELECT COUNT(*) FROM companies WHERE apollo_status IN ('partial','failed') OR
            hubspot_status IN ('partial','failed')) incomplete,
        (SELECT COUNT(*) FROM integration_logs WHERE success=0 AND
            created_at >= strftime('%Y-%m-%dT%H:%M:%S','now','-1 day')) failures_24h,
        (SELECT ROUND(COALESCE(SUM(cost_usd),0),4) FROM capability_analysis) cost""")
    cols = st.columns(6)
    cols[0].metric("Total jobs", m["jobs"])
    cols[1].metric("Jobs analysed", m["analysed"])
    cols[2].metric("Companies", m["companies"])
    cols[3].metric("Contacts", m["contacts"])
    cols[4].metric("High-priority", m["high"])
    cols[5].metric("Medium-priority", m["medium"])
    cols = st.columns(6)
    cols[0].metric("Apollo accounts", m["apollo_accounts"])
    cols[1].metric("Apollo contacts", m["apollo_contacts"])
    cols[2].metric("HubSpot companies", m["hs_companies"])
    cols[3].metric("HubSpot contacts", m["hs_contacts"])
    cols[4].metric("Integration failures (24h)", m["failures_24h"])
    last = db.one("SELECT * FROM processing_runs WHERE status NOT IN ('queued','running')"
                  " ORDER BY id DESC LIMIT 1")
    cols[5].metric("Last execution", f"#{last['id']} {last['status']}" if last else "—")
    st.caption(f"HubSpot associations: {m['hs_assoc']} · companies with incomplete CRM sync: "
               f"{m['incomplete']} · OpenAI analyses failed: {m['analysis_failed']} · rejected "
               f"after review (excluded): {m['rejected_companies']} companies, "
               f"{m['rejected_contacts']} contacts · OpenAI spend to date: ${m['cost']}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Latest executions by outcome**")
        rows = []
        for status in ("success", "partial", "failed"):
            r = db.one("SELECT id, finished_at, error FROM processing_runs WHERE status=?"
                       " ORDER BY id DESC LIMIT 1", (status,))
            rows.append({"outcome": badge(status), "run": f"#{r['id']}" if r else "—",
                         "finished": fmt_time(r["finished_at"]) if r else "—",
                         "age": fmt_age(elapsed(r["finished_at"])) if r else "—"})
        table(rows)
    with right:
        st.markdown("**Integration status (last successful call / last failure)**")
        rows = db.query("""SELECT provider,
            MAX(CASE WHEN success=1 THEN created_at END) last_success,
            MAX(CASE WHEN success=0 THEN created_at END) last_failure,
            SUM(CASE WHEN success=0 AND created_at >= strftime('%Y-%m-%dT%H:%M:%S','now',
                '-1 day') THEN 1 ELSE 0 END) failures_24h
            FROM integration_logs GROUP BY provider ORDER BY provider""")
        for r in rows:
            healthy = not r["last_failure"] or (r["last_success"] or "") > r["last_failure"]
            r["status"] = "🟢 healthy" if healthy else "🔴 last call failed"
            r["last_success"], r["last_failure"] = fmt_time(r["last_success"]), fmt_time(
                r["last_failure"])
        table(rows, ["provider", "status", "last_success", "last_failure", "failures_24h"])
    sync = db.one(f"""SELECT
        (SELECT MAX(apollo_synced_at) FROM companies WHERE apollo_status='success') apollo,
        (SELECT MAX(hubspot_synced_at) FROM companies WHERE hubspot_status='success') hubspot""")
    st.caption(f"Last successful CRM synchronisation — Apollo: {fmt_time(sync['apollo'])} · "
               f"HubSpot: {fmt_time(sync['hubspot'])}")

    st.subheader("Company opportunities")
    rows = db.query(f"""SELECT c.name, c.domain, c.country, c.employees, s.total opportunity_score,
        s.band priority, (SELECT MAX(a.capability_score) FROM capability_analysis a JOIN jobs j
        ON j.id=a.job_id WHERE j.company_key=c.key) capability_score,
        s.matched_capabilities_json caps,
        (SELECT COUNT(*) FROM contacts k WHERE k.company_key=c.key AND k.{VALID_CONTACT})
        contacts, c.apollo_status, c.hubspot_status FROM companies c LEFT JOIN scoring_results s
        ON s.company_key=c.key ORDER BY s.total DESC NULLS LAST, c.name""")
    for r in rows:
        r["caps"] = ", ".join(db.loads(r["caps"], []))
        r["overall"] = badge(company_overall(r["apollo_status"], r["hubspot_status"]))
        r["apollo_status"], r["hubspot_status"] = badge(r["apollo_status"]), badge(
            r["hubspot_status"])
    table(rows, ["name", "overall", "opportunity_score", "priority", "capability_score",
                 "contacts", "apollo_status", "hubspot_status", "domain", "country",
                 "employees", "caps"], fit=True)


with tabs[0]:
    executive_overview()


# ------------------------------------------------------------------ B. live execution
def run_controls():
    if READ_ONLY:
        st.info("Read-only portal: workflow runs are started by BD operators on the host "
                "(scripts/start-run.ps1). Live progress of any run appears below.")
        return
    cfg = run_config()
    with st.form("start_run", border=True):
        st.markdown("**Start a workflow run** (executed by the bd-live-worker container)")
        c1, c2, c3 = st.columns([1, 2, 1])
        max_jobs = c1.number_input("New jobs to retrieve", 1, int(cfg["max_jobs_limit"]),
                                   int(cfg["default_max_jobs"]))
        sync_existing = c2.checkbox("Retry companies with failed/partial CRM sync", value=True)
        reprocess = c2.checkbox("Reprocess stored jobs instead (no new CRM records; "
                                "idempotency check)", value=False)
        submitted = c3.form_submit_button("▶ Start full live workflow", type="primary",
                                          disabled=bool(missing), width="stretch")
    if submitted:
        try:
            run_id = runs.request_run({"max_jobs": int(max_jobs),
                                       "sync_existing": sync_existing,
                                       "reprocess_existing": reprocess}, source="portal")
            st.success(f"Run #{run_id} queued; the worker will pick it up within seconds.")
        except runs.RunAlreadyActive as exc:
            st.warning(f"Not started: run #{exc.run_id} is already {exc.status}.")


@st.fragment(run_every=LIVE_REFRESH)
def live_execution():
    w = runs.worker_state()
    run = runs.active_run() or db.one("SELECT * FROM processing_runs ORDER BY id DESC LIMIT 1")
    if not run:
        st.info("No runs yet.")
        return
    s = db.loads(run["summary_json"], {}) or {}
    is_active = run["status"] in runs.ACTIVE
    heartbeat_age = runs.age_seconds(run.get("heartbeat_at"))
    stale = run["status"] == "running" and (
        not w["online"] or heartbeat_age is None or heartbeat_age > runs.HEARTBEAT_STALE_SECONDS)
    title = "Current run" if is_active else "Most recent run (nothing running now)"
    st.subheader(f"{title}: #{run['id']} — {badge(run['status'])}")
    if stale:
        st.error("⚠ This run's status is stale: no worker heartbeat for "
                 f"{fmt_age(heartbeat_age)}. It will be marked failed (interrupted) when the "
                 "worker restarts.")
    if run["status"] == "queued" and not w["online"]:
        st.warning("Queued, but the worker is offline.")
    st.progress(float(run.get("progress") or 0),
                text=f"{run['stage'] or ''}")
    c = st.columns(6)
    c[0].metric("Jobs retrieved", s.get("jobs_retrieved", 0))
    c[1].metric("Jobs analysed", s.get("jobs_analysed", 0))
    c[2].metric("Companies processed", s.get("companies", 0))
    c[3].metric("Apollo acc / contacts",
                f"{s.get('apollo_accounts', 0)} / {s.get('apollo_contacts', 0)}")
    c[4].metric("HubSpot co / contacts",
                f"{s.get('hubspot_companies', 0)} / {s.get('hubspot_contacts', 0)}")
    c[5].metric("Elapsed", fmt_age(elapsed(run["started_at"], run["finished_at"])))
    st.caption(f"Requested {fmt_time(run.get('requested_at'))} via "
               f"{run.get('trigger_source') or 'n/a'} · started {fmt_time(run['started_at'])}"
               f" · finished {fmt_time(run['finished_at'])} · worker "
               f"{run.get('worker_id') or '—'} · params {run['params_json']}")
    if s.get("errors"):
        st.markdown("**Errors / incomplete results in this run**")
        for e in s["errors"]:
            st.error(e)
    recent = db.query("SELECT created_at, provider, operation, http_status, success, entity,"
                      " error FROM integration_logs WHERE run_id=? ORDER BY id DESC LIMIT 15",
                      (run["id"],))
    if recent:
        st.markdown("**Latest integration calls for this run**")
        for r in recent:
            r["success"] = "✅" if r["success"] else "❌"
        table(recent)


with tabs[1]:
    run_controls()
    live_execution()


# ------------------------------------------------------------------ C. opportunity intelligence
with tabs[2]:
    rows = db.query(f"""SELECT c.name company, c.domain, j.title job_title, j.location,
        a.matches_json, a.capability_score, s.total opportunity_score, s.band priority,
        a.summary ai_recommendation, j.source_url job_url, c.apollo_status, c.hubspot_status
        FROM jobs j JOIN companies c ON c.key=j.company_key
        LEFT JOIN capability_analysis a ON a.job_id=j.id AND a.status='success'
        LEFT JOIN scoring_results s ON s.company_key=c.key
        WHERE c.{VALID_COMPANY}
        ORDER BY s.total DESC NULLS LAST, a.capability_score DESC NULLS LAST""")
    priorities = st.multiselect("Priority", ["High", "Medium", "Low"],
                                default=["High", "Medium", "Low"])
    out = []
    for r in rows:
        if r["priority"] and r["priority"] not in priorities:
            continue
        matches = db.loads(r.pop("matches_json"), []) or []
        r["capability_matches"] = "; ".join(
            f"{m.get('capability_name', m['capability_id'])} ({m['confidence']:.2f})"
            for m in matches)
        out.append(r)
    table(out, ["company", "domain", "job_title", "location", "capability_matches",
                "capability_score", "opportunity_score", "priority", "ai_recommendation",
                "job_url"],
          column_config={"job_url": st.column_config.LinkColumn("Job URL"),
                         "ai_recommendation": st.column_config.TextColumn(width="large")})

# ------------------------------------------------------------------ AI analysis (existing)
with tabs[3]:
    rows = db.query("""SELECT a.*, j.title, j.company_name, j.location, j.source_url
        FROM capability_analysis a JOIN jobs j ON j.id=a.job_id ORDER BY a.analysed_at DESC""")
    if not rows:
        st.info("No analyses yet.")
    for r in rows:
        header = (f"{badge(r['status'])} · {r['title']} @ {r['company_name']} — capability score "
                  f"{r['capability_score'] if r['capability_score'] is not None else '—'}")
        with st.expander(header):
            if r["status"] != "success":
                st.error(r["error"])
                continue
            st.write(r["summary"])
            matches = db.loads(r["matches_json"], [])
            if matches:
                table([{"capability": m.get("capability_name", m["capability_id"]),
                        "id": m["capability_id"], "confidence": m["confidence"],
                        "reasoning": m["explanation"], "evidence": m["evidence"]}
                       for m in matches])
            else:
                st.info("No evidenced Celstream capability match.")
            c1, c2, c3 = st.columns(3)
            c1.write("**Technologies:** " + ", ".join(db.loads(r["technologies_json"], [])))
            c2.write("**Suggested personas:** " + ", ".join(db.loads(r["personas_json"], [])))
            c3.write(f"**Model:** {r['model']} · {r['input_tokens']}→{r['output_tokens']} tokens"
                     f" · ${r['cost_usd']}")
            if caveats := db.loads(r["caveats_json"], []):
                st.caption("Caveats: " + " | ".join(caveats))
            if rejected := db.loads(r["rejected_matches_json"], []):
                st.caption(f"{len(rejected)} AI match(es) rejected because the evidence was not "
                           "found verbatim in the posting.")
            if r["source_url"]:
                st.link_button("Open job posting", r["source_url"])
    scores = db.query("SELECT c.name, s.* FROM scoring_results s JOIN companies c ON "
                      "c.key=s.company_key ORDER BY s.total DESC")
    if scores:
        st.subheader("Company opportunity scoring (scoring.yaml)")
        for s in scores:
            with st.expander(f"{s['name']}: {s['total']} ({s['band']})"):
                st.json(db.loads(s["components_json"], {}))
                for line in db.loads(s["rationale_json"], []):
                    st.write("• " + line)

# ------------------------------------------------------------------ Jobs (existing)
with tabs[4]:
    rows = db.query("""SELECT j.company_name company, j.title, j.location, j.posted_at,
        j.seniority, j.analysis_status, a.capability_score, j.source_url, j.theirstack_id,
        j.retrieved_at FROM jobs j LEFT JOIN capability_analysis a ON a.job_id=j.id
        ORDER BY j.retrieved_at DESC""")
    for r in rows:
        r["analysis_status"] = badge(r["analysis_status"])
    table(rows, column_config={"source_url": st.column_config.LinkColumn("Source URL")})

# ------------------------------------------------------------------ D. Apollo
with tabs[5]:
    prefix = run_config()["apollo"].get("label_prefix", "Celstream BD")
    st.subheader("Apollo accounts")
    rows = db.query(f"""SELECT c.name, c.domain, c.apollo_status status, s.band,
        (SELECT COUNT(*) FROM contacts k WHERE k.company_key=c.key AND
        k.apollo_contact_id IS NOT NULL AND k.{VALID_CONTACT}) linked_contacts,
        c.apollo_account_id, c.apollo_synced_at, c.apollo_error FROM companies c
        LEFT JOIN scoring_results s ON s.company_key=c.key ORDER BY c.updated_at DESC""")
    for r in rows:
        r["priority_labels"] = f"{prefix}; {prefix} Priority: {r.pop('band') or 'n/a'}"
        r["status"] = badge(r["status"])
        r["link"] = apollo_link("account", r["apollo_account_id"])
    table(rows, ["name", "domain", "status", "priority_labels", "linked_contacts",
                 "apollo_account_id", "link", "apollo_synced_at", "apollo_error"],
          column_config={"link": st.column_config.LinkColumn("Open in Apollo")})
    st.subheader("Apollo contacts (relationship: contact → account)")
    rows = db.query("""SELECT c.name account, k.first_name, k.last_name, k.title,
        k.role_category, k.email, k.apollo_status status, k.apollo_contact_id,
        c.apollo_account_id, k.apollo_synced_at, k.apollo_error FROM contacts k
        JOIN companies c ON c.key=k.company_key ORDER BY c.name, k.id""")
    for r in rows:
        r["status"] = badge(r["status"])
        r["link"] = apollo_link("contact", r["apollo_contact_id"])
    table(rows, column_config={"link": st.column_config.LinkColumn("Open in Apollo")})

# ------------------------------------------------------------------ E. HubSpot
with tabs[6]:
    from bdlive.workflow import company_context

    st.subheader("HubSpot companies")
    rows = db.query("""SELECT c.key, c.name, c.domain, c.hubspot_status status, c.hubspot_id,
        s.total opportunity_score, s.band priority, (SELECT COUNT(*) FROM contacts k WHERE
        k.company_key=c.key AND k.hubspot_associated=1 AND k.apollo_status != 'rejected')
        associated_contacts, c.hubspot_synced_at, c.hubspot_error FROM companies c
        LEFT JOIN scoring_results s ON s.company_key=c.key ORDER BY s.total DESC NULLS LAST""")
    for r in rows:
        ctx = company_context(r.pop("key"))
        r["capability_score"] = ctx["capability_score"]
        r["hiring_signals"] = ctx["hiring_text"]
        r["ai_recommendation"] = ctx["recommendation"]
        r["status"] = badge(r["status"])
        r["link"] = hs_link("company", r["hubspot_id"])
    table(rows, ["name", "domain", "status", "capability_score", "opportunity_score",
                 "priority", "associated_contacts", "hiring_signals", "ai_recommendation",
                 "hubspot_id", "link", "hubspot_synced_at", "hubspot_error"],
          column_config={"link": st.column_config.LinkColumn("Open in HubSpot"),
                         "ai_recommendation": st.column_config.TextColumn(width="large"),
                         "hiring_signals": st.column_config.TextColumn(width="large")})
    st.caption("Capability/opportunity score, hiring signals and AI recommendation are the "
               "values written to the celstream_* HubSpot properties on the last sync.")
    st.subheader("HubSpot contacts (association: contact → company)")
    rows = db.query("""SELECT c.name company, k.first_name, k.last_name, k.title, k.email,
        k.hubspot_status status, k.hubspot_associated associated, k.hubspot_id,
        c.hubspot_id hubspot_company_id, k.hubspot_synced_at, k.hubspot_error FROM contacts k
        JOIN companies c ON c.key=k.company_key ORDER BY c.name, k.id""")
    for r in rows:
        r["status"] = badge(r["status"])
        r["associated"] = "✅" if r["associated"] else "—"
        r["link"] = hs_link("contact", r["hubspot_id"])
    table(rows, column_config={"link": st.column_config.LinkColumn("Open in HubSpot")})

# ------------------------------------------------------------------ F. execution history
@st.fragment(run_every=OVERVIEW_REFRESH)
def execution_history():
    history = db.query("SELECT * FROM processing_runs ORDER BY id DESC LIMIT 50")
    usage = {r["run_id"]: r for r in db.query(
        "SELECT run_id, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) c"
        " FROM capability_analysis GROUP BY run_id")}
    out = []
    for r in history:
        s = db.loads(r["summary_json"], {}) or {}
        u = usage.get(r["id"], {})
        tokens_in = s.get("ai_input_tokens", u.get("i") or 0)
        tokens_out = s.get("ai_output_tokens", u.get("o") or 0)
        out.append({
            "run": r["id"], "requested / started": fmt_time(r.get("requested_at")
                                                            or r["started_at"]),
            "duration": fmt_age(elapsed(r["started_at"], r["finished_at"]))
            if r["finished_at"] else "—",
            "trigger": r.get("trigger_source") or "cli/legacy",
            "jobs processed": s.get("jobs_retrieved"), "new jobs": s.get("jobs_new"),
            "companies": s.get("companies"),
            "contacts": s.get("apollo_contacts"),
            "openai requests": s.get("ai_requests", u.get("n") or 0),
            "openai tokens (in/out)": f"{tokens_in}/{tokens_out}",
            "est. cost $": s.get("ai_cost_usd"),
            "apollo": badge(integration_status(s, "apollo")),
            "hubspot": badge(integration_status(s, "hubspot")),
            "overall": badge(r["status"]),
            "errors": len(s.get("errors", [])),
        })
    table(out)
    st.caption("Estimated cost uses the configured OpenAI prices (.env). Token counts for runs "
               "before usage tracking are derived from stored analyses.")


with tabs[7]:
    execution_history()
    history = db.query("SELECT id FROM processing_runs ORDER BY id DESC LIMIT 50")
    st.subheader("Integration log")
    c1, c2 = st.columns(2)
    only_errors = c1.checkbox("Errors only", value=False)
    run_filter = c2.selectbox("Run", ["all"] + [r["id"] for r in history])
    sql = ("SELECT created_at, run_id, provider, operation, method, endpoint, attempt,"
           " http_status, success, duration_ms, entity, external_id, error FROM integration_logs")
    where, params = [], []
    if only_errors:
        where.append("success = 0")
    if run_filter != "all":
        where.append("run_id = ?")
        params.append(run_filter)
    if where:
        sql += " WHERE " + " AND ".join(where)
    table(db.query(sql + " ORDER BY id DESC LIMIT 500", params))

    st.subheader("Destination verification (read-only)")
    if READ_ONLY:
        st.caption("Available to operators on the host: docker compose exec bd-live-worker "
                   "python -c \"from bdlive.verify import verify_all; print(verify_all())\"")
    elif st.button("Read records back from Apollo and HubSpot APIs"):
        from bdlive.verify import verify_all

        with st.spinner("Querying Apollo and HubSpot…"):
            try:
                result = json.loads(json.dumps(verify_all(), default=str))
                totals = result["totals"]
                v = st.columns(4)
                v[0].metric("Apollo accounts (API)", totals["apollo_accounts_total"])
                v[1].metric("Apollo contacts (API)", totals["apollo_contacts_total"])
                v[2].metric("HubSpot companies (portal)", totals["hubspot_companies_total"])
                v[3].metric("HubSpot contacts (portal)", totals["hubspot_contacts_total"])
                if totals["apollo_duplicate_account_domains"]:
                    st.error(f"Duplicate Apollo accounts: "
                             f"{totals['apollo_duplicate_account_domains']}")
                if result["issues"]:
                    st.warning("Incomplete or inconsistent records found:")
                    table([{"company": k, "issue": i} for k, v in result["issues"].items()
                           for i in v])
                else:
                    st.success("All records verified in Apollo and HubSpot.")
                with st.expander("Full read-back"):
                    st.json(result)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Verification failed: {type(exc).__name__}")
