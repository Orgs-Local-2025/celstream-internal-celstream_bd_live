"""Independent read-back of destination records from Apollo and HubSpot APIs, including
label correctness, company/contact linkage and duplicate detection."""

from __future__ import annotations

from collections import Counter

from bdlive import db
from bdlive.apollo import ApolloClient
from bdlive.http import ApiError
from bdlive.hubspot import COMPANY_READ, CONTACT_READ, HubSpotClient
from bdlive.settings import load_yaml

PREFIX = load_yaml("live_run.yaml")["apollo"].get("label_prefix", "Celstream BD")


def apollo_labels(apollo: ApolloClient) -> dict[str, str]:
    body = apollo.request("GET", "/api/v1/labels", "labels_list")
    return {label["id"]: label["name"] for label in body.get("data", [])}


def verify_company(key: str, apollo: ApolloClient, hubspot: HubSpotClient,
                   labels: dict[str, str]) -> dict:
    company = db.one("SELECT * FROM companies WHERE key = ?", (key,))
    score = db.one("SELECT * FROM scoring_results WHERE company_key = ?", (key,)) or {}
    contacts = db.query("SELECT * FROM contacts WHERE company_key = ? AND apollo_status !="
                        " 'rejected'", (key,))
    expected = {PREFIX, f"{PREFIX} Priority: {score.get('band')}"}
    out = {"company": key, "name": company["name"], "band": score.get("band"),
           "apollo": {}, "hubspot": {}, "issues": []}

    # ---- Apollo
    if company.get("apollo_account_id"):
        try:
            acc = apollo.get_account(company["apollo_account_id"])
            names = sorted(labels.get(i, i) for i in acc.get("label_ids") or [])
            out["apollo"]["account"] = {"id": acc.get("id"), "name": acc.get("name"),
                                        "domain": acc.get("domain"), "labels": names}
            if set(names) != expected:
                out["issues"].append(f"Apollo account labels {names} != expected "
                                     f"{sorted(expected)}")
        except ApiError as exc:
            out["issues"].append(f"Apollo account read failed: {exc}")
    else:
        out["issues"].append("no Apollo account")
    out["apollo"]["contacts"] = []
    for c in contacts:
        if not c.get("apollo_contact_id"):
            continue
        try:
            rec = apollo.get_contact(c["apollo_contact_id"])
            linked = rec.get("account_id") == company.get("apollo_account_id")
            out["apollo"]["contacts"].append({
                "id": rec.get("id"), "name": rec.get("name"), "title": rec.get("title"),
                "account_id": rec.get("account_id"), "linked_to_account": linked,
                "labels": sorted(labels.get(i, i) for i in rec.get("label_ids") or []),
            })
            if not linked:
                out["issues"].append(f"Apollo contact {rec.get('name')} not linked to account")
        except ApiError as exc:
            out["issues"].append(f"Apollo contact {c['apollo_contact_id']} read failed: {exc}")
    if not out["apollo"]["contacts"]:
        out["issues"].append("no Apollo contacts (incomplete)")

    # ---- HubSpot
    if company.get("hubspot_id"):
        rec = hubspot.get("companies", company["hubspot_id"], COMPANY_READ, "contacts")
        assoc = sorted({a["id"] for a in rec.get("associations", {}).get("contacts", {})
                        .get("results", [])})
        props = rec.get("properties", {})
        out["hubspot"]["company"] = {"id": rec.get("id"),
                                     **{k: props.get(k) for k in COMPANY_READ
                                        if k.startswith("celstream") or k in ("name", "domain")},
                                     "associated_contact_ids": assoc}
        for prop in ("celstream_opportunity_score", "celstream_opportunity_priority",
                     "celstream_capability_score", "celstream_matched_capability_count",
                     "celstream_hiring_signals", "celstream_ai_recommendation"):
            if props.get(prop) in (None, ""):
                out["issues"].append(f"HubSpot company property {prop} empty")
        dupes = hubspot.request(
            "POST", "/crm/v3/objects/companies/search", "companies_search",
            json={"filterGroups": [{"filters": [{"propertyName": "domain", "operator": "EQ",
                                                  "value": company["domain"]}]}], "limit": 10})
        out["hubspot"]["companies_with_domain"] = dupes.get("total")
        if dupes.get("total", 0) != 1:
            out["issues"].append(f"HubSpot has {dupes.get('total')} companies for domain")
        out["hubspot"]["contacts"] = []
        for c in contacts:
            if not c.get("hubspot_id"):
                continue
            rec = hubspot.get("contacts", c["hubspot_id"], CONTACT_READ)
            p = rec.get("properties", {})
            same = hubspot.request(
                "POST", "/crm/v3/objects/contacts/search", "contacts_search",
                json={"filterGroups": [{"filters": [{"propertyName": "celstream_apollo_id",
                                                      "operator": "EQ",
                                                      "value": c["apollo_person_id"]}]}],
                      "limit": 10}).get("total")
            out["hubspot"]["contacts"].append({
                "id": rec.get("id"), "name": f"{p.get('firstname')} {p.get('lastname')}",
                "jobtitle": p.get("jobtitle"), "associated": rec.get("id") in assoc,
                "records_with_same_apollo_id": same,
            })
            if rec.get("id") not in assoc:
                out["issues"].append(f"HubSpot contact {rec.get('id')} not associated")
            if same != 1:
                out["issues"].append(f"HubSpot has {same} contacts for Apollo person "
                                     f"{c['apollo_person_id']}")
    else:
        out["issues"].append("no HubSpot company")
    return out


def platform_totals(apollo: ApolloClient, hubspot: HubSpotClient) -> dict:
    def apollo_total(path):
        body = apollo.request("POST", path, path.rsplit("/", 2)[-2] + "_search",
                              json={"page": 1, "per_page": 1})
        return (body.get("pagination") or {}).get("total_entries")

    def hubspot_total(kind):
        return hubspot.request("POST", f"/crm/v3/objects/{kind}/search", f"{kind}_search",
                               json={"limit": 1}).get("total")

    accounts = apollo.request("POST", "/api/v1/accounts/search", "accounts_search",
                              json={"page": 1, "per_page": 100}).get("accounts", [])
    domains = Counter((a.get("domain") or "").lower() for a in accounts)
    return {
        "apollo_accounts_total": apollo_total("/api/v1/accounts/search"),
        "apollo_contacts_total": apollo_total("/api/v1/contacts/search"),
        "apollo_duplicate_account_domains": {d: n for d, n in domains.items() if n > 1 and d},
        "hubspot_companies_total": hubspot_total("companies"),
        "hubspot_contacts_total": hubspot_total("contacts"),
    }


def verify_all() -> dict:
    keys = [r["key"] for r in db.query(
        "SELECT key FROM companies WHERE (apollo_account_id IS NOT NULL OR hubspot_id IS NOT"
        " NULL) AND apollo_status != 'rejected'"
        " ORDER BY key")]
    apollo, hubspot = ApolloClient(), HubSpotClient()
    try:
        labels = apollo_labels(apollo)
        companies = [verify_company(k, apollo, hubspot, labels) for k in keys]
        totals = platform_totals(apollo, hubspot)
    finally:
        apollo.close()
        hubspot.close()
    return {"totals": totals, "companies": companies,
            "issues": {c["company"]: c["issues"] for c in companies if c["issues"]}}
