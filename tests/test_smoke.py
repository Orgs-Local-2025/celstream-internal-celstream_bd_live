"""Targeted smoke tests: configuration, orchestration, duplicate prevention and partial
failure handling. External providers are replaced by small in-memory fakes; the real
workflow, scoring, DB and HubSpot upsert logic run unchanged.

Live behaviour is proven separately by run_workflow.py + bdlive.verify against real APIs.
"""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

from bdlive import db, workflow
from bdlive.apollo import pick_people, role_for
from bdlive.http import ApiError
from bdlive.hubspot import COMPANY_READ, CONTACT_READ, HubSpotClient
from bdlive.settings import analysis_prompt, apollo_config, catalogue, scoring_rules
from bdlive.theirstack import normalize_job, run_config

TODAY = "2026-11-14"
DESCRIPTION = ("Acme builds cloud SaaS products. Design and implement Python microservices on "
               "AWS. Contact jobs@acme.com.")


def raw_job(job_id: int, domain: str, name: str) -> dict:
    return {
        "id": job_id, "job_title": "Backend Engineer", "company": name,
        "company_domain": domain, "description": DESCRIPTION, "date_posted": TODAY,
        "country": "United States", "location": "Remote",
        "company_object": {"id": f"c-{domain}", "name": name, "domain": domain,
                           "employee_count": 200, "country": "United States"},
    }


# ----------------------------------------------------------------------------- fakes
class FakeTheirStack:
    jobs: list[dict] = []
    calls: list[dict] = []

    def __init__(self, run_id=None):
        pass

    def search_jobs(self, max_jobs, extra=None, page_size=10, required=None):
        FakeTheirStack.calls.append(dict(required or {}))
        required = required or {}
        jobs = FakeTheirStack.jobs
        if "job_id_or" in required:
            jobs = [j for j in jobs if j["id"] in required["job_id_or"]]
        if "job_id_not" in required:
            jobs = [j for j in jobs if j["id"] not in required["job_id_not"]]
        return jobs[:max_jobs]

    def close(self):
        pass


class FakeAnalyzer:
    calls = 0

    def __init__(self, run_id=None):
        self.spent_usd = 0.0
        self.requests = self.input_tokens = self.output_tokens = 0

    def analyse(self, job):
        FakeAnalyzer.calls += 1
        return {
            "model": "fake", "prompt_version": "test", "summary": "Cloud backend role.",
            "seniority": None, "technologies": ["Python", "AWS"], "skills": [],
            "responsibilities": [], "rejected_matches": [], "personas": ["CTO"], "caveats": [],
            "matches": [
                {"capability_id": "software-engineering", "capability_name": "Software",
                 "evidence": "Design and implement Python microservices", "explanation": "x",
                 "confidence": 0.9},
                {"capability_id": "cloud-platforms", "capability_name": "Cloud",
                 "evidence": "on AWS", "explanation": "x", "confidence": 0.8},
                {"capability_id": "web-application-development", "capability_name": "Web",
                 "evidence": "cloud SaaS products", "explanation": "x", "confidence": 0.7},
            ],
            "confidence": 0.9, "capability_score": 80.0, "input_tokens": 1,
            "output_tokens": 1, "cost_usd": 0.0,
        }


class FakeApollo:
    people: dict[str, list[dict]] = {}
    fail_accounts = False
    ids = itertools.count(1)

    def __init__(self, run_id=None):
        self.accounts: dict[str, dict] = FakeApollo.store["accounts"]
        self.contacts: dict[str, dict] = FakeApollo.store["contacts"]

    def enrich_organization(self, domain):
        return {"id": f"org-{domain}", "name": domain}

    def find_account(self, name, domain):
        return next((a for a in self.accounts.values() if a["domain"] == domain), None)

    def create_account(self, fields):
        if FakeApollo.fail_accounts:
            raise ApiError("apollo", "server_error", "boom", 500)
        account_id = f"acc{next(FakeApollo.ids)}"
        self.accounts[account_id] = {"id": account_id, **fields}
        return self.accounts[account_id]

    def update_account(self, account_id, fields):
        self.accounts[account_id].update(fields)
        return self.accounts[account_id]

    def search_people(self, domain, titles, similar, per_page=10, seniorities=None):
        return list(FakeApollo.people.get(domain, []))

    def enrich_person(self, person_id):
        return {"id": person_id, "first_name": "Pat", "last_name": person_id,
                "email": None, "title": None}

    def create_contact(self, fields):
        contact_id = f"con{next(FakeApollo.ids)}"
        self.contacts[contact_id] = {"id": contact_id, **fields}
        return self.contacts[contact_id]

    def update_contact(self, contact_id, fields):
        self.contacts[contact_id].update(fields)
        return self.contacts[contact_id]

    def close(self):
        pass


class FakeHubSpot(HubSpotClient):
    """Real HubSpotClient upsert/supported logic over an in-memory CRM."""

    def __init__(self, run_id=None):  # no HTTP client
        self.run_id = run_id
        self._properties = {}
        self.store = FakeHubSpot.store

    def close(self):
        pass

    def request(self, method, path, operation, *, params=None, json=None, entity=None,
                retries=None, ok_statuses=()):
        parts = path.strip("/").split("/")
        if path.startswith("/crm/v3/properties/"):
            names = COMPANY_READ if parts[3] == "companies" else CONTACT_READ
            return {"results": [{"name": n} for n in [*names, "website"]]}
        if path.startswith("/crm/v4/objects/companies/"):
            self.store["associations"].add((parts[4], parts[-1]))
            return {}
        kind = parts[3]
        records = self.store[kind]
        if path.endswith("/search"):
            f = json["filterGroups"][0]["filters"][0]
            hits = [r for r in records.values()
                    if str(r["properties"].get(f["propertyName"])) == str(f["value"])]
            return {"total": len(hits), "results": hits}
        if method == "POST":
            object_id = str(len(records) + 1000)
            records[object_id] = {"id": object_id, "properties": dict(json["properties"])}
            return records[object_id]
        if method == "PATCH":
            records[parts[4]]["properties"].update(json["properties"])
            return records[parts[4]]
        raise AssertionError(f"unexpected call {method} {path}")


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "settings", lambda: SimpleNamespace(
        database_path=tmp_path / "test.db"))
    monkeypatch.setattr(workflow, "settings",
                        lambda: SimpleNamespace(missing_credentials=lambda: []))
    for name, fake in [("TheirStackClient", FakeTheirStack), ("OpenAIAnalyzer", FakeAnalyzer),
                       ("ApolloClient", FakeApollo), ("HubSpotClient", FakeHubSpot)]:
        monkeypatch.setattr(workflow, name, fake)
    monkeypatch.setattr(workflow, "ensure_properties", lambda client: {"failed": []})
    FakeTheirStack.jobs, FakeTheirStack.calls = [], []
    FakeAnalyzer.calls = 0
    FakeApollo.people, FakeApollo.fail_accounts = {}, False
    FakeApollo.store = {"accounts": {}, "contacts": {}}
    FakeHubSpot.store = {"companies": {}, "contacts": {}, "associations": set()}
    db.init()
    return SimpleNamespace(apollo=FakeApollo.store, hubspot=FakeHubSpot.store)


def leaders(domain):
    return [{"id": f"{domain}-cto", "first_name": "Ana", "title": "Chief Technology Officer"},
            {"id": f"{domain}-vp", "first_name": "Ben", "title": "VP of Engineering"},
            {"id": f"{domain}-hr", "first_name": "Cy", "title": "Head of Talent Acquisition"}]


# ----------------------------------------------------------------------------- tests
def test_configuration_loads():
    cat = catalogue()
    ids = [c["id"] for c in cat["capabilities"]]
    assert ids and len(ids) == len(set(ids))
    rules = scoring_rules()
    assert sum(rules["weights"].values()) == 100
    assert rules["high_threshold"] > rules["medium_threshold"]
    assert apollo_config()["roles"]
    cfg = run_config()
    assert 1 <= cfg["default_max_jobs"] <= cfg["max_jobs_limit"]
    assert cfg["apollo"]["contacts_per_company"] >= 1
    assert "capability" in analysis_prompt()


def test_normalize_job_scrubs_contact_details_and_keys_by_domain():
    job = normalize_job(raw_job(7, "www.Acme.com", "Acme"))
    assert job["id"] == "theirstack:7" and job["company_key"] == "acme.com"
    assert "jobs@acme.com" not in job["description"]


def test_role_selection_excludes_non_decision_makers():
    assert role_for("Capability Owner | Middleware Platform") is None
    assert role_for("Head of Talent Acquisition") is None
    assert role_for("Founder & CEO") == "executive_founder"
    picked = pick_people(leaders("acme.com"), 5)
    assert [p["title"] for p, _ in picked] == ["Chief Technology Officer", "VP of Engineering"]


def test_workflow_end_to_end_then_rerun_is_idempotent(env):
    FakeTheirStack.jobs = [raw_job(1, "acme.com", "Acme"), raw_job(2, "acme.com", "Acme"),
                           raw_job(3, "globex.com", "Globex")]
    FakeApollo.people = {"acme.com": leaders("acme.com"), "globex.com": leaders("globex.com")}

    first = workflow.run_workflow(max_jobs=3)
    assert first["status"] == "success", first["errors"]
    assert (first["jobs_new"], first["jobs_analysed"], first["companies_scored"]) == (3, 3, 2)
    assert first["apollo_contacts_created"] == 4 and first["hubspot_contacts_created"] == 4
    assert len(env.apollo["accounts"]) == 2 and len(env.apollo["contacts"]) == 4
    assert len(env.hubspot["companies"]) == 2 and len(env.hubspot["contacts"]) == 4
    assert len(env.hubspot["associations"]) == 4
    acme = db.one("SELECT * FROM scoring_results WHERE company_key='acme.com'")
    assert acme["band"] in {"High", "Medium", "Low"} and acme["total"] > 0
    snapshot = (dict(env.apollo["accounts"]), dict(env.apollo["contacts"]),
                set(env.hubspot["companies"]), set(env.hubspot["contacts"]),
                set(env.hubspot["associations"]))

    second = workflow.run_workflow(reprocess_existing=True)
    assert FakeTheirStack.calls[-1]["job_id_or"]  # exact re-fetch of stored jobs
    assert second["status"] == "success", second["errors"]
    assert second["jobs_retrieved"] == 3 and second["jobs_new"] == 0
    assert FakeAnalyzer.calls == 3  # no re-analysis of already analysed jobs
    assert second["apollo_contacts_created"] == 0 and second["hubspot_contacts_created"] == 0
    assert second["hubspot_companies_created"] == 0 and second["hubspot_companies_updated"] == 2
    assert (set(env.apollo["accounts"]), set(env.apollo["contacts"]),
            set(env.hubspot["companies"]), set(env.hubspot["contacts"]),
            env.hubspot["associations"]) == (set(snapshot[0]), set(snapshot[1]), *snapshot[2:])
    counts = db.one("SELECT (SELECT COUNT(*) FROM jobs) j, (SELECT COUNT(*) FROM companies) c,"
                    " (SELECT COUNT(*) FROM contacts) k")
    assert counts == {"j": 3, "c": 2, "k": 4}

    third = workflow.run_workflow(max_jobs=3)  # normal run excludes known job IDs
    assert FakeTheirStack.calls[-1]["job_id_not"] and third["status"] == "failed"
    assert "no new jobs" in third["errors"][0]


def test_company_without_decision_makers_is_partial(env):
    FakeTheirStack.jobs = [raw_job(1, "acme.com", "Acme"), raw_job(2, "tiny.io", "Tiny")]
    FakeApollo.people = {"acme.com": leaders("acme.com"), "tiny.io": []}
    result = workflow.run_workflow(max_jobs=2)
    assert result["status"] == "partial"
    assert result["companies_partial"] == ["tiny.io"]
    tiny = db.one("SELECT * FROM companies WHERE key='tiny.io'")
    assert tiny["apollo_status"] == "partial" and tiny["hubspot_status"] == "partial"
    assert "no matching decision-maker" in tiny["apollo_error"]
    assert db.one("SELECT apollo_status FROM companies WHERE key='acme.com'")["apollo_status"] \
        == "success"


def test_apollo_failure_does_not_block_hubspot(env):
    FakeTheirStack.jobs = [raw_job(1, "acme.com", "Acme")]
    FakeApollo.people = {"acme.com": leaders("acme.com")}
    FakeApollo.fail_accounts = True
    result = workflow.run_workflow(max_jobs=1)
    assert result["status"] == "partial"
    assert result["apollo_failed"] == 1 and result["hubspot_companies"] == 1
    company = db.one("SELECT * FROM companies WHERE key='acme.com'")
    assert company["apollo_status"] == "failed" and "server_error" in company["apollo_error"]
    assert company["hubspot_id"] and len(env.hubspot["companies"]) == 1
    failed_logs = db.query("SELECT * FROM integration_logs WHERE success=0")
    assert result["errors"] and isinstance(failed_logs, list)


def test_rejected_contact_is_not_resynced_and_is_replaced(env):
    FakeTheirStack.jobs = [raw_job(1, "acme.com", "Acme")]
    FakeApollo.people = {"acme.com": leaders("acme.com")}
    workflow.run_workflow(max_jobs=1)
    db.update("contacts", "apollo_person_id", "acme.com-vp", apollo_status="rejected")
    FakeApollo.people["acme.com"].append(
        {"id": "acme.com-dir", "first_name": "Di", "title": "Director of Engineering"})
    result = workflow.run_workflow(reprocess_existing=True)
    assert result["apollo_contacts_created"] == 1  # replacement only
    assert db.one("SELECT apollo_status FROM contacts WHERE apollo_person_id='acme.com-vp'")[
        "apollo_status"] == "rejected"


def test_rejected_company_is_never_resynced(env):
    FakeTheirStack.jobs = [raw_job(1, "acme.com", "Acme")]
    FakeApollo.people = {"acme.com": leaders("acme.com")}
    workflow.run_workflow(max_jobs=1)
    db.update("companies", "key", "acme.com", apollo_status="rejected",
              hubspot_status="rejected")
    before = (len(env.apollo["contacts"]), len(env.hubspot["contacts"]))
    result = workflow.run_workflow(reprocess_existing=True, sync_existing=True)
    assert result["apollo_accounts"] == 0 and result["hubspot_companies"] == 0
    assert (len(env.apollo["contacts"]), len(env.hubspot["contacts"])) == before
    assert db.one("SELECT apollo_status FROM companies WHERE key='acme.com'")[
        "apollo_status"] == "rejected"


def test_queue_prevents_duplicates_and_worker_executes_and_recovers(env):
    from bdlive import runs
    from bdlive.worker import Worker

    FakeTheirStack.jobs = [raw_job(1, "acme.com", "Acme")]
    FakeApollo.people = {"acme.com": leaders("acme.com")}
    run_id = runs.request_run({"max_jobs": 1, "sync_existing": False})
    with pytest.raises(runs.RunAlreadyActive):
        runs.request_run({"max_jobs": 1})  # one queued/running run at a time

    worker = Worker("test-worker")
    claimed = runs.claim_next(worker.worker_id)
    assert claimed["id"] == run_id and runs.claim_next(worker.worker_id) is None
    result = worker.execute(claimed)
    assert result["status"] == "success"
    row = db.one("SELECT * FROM processing_runs WHERE id=?", (run_id,))
    assert row["status"] == "success" and row["worker_id"] == "test-worker"
    assert runs.worker_state()["online"] and runs.worker_state()["state"] == "idle"

    # A run left 'running' by a killed worker is closed on the next worker start.
    stuck = runs.request_run({"max_jobs": 1})
    runs.claim_next("dead-worker")
    assert runs.recover_interrupted("test-worker") == [stuck]
    stuck_row = db.one("SELECT * FROM processing_runs WHERE id=?", (stuck,))
    assert stuck_row["status"] == "failed" and "interrupted" in stuck_row["error"]
    assert runs.active_run() is None  # a new run can now be requested
