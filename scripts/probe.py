"""Quick live connectivity probe for all four providers. Never prints secrets."""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
T = httpx.Timeout(30, connect=10)


def show(name, resp, extra=""):
    print(f"[{name}] HTTP {resp.status_code} {extra}")
    if resp.status_code >= 300:
        print("   body:", resp.text[:400].replace("\n", " "))


def theirstack():
    r = httpx.post(
        "https://api.theirstack.com/v1/jobs/search",
        headers={"Authorization": "Bearer " + os.environ["THEIRSTACK_API_KEY"]},
        json={
            "posted_at_max_age_days": 30,
            "job_title_or": ["DevOps Engineer"],
            "job_country_code_or": ["US"],
            "page": 0,
            "limit": 1,
        },
        timeout=T,
    )
    extra = ""
    if r.is_success:
        d = r.json()
        j = (d.get("data") or [{}])[0]
        extra = f"jobs={len(d.get('data', []))} keys={sorted(j)[:60]} meta={d.get('metadata')}"
    show("theirstack", r, extra)


def openai_probe():
    r = httpx.get(
        "https://api.openai.com/v1/models/" + os.environ["OPENAI_MODEL"],
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
        timeout=T,
    )
    show("openai", r, r.text[:200] if r.is_success else "")


def apollo():
    h = {"x-api-key": os.environ["APOLLO_API_KEY"], "accept": "application/json",
         "Content-Type": "application/json"}
    r = httpx.get("https://api.apollo.io/v1/auth/health", headers=h, timeout=T)
    show("apollo health", r, r.text[:200])
    r = httpx.post("https://api.apollo.io/api/v1/accounts/search", headers=h,
                   json={"page": 1, "per_page": 1}, timeout=T)
    show("apollo accounts/search", r,
         str(r.json().get("pagination")) if r.is_success else "")
    r = httpx.post("https://api.apollo.io/api/v1/contacts/search", headers=h,
                   json={"page": 1, "per_page": 1}, timeout=T)
    show("apollo contacts/search", r,
         str(r.json().get("pagination")) if r.is_success else "")
    r = httpx.post("https://api.apollo.io/api/v1/mixed_people/api_search", headers=h,
                   params={"page": 1, "per_page": 1, "q_organization_domains_list[]": "stripe.com"},
                   timeout=T)
    show("apollo mixed_people/api_search", r,
         f"total={r.json().get('total_entries')}" if r.is_success else "")
    r = httpx.get("https://api.apollo.io/api/v1/organizations/enrich", headers=h,
                  params={"domain": "stripe.com"}, timeout=T)
    show("apollo organizations/enrich", r,
         str((r.json().get("organization") or {}).get("name")) if r.is_success else "")


def hubspot():
    h = {"Authorization": "Bearer " + os.environ["HUBSPOT_ACCESS_TOKEN"]}
    for kind in ("companies", "contacts"):
        r = httpx.get(f"https://api.hubapi.com/crm/v3/properties/{kind}", headers=h, timeout=T)
        extra = ""
        if r.is_success:
            names = sorted(p["name"] for p in r.json()["results"] if p["name"].startswith("celstream"))
            extra = f"celstream props={names}"
        show(f"hubspot {kind} props", r, extra)
    r = httpx.post("https://api.hubapi.com/crm/v3/objects/companies/search", headers=h,
                   json={"limit": 1}, timeout=T)
    show("hubspot companies/search", r, f"total={r.json().get('total')}" if r.is_success else "")


if __name__ == "__main__":
    targets = sys.argv[1:] or ["theirstack", "openai", "apollo", "hubspot"]
    for t in targets:
        try:
            {"theirstack": theirstack, "openai": openai_probe, "apollo": apollo,
             "hubspot": hubspot}[t]()
        except Exception as e:  # noqa: BLE001
            print(f"[{t}] EXCEPTION {type(e).__name__}")
