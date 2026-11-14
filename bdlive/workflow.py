"""End-to-end automated BD workflow.

TheirStack -> normalize/dedupe -> OpenAI capability analysis -> scoring ->
Apollo (account + contacts) -> HubSpot (company + contacts + associations) -> SQLite.

No manual approvals between stages. Each integration is isolated: an Apollo
failure does not stop HubSpot, a HubSpot failure does not undo Apollo, and every
failure is recorded on the entity row and in integration_logs.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import date

from bdlive import db
from bdlive.analysis import OpenAIAnalyzer
from bdlive.apollo import DECISION_SENIORITIES, ApolloClient, configured_titles, pick_people
from bdlive.http import ApiError, log_event, scrub
from bdlive.hubspot import COMPANY_READ, CONTACT_READ, HubSpotClient
from bdlive.hubspot_schema import ensure_properties
from bdlive.scoring import score_company
from bdlive.settings import settings
from bdlive.theirstack import TheirStackClient, normalize_domain, normalize_job, run_config

log = logging.getLogger("bdlive")
Progress = Callable[[str, float], None]


def _noop(message: str, fraction: float) -> None:
    log.info("[%3d%%] %s", int(fraction * 100), message)


def _err(exc: Exception) -> str:
    return scrub(str(exc) if isinstance(exc, ApiError) else f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- storage
def store_job(job: dict, run_id: int) -> bool:
    """Insert or refresh a job + its company. Returns True when the job is new."""
    c = job["company"]
    with db.tx() as conn:
        existing = conn.execute("SELECT id FROM jobs WHERE id = ?", (job["id"],)).fetchone()
        if existing:
            conn.execute("UPDATE jobs SET last_run_id = ?, retrieved_at = ? WHERE id = ?",
                         (run_id, db.now(), job["id"]))
        else:
            conn.execute(
                "INSERT INTO jobs (id, theirstack_id, first_run_id, last_run_id, title,"
                " company_name, company_domain, company_key, location, country, source_url,"
                " posted_at, seniority, employment_type, remote, technologies_json, description,"
                " retrieved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job["id"], job["theirstack_id"], run_id, run_id, job["title"],
                 job["company_name"], job["company_domain"], job["company_key"], job["location"],
                 job["country"], job["source_url"], job["posted_at"], job["seniority"],
                 job["employment_type"], job["remote"], db.dumps(job["technologies"]),
                 job["description"], db.now()),
            )
        conn.execute(
            "INSERT INTO companies (key, name, domain, country, industry, employees, linkedin_url,"
            " theirstack_company_id, last_run_id, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET name=excluded.name,"
            " country=COALESCE(excluded.country, companies.country),"
            " industry=COALESCE(excluded.industry, companies.industry),"
            " employees=COALESCE(excluded.employees, companies.employees),"
            " linkedin_url=COALESCE(excluded.linkedin_url, companies.linkedin_url),"
            " last_run_id=excluded.last_run_id, updated_at=excluded.updated_at",
            (c["key"], c["name"], c["domain"], c["country"], c["industry"], c["employees"],
             c["linkedin_url"], c["theirstack_company_id"], run_id, db.now()),
        )
    return not existing


def store_analysis(job_id: str, run_id: int, result: dict | None, error: str | None) -> None:
    with db.tx() as conn:
        if result is None:
            conn.execute(
                "INSERT INTO capability_analysis (job_id, run_id, status, error, analysed_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET run_id=excluded.run_id,"
                " status=excluded.status, error=excluded.error, analysed_at=excluded.analysed_at",
                (job_id, run_id, "failed", error, db.now()),
            )
            conn.execute("UPDATE jobs SET analysis_status='failed' WHERE id=?", (job_id,))
            return
        conn.execute(
            "INSERT OR REPLACE INTO capability_analysis (job_id, run_id, status, model,"
            " prompt_version, summary, seniority, technologies_json, skills_json,"
            " responsibilities_json, matches_json, rejected_matches_json, personas_json,"
            " caveats_json, confidence, capability_score, input_tokens, output_tokens, cost_usd,"
            " error, analysed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, run_id, "success", result["model"], result["prompt_version"],
             result["summary"], result["seniority"], db.dumps(result["technologies"]),
             db.dumps(result["skills"]), db.dumps(result["responsibilities"]),
             db.dumps(result["matches"]), db.dumps(result["rejected_matches"]),
             db.dumps(result["personas"]), db.dumps(result["caveats"]), result["confidence"],
             result["capability_score"], result["input_tokens"], result["output_tokens"],
             result["cost_usd"], None, db.now()),
        )
        conn.execute("UPDATE jobs SET analysis_status='success' WHERE id=?", (job_id,))


def analyses_for(job_ids: list[str]) -> dict[str, dict]:
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = db.query(f"SELECT * FROM capability_analysis WHERE status='success' AND job_id IN "
                    f"({marks})", job_ids)
    return {r["job_id"]: {**r, "matches": db.loads(r["matches_json"], []),
                          "personas": db.loads(r["personas_json"], [])} for r in rows}


def rescore_company(key: str, run_id: int) -> dict | None:
    company = db.one("SELECT * FROM companies WHERE key = ?", (key,))
    jobs = db.query("SELECT * FROM jobs WHERE company_key = ?", (key,))
    analyses = analyses_for([j["id"] for j in jobs])
    if not analyses:
        return None
    score = score_company(company, jobs, analyses, date.today())
    with db.tx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scoring_results (company_key, run_id, total, band,"
            " components_json, rationale_json, matched_capabilities_json, relevant_job_ids_json,"
            " rules_version, scored_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, run_id, score["total"], score["band"], db.dumps(score["components"]),
             db.dumps(score["rationale"]), db.dumps(score["matched_capabilities"]),
             db.dumps(score["relevant_job_ids"]), score["rules_version"], db.now()),
        )
    return score


# --------------------------------------------------------------------------- CRM content
def company_context(key: str) -> dict:
    company = db.one("SELECT * FROM companies WHERE key = ?", (key,))
    score = db.one("SELECT * FROM scoring_results WHERE company_key = ?", (key,)) or {}
    jobs = db.query("SELECT * FROM jobs WHERE company_key = ? ORDER BY posted_at DESC", (key,))
    analyses = analyses_for([j["id"] for j in jobs])
    caps: dict[str, dict] = {}
    personas: list[str] = []
    summaries = []
    for job in jobs:
        a = analyses.get(job["id"])
        if not a:
            continue
        summaries.append(f"{job['title']}: {a.get('summary') or ''}")
        for p in a["personas"]:
            if p not in personas:
                personas.append(p)
        for m in a["matches"]:
            cur = caps.get(m["capability_id"])
            if cur is None or m["confidence"] > cur["confidence"]:
                caps[m["capability_id"]] = m
    ranked = sorted(caps.values(), key=lambda m: -m["confidence"])
    capability_text = "; ".join(
        f"{m.get('capability_name', m['capability_id'])} ({m['confidence']:.2f})" for m in ranked
    )
    hiring = "\n".join(
        f"- {j['title']} | {j['location']} | posted {j['posted_at']} | {j['source_url'] or ''}"
        for j in jobs[:10]
    )
    recommendation = (
        f"Celstream opportunity score {score.get('total', 0)} ({score.get('band', 'n/a')}).\n"
        f"Matched capabilities: {capability_text or 'none'}.\n"
        f"Suggested personas: {', '.join(personas[:6]) or 'n/a'}.\n"
        + "\n".join(summaries[:5])
        + "\nHiring signals indicate service relevance; outsourcing intent is unverified."
    )
    capability_scores = [a["capability_score"] for a in analyses.values()
                         if a.get("capability_score") is not None]
    job_refs = "\n".join(f"{j['id']} | {j['source_url'] or ''}" for j in jobs[:20])
    return {"company": company, "score": score, "jobs": jobs, "capabilities": ranked,
            "capability_text": capability_text, "hiring_text": hiring,
            "capability_score": max(capability_scores) if capability_scores else None,
            "job_references": job_refs,
            "recommendation": recommendation[:5000]}


SOURCE_SYSTEM = "celstream_bd_live_poc"
LEAD_SOURCE = "TheirStack hiring signal"


# --------------------------------------------------------------------------- Apollo
def sync_apollo(key: str, run_id: int, apollo: ApolloClient, cfg: dict) -> dict:
    ctx = company_context(key)
    company, score = ctx["company"], ctx["score"]
    domain = company["domain"]
    prefix = cfg["apollo"].get("label_prefix", "Celstream BD")
    labels = [prefix, f"{prefix} Priority: {score.get('band', 'n/a')}"]
    contacts_done, contact_errors = 0, []
    try:
        org = apollo.enrich_organization(domain) or {}
        account_id = company["apollo_account_id"]
        if not account_id:
            found = apollo.find_account(company["name"], domain)
            account_id = found.get("id") if found else None
        fields = {"name": org.get("name") or company["name"], "domain": domain,
                  "label_names": labels}
        if account_id:
            apollo.update_account(account_id, {"label_names": labels})
            action = "updated"
        else:
            account = apollo.create_account(fields)
            account_id = account.get("id")
            action = "created"
            if not account_id:
                raise ApiError("apollo", "invalid_response", "account id missing in response")
            # Apollo only applies account labels on update, not on create.
            apollo.update_account(account_id, {"label_names": labels})
        log_event("apollo", f"account_{action}", run_id=run_id, entity=domain,
                  external_id=account_id)
        db.update("companies", "key", key, apollo_org_id=org.get("id"),
                  apollo_account_id=account_id, apollo_status="success", apollo_error=None,
                  apollo_synced_at=db.now(),
                  employees=company["employees"] or org.get("estimated_num_employees"))
    except Exception as exc:  # noqa: BLE001
        db.update("companies", "key", key, apollo_status="failed", apollo_error=_err(exc),
                  apollo_synced_at=db.now())
        return {"account": "failed", "contacts": 0, "error": _err(exc)}

    # People discovery: configured titles first, then Apollo's similar-title expansion.
    limit = int(cfg["apollo"].get("contacts_per_company", 2))
    existing = db.query("SELECT * FROM contacts WHERE company_key = ? AND apollo_contact_id IS"
                        " NOT NULL AND apollo_status != 'rejected'", (key,))
    updated = 0
    for row in existing:  # refresh labels / account link on already-created contacts
        try:
            apollo.update_contact(row["apollo_contact_id"], {"label_names": labels,
                                                             "account_id": account_id})
            db.update("contacts", "id", row["id"], apollo_status="success", apollo_error=None,
                      apollo_synced_at=db.now(), last_run_id=run_id, updated_at=db.now())
            updated += 1
        except Exception as exc:  # noqa: BLE001
            contact_errors.append(_err(exc))
            db.update("contacts", "id", row["id"], apollo_status="failed",
                      apollo_error=_err(exc), apollo_synced_at=db.now())
    if len(existing) >= limit:
        return {"account": "success", "contacts": len(existing), "created": 0,
                "updated": updated, "error": "; ".join(contact_errors) or None}
    try:
        people = apollo.search_people(domain, configured_titles(), similar=False)
        if len(pick_people(people, limit)) < limit:
            people += apollo.search_people(domain, configured_titles(), similar=True)
        if len(pick_people(people, limit)) < limit:
            # Smaller companies: fall back to senior decision-makers (founder/CEO/CIO/COO).
            people += apollo.search_people(domain, [], similar=False, per_page=25,
                                           seniorities=DECISION_SENIORITIES)
        picked = pick_people(people, limit)
    except Exception as exc:  # noqa: BLE001
        db.update("companies", "key", key, apollo_status="partial",
                  apollo_error="people search: " + _err(exc))
        return {"account": "success", "contacts": 0, "error": _err(exc)}
    # Rejected people never re-enter selection, so a proper replacement can take the slot.
    rejected_ids = {r["apollo_person_id"] for r in db.query(
        "SELECT apollo_person_id FROM contacts WHERE company_key = ? AND apollo_status ="
        " 'rejected'", (key,))}
    people = [p for p in people if p.get("id") not in rejected_ids]
    picked = pick_people(people, limit + len(existing))
    if not picked and not existing:
        message = "no matching decision-maker roles found in Apollo"
        db.update("companies", "key", key, apollo_status="partial", apollo_error=message)
        return {"account": "success", "contacts": 0, "created": 0, "updated": 0,
                "error": message}

    for person, role in picked:
        person_id = person.get("id")
        row = db.one("SELECT * FROM contacts WHERE apollo_person_id = ?", (person_id,))
        if row and row["apollo_contact_id"]:
            continue
        if len(existing) + contacts_done >= limit:
            break
        with db.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO contacts (company_key, apollo_person_id, first_name, title,"
                " role_category, last_run_id, updated_at) VALUES (?,?,?,?,?,?,?)",
                (key, person_id, person.get("first_name"), person.get("title"), role, run_id,
                 db.now()),
            )
        try:
            enriched = person
            if cfg["apollo"].get("enrich_contacts", True):
                enriched = apollo.enrich_person(person_id) or person
            email = (enriched.get("email") or "").lower() or None
            if email and not email.endswith("@" + domain) and normalize_domain(
                email.split("@")[-1]
            ) != domain:
                email = None  # professional address at the target company only
            if email and "email_not_unlocked" in email:
                email = None
            first = enriched.get("first_name") or person.get("first_name")
            last = enriched.get("last_name") or person.get("last_name_obfuscated")
            linkedin = enriched.get("linkedin_url")
            fields = {"first_name": first, "last_name": last,
                      "title": enriched.get("title") or person.get("title"),
                      "organization_name": company["name"], "account_id": account_id,
                      "website_url": f"https://{domain}", "label_names": labels}
            if email:
                fields["email"] = email
            if linkedin:
                fields["linkedin_url"] = linkedin
            existing_contact_id = (enriched.get("contact") or {}).get("id") or enriched.get(
                "contact_id")
            if existing_contact_id:
                # Person is already a contact in Apollo: update instead of duplicating.
                contact = apollo.update_contact(existing_contact_id, fields)
                contact_id = contact.get("id") or existing_contact_id
            else:
                contact = apollo.create_contact(fields)
                contact_id = contact.get("id")
            if not contact_id:
                raise ApiError("apollo", "invalid_response", "contact id missing in response")
            linkedin = linkedin or contact.get("linkedin_url")
            log_event("apollo", "contact_created", run_id=run_id, entity=f"{first} {last}",
                      external_id=contact_id)
            db.update("contacts", "apollo_person_id", person_id, apollo_contact_id=contact_id,
                      first_name=first, last_name=last, title=fields["title"], email=email,
                      email_status=enriched.get("email_status"), linkedin_url=linkedin,
                      apollo_status="success", apollo_error=None, apollo_synced_at=db.now(),
                      last_run_id=run_id, updated_at=db.now())
            contacts_done += 1
        except Exception as exc:  # noqa: BLE001
            contact_errors.append(_err(exc))
            db.update("contacts", "apollo_person_id", person_id, apollo_status="failed",
                      apollo_error=_err(exc), apollo_synced_at=db.now(), updated_at=db.now())
    total = len(existing) + contacts_done
    if contact_errors:
        db.update("companies", "key", key, apollo_status="partial",
                  apollo_error="; ".join(contact_errors)[:600])
    elif total == 0:
        contact_errors.append("no Apollo contacts could be created")
        db.update("companies", "key", key, apollo_status="partial",
                  apollo_error=contact_errors[0])
    return {"account": "success", "contacts": total, "created": contacts_done,
            "updated": updated, "error": "; ".join(contact_errors) or None}


# --------------------------------------------------------------------------- HubSpot
def sync_hubspot(key: str, run_id: int, hubspot: HubSpotClient) -> dict:
    ctx = company_context(key)
    company, score = ctx["company"], ctx["score"]
    domain = company["domain"]
    try:
        fill = {"name": company["name"], "domain": domain, "website": f"https://{domain}",
                "numberofemployees": company["employees"], "country": company["country"],
                "linkedin_company_page": company["linkedin_url"]}
        owned = {
            "celstream_opportunity_score": score.get("total"),
            "celstream_opportunity_priority": score.get("band"),
            "celstream_service_interest": len(ctx["capabilities"]),
            "celstream_hiring_signals": ctx["hiring_text"],
            "celstream_ai_recommendation": ctx["recommendation"],
            "celstream_capability_score": ctx["capability_score"],
            "celstream_matched_capability_count": len(ctx["capabilities"]),
            "celstream_matched_capabilities": ctx["capability_text"],
            "celstream_job_references": ctx["job_references"],
            "celstream_lead_source": LEAD_SOURCE,
            "celstream_source_system": SOURCE_SYSTEM,
            "celstream_last_discovery_date": date.today().isoformat(),
        }
        company_id, action = hubspot.upsert(
            "companies", [("domain", domain)], fill, owned, COMPANY_READ
        )
        log_event("hubspot", f"company_{action}", run_id=run_id, entity=domain,
                  external_id=company_id)
        db.update("companies", "key", key, hubspot_id=company_id, hubspot_status="success",
                  hubspot_error=None, hubspot_synced_at=db.now())
        company_action = action
    except Exception as exc:  # noqa: BLE001
        db.update("companies", "key", key, hubspot_status="failed", hubspot_error=_err(exc),
                  hubspot_synced_at=db.now())
        return {"company": "failed", "contacts": 0, "error": _err(exc)}

    errors, done, created = [], 0, 0
    contacts = db.query("SELECT * FROM contacts WHERE company_key = ? AND first_name IS NOT NULL"
                        " AND apollo_status != 'rejected'", (key,))
    for contact in contacts:
        try:
            fill = {"firstname": contact["first_name"], "lastname": contact["last_name"],
                    "email": contact["email"], "jobtitle": contact["title"],
                    "company": company["name"], "hs_linkedin_url": contact["linkedin_url"],
                    "website": f"https://{domain}"}
            owned = {"celstream_apollo_id": contact["apollo_person_id"],
                     "celstream_linkedin_url": contact["linkedin_url"],
                     "celstream_source_system": SOURCE_SYSTEM,
                     "celstream_last_discovery_date": date.today().isoformat()}
            match = [("email", contact["email"]), ("celstream_apollo_id",
                                                   contact["apollo_person_id"])]
            if contact["hubspot_id"]:
                hubspot.patch("contacts", contact["hubspot_id"],
                              hubspot.supported("contacts", owned))
                contact_id, action = contact["hubspot_id"], "updated"
            else:
                contact_id, action = hubspot.upsert("contacts", match, fill, owned, CONTACT_READ)
            hubspot.associate(company_id, contact_id)
            created += action == "created"
            log_event("hubspot", f"contact_{action}+associated", run_id=run_id,
                      entity=f"{contact['first_name']} {contact['last_name']}",
                      external_id=contact_id)
            db.update("contacts", "id", contact["id"], hubspot_id=contact_id,
                      hubspot_status="success", hubspot_error=None, hubspot_associated=1,
                      hubspot_synced_at=db.now(), updated_at=db.now())
            done += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(_err(exc))
            db.update("contacts", "id", contact["id"], hubspot_status="failed",
                      hubspot_error=_err(exc), hubspot_synced_at=db.now())
    if errors:
        db.update("companies", "key", key, hubspot_status="partial",
                  hubspot_error="; ".join(errors)[:600])
    elif not contacts:
        errors.append("company synced but no decision-maker contacts available to associate")
        db.update("companies", "key", key, hubspot_status="partial", hubspot_error=errors[0])
    return {"company": "success", "company_action": company_action, "contacts": done,
            "created": created, "updated": done - created, "error": "; ".join(errors) or None}


# --------------------------------------------------------------------------- orchestrator
def run_workflow(max_jobs: int | None = None, progress: Progress = _noop,
                 sync_existing: bool = False, reprocess_existing: bool = False,
                 run_id: int | None = None, job_ids: list[str] | None = None,
                 reanalyse: bool = False) -> dict:
    """reprocess_existing=True re-fetches exactly the already-stored jobs from TheirStack
    (job_id_or; optionally only `job_ids`) and re-runs every downstream stage; used to prove
    idempotency without creating CRM records. reanalyse=True also re-sends those jobs to
    OpenAI. run_id is set when the worker has already claimed a queued run."""
    db.init()
    cfg = run_config()
    s = settings()
    max_jobs = min(int(max_jobs or cfg["default_max_jobs"]), int(cfg["max_jobs_limit"]))
    params = {"max_jobs": max_jobs, "sync_existing": sync_existing,
              "reprocess_existing": reprocess_existing, "job_ids": job_ids or [],
              "reanalyse": reanalyse}
    if run_id is not None:
        db.update("processing_runs", "id", run_id, params_json=db.dumps(params),
                  stage="starting")
    else:
        with db.tx() as conn:
            # Direct (CLI) runs share the single-run lock with queued/worker runs.
            conn.execute("UPDATE processing_runs SET status='failed', finished_at=?, error="
                         "'abandoned (process stopped before completion)' WHERE status='running'"
                         " AND COALESCE(heartbeat_at, started_at) <"
                         " strftime('%Y-%m-%dT%H:%M:%S','now','-30 minutes')", (db.now(),))
            active = conn.execute("SELECT id, status FROM processing_runs WHERE status IN"
                                  " ('queued','running')").fetchone()
            if active:
                return {"run_id": active["id"], "status": "already_running",
                        "errors": [f"run {active['id']} is already {active['status']}"]}
            run_id = conn.execute(
                "INSERT INTO processing_runs (started_at, requested_at, status, stage,"
                " params_json, trigger_source) VALUES (?,?,?,?,?,?)",
                (db.now(), db.now(), "running", "starting", db.dumps(params), "cli"),
            ).lastrowid
    summary = {"run_id": run_id, "max_jobs": max_jobs, "jobs_retrieved": 0, "jobs_new": 0,
               "jobs_analysed": 0, "analysis_failed": 0, "companies": 0, "companies_scored": 0,
               "apollo_accounts": 0, "apollo_contacts": 0, "apollo_contacts_created": 0,
               "apollo_contacts_updated": 0, "apollo_failed": 0,
               "hubspot_companies": 0, "hubspot_companies_created": 0,
               "hubspot_companies_updated": 0, "hubspot_contacts": 0,
               "hubspot_contacts_created": 0, "hubspot_contacts_updated": 0,
               "hubspot_failed": 0, "companies_partial": [],
               "skipped_no_domain": 0, "ai_cost_usd": 0.0, "ai_requests": 0,
               "ai_input_tokens": 0, "ai_output_tokens": 0, "errors": []}

    def stage(name: str, fraction: float):
        db.update("processing_runs", "id", run_id, stage=name, progress=round(fraction, 3),
                  summary_json=db.dumps(summary))
        progress(name, fraction)

    def finish(status: str, error: str | None = None):
        db.update("processing_runs", "id", run_id, status=status, finished_at=db.now(),
                  summary_json=db.dumps(summary), error=error, stage="finished", progress=1.0)
        summary["status"] = status
        return summary

    missing = s.missing_credentials()
    if missing:
        summary["errors"].append(f"missing configuration: {', '.join(missing)}")
        return finish("failed", summary["errors"][-1])

    clients = []
    try:
        # STEP 1-3: retrieve, normalize, dedupe, extract companies
        stage("1/6 Retrieving live jobs from TheirStack", 0.05)
        theirstack = TheirStackClient(run_id)
        clients.append(theirstack)
        extra = dict(cfg.get("theirstack_extra_filters") or {})
        known = [int(r["theirstack_id"]) for r in db.query(
            "SELECT theirstack_id FROM jobs ORDER BY retrieved_at DESC LIMIT 500"
        ) if r["theirstack_id"].isdigit()]
        required = {}
        if reprocess_existing:
            if not known:
                summary["errors"].append("no stored jobs to reprocess")
                return finish("failed", summary["errors"][-1])
            if job_ids:
                wanted = {j.removeprefix("theirstack:") for j in job_ids}
                known = [k for k in known if str(k) in wanted]
                if not known:
                    summary["errors"].append("none of the requested job IDs are stored")
                    return finish("failed", summary["errors"][-1])
            known = known[: int(cfg["max_jobs_limit"])]
            required["job_id_or"] = known
            extra = {}  # exact IDs: the stored jobs already passed the original filters
            max_jobs = summary["max_jobs"] = len(known)
            required["posted_at_max_age_days"] = 365
        elif known:
            required["job_id_not"] = known  # only new postings consume TheirStack credits
        try:
            raw_jobs = theirstack.search_jobs(max_jobs, extra, int(cfg["theirstack_page_size"]),
                                              required)
        except ApiError as exc:
            summary["errors"].append(f"TheirStack: {exc}")
            return finish("failed", str(exc))
        stage("2/6 Normalizing and de-duplicating jobs", 0.15)
        seen, jobs = set(), []
        for raw in raw_jobs:
            job = normalize_job(raw)
            if not job or job["id"] in seen:
                continue
            seen.add(job["id"])
            jobs.append(job)
            if store_job(job, run_id):
                summary["jobs_new"] += 1
        summary["jobs_retrieved"] = len(jobs)
        company_keys = list(dict.fromkeys(j["company_key"] for j in jobs))
        if sync_existing:
            # Retry incomplete companies: failed/partial syncs or fewer valid contacts than
            # the configured target (e.g. after a contact was rejected on review).
            company_keys += [r["key"] for r in db.query(
                "SELECT key FROM companies c WHERE apollo_status NOT IN ('success','skipped',"
                "'rejected') OR hubspot_status NOT IN ('success','skipped','rejected')"
                " OR (apollo_status = 'success'"
                " AND (SELECT COUNT(*) FROM contacts k WHERE k.company_key = c.key AND"
                " k.apollo_contact_id IS NOT NULL AND k.apollo_status != 'rejected') < ?)",
                (int(cfg["apollo"].get("contacts_per_company", 2)),))
                if r["key"] not in company_keys]
        summary["companies"] = len(company_keys)
        if not jobs and not company_keys:
            summary["errors"].append("TheirStack returned no new jobs for the criteria")
            return finish("failed", summary["errors"][-1])

        # STEP 4-6: OpenAI analysis + scoring
        analyzer = OpenAIAnalyzer(run_id)
        pending = [j for j in jobs if reanalyse or (db.one(
            "SELECT status FROM capability_analysis WHERE job_id=?", (j["id"],)) or {}
        ).get("status") != "success"]
        for i, job in enumerate(pending, 1):
            stage(f"3/6 OpenAI analysis {i}/{len(pending)}: {job['title']} @ "
                  f"{job['company_name']}", 0.15 + 0.35 * (i - 1) / max(len(pending), 1))
            try:
                result = analyzer.analyse(job)
                store_analysis(job["id"], run_id, result, None)
                summary["jobs_analysed"] += 1
            except Exception as exc:  # noqa: BLE001
                store_analysis(job["id"], run_id, None, _err(exc))
                summary["analysis_failed"] += 1
                summary["errors"].append(f"OpenAI {job['id']}: {_err(exc)}")
        summary["ai_cost_usd"] = round(analyzer.spent_usd, 6)
        summary["ai_requests"] = analyzer.requests
        summary["ai_input_tokens"] = analyzer.input_tokens
        summary["ai_output_tokens"] = analyzer.output_tokens

        stage("4/6 Scoring company opportunities", 0.5)
        sync_keys = []
        for key in company_keys:
            score = rescore_company(key, run_id)
            if not score:
                continue
            summary["companies_scored"] += 1
            company = db.one("SELECT domain, apollo_status FROM companies WHERE key = ?",
                             (key,))
            if company["apollo_status"] == "rejected":
                continue  # rejected on review (e.g. identity mismatch): never re-synced
            if not company["domain"]:
                summary["skipped_no_domain"] += 1
                db.update("companies", "key", key, apollo_status="skipped",
                          hubspot_status="skipped", apollo_error="no company domain",
                          hubspot_error="no company domain")
                continue
            if not score["matched_capabilities"]:
                db.update("companies", "key", key, apollo_status="skipped",
                          hubspot_status="skipped",
                          apollo_error="no evidenced Celstream capability match",
                          hubspot_error="no evidenced Celstream capability match")
                continue
            sync_keys.append(key)

        # STEP 7-10: Apollo then HubSpot, isolated per company and per integration
        apollo, hubspot = ApolloClient(run_id), HubSpotClient(run_id)
        clients += [apollo, hubspot]
        if sync_keys:
            try:  # idempotent: only creates celstream_* properties that are missing
                schema = ensure_properties(hubspot)
                for failure in schema["failed"]:
                    summary["errors"].append(f"HubSpot property: {failure}")
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"HubSpot property check: {_err(exc)}")
        for i, key in enumerate(sync_keys, 1):
            base = 0.55 + 0.4 * (i - 1) / max(len(sync_keys), 1)
            stage(f"5/6 Apollo: {key} ({i}/{len(sync_keys)})", base)
            try:
                result = sync_apollo(key, run_id, apollo, cfg)
            except Exception as exc:  # noqa: BLE001
                result = {"account": "failed", "contacts": 0, "error": _err(exc)}
            if result["account"] == "success":
                summary["apollo_accounts"] += 1
            else:
                summary["apollo_failed"] += 1
            summary["apollo_contacts"] += result["contacts"]
            summary["apollo_contacts_created"] += result.get("created", 0)
            summary["apollo_contacts_updated"] += result.get("updated", 0)
            if result.get("error"):
                summary["errors"].append(f"Apollo {key}: {result['error']}")

            stage(f"6/6 HubSpot: {key} ({i}/{len(sync_keys)})", base + 0.2 / max(len(sync_keys), 1))
            try:
                result = sync_hubspot(key, run_id, hubspot)
            except Exception as exc:  # noqa: BLE001
                result = {"company": "failed", "contacts": 0, "error": _err(exc)}
            if result["company"] == "success":
                summary["hubspot_companies"] += 1
            else:
                summary["hubspot_failed"] += 1
            summary["hubspot_contacts"] += result["contacts"]
            summary["hubspot_contacts_created"] += result.get("created", 0)
            summary["hubspot_contacts_updated"] += result.get("updated", 0)
            if result.get("company_action") == "created":
                summary["hubspot_companies_created"] += 1
            elif result.get("company_action") in ("updated", "unchanged"):
                summary["hubspot_companies_updated"] += 1
            if result.get("error"):
                summary["errors"].append(f"HubSpot {key}: {result['error']}")

        stage("Finished", 1.0)
        for key in sync_keys:
            row = db.one("SELECT apollo_status, hubspot_status FROM companies WHERE key = ?",
                         (key,))
            if row["apollo_status"] != "success" or row["hubspot_status"] != "success":
                summary["companies_partial"].append(key)
        failures = (summary["analysis_failed"] + summary["apollo_failed"]
                    + summary["hubspot_failed"] + len(summary["errors"])
                    + len(summary["companies_partial"]))
        if not sync_keys:
            status = "partial" if summary["jobs_analysed"] else "failed"
        elif summary["apollo_failed"] == len(sync_keys) and summary["hubspot_failed"] == len(
                sync_keys):
            status = "failed"
        else:
            # "success" only when every stage, company and contact sync completed.
            status = "partial" if failures else "success"
        return finish(status, "; ".join(summary["errors"])[:2000] or None)
    except Exception as exc:  # noqa: BLE001
        log.error("workflow crashed: %s", traceback.format_exc())
        summary["errors"].append(f"workflow error: {_err(exc)}")
        return finish("failed", summary["errors"][-1])
    finally:
        for client in clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
