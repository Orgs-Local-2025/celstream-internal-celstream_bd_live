"""Apollo client: organization enrichment, people discovery/enrichment, and creation of
actual Apollo Account and Contact records (reuses the reference project's x-api-key auth,
people search/match endpoints, role titles and no-personal-email/phone reveal policy)."""

from __future__ import annotations

import re
import unicodedata

from bdlive.http import ApiClient, ApiError
from bdlive.settings import apollo_config, settings


def normalize_title(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", value or "")
                           .casefold()).split())


def role_for(title: str) -> str | None:
    normalized = normalize_title(title)
    for role in apollo_config()["roles"]:
        if normalized in {normalize_title(t) for t in role["titles"]}:
            return role["category"]
    # Looser fallback for similar titles Apollo returns ("VP, Engineering", "CTO & Co-founder").
    keywords = [
        ("technology_executive", ("cto", "chief technology", "cio", "chief information",
                                  "chief digital")),
        ("engineering_vp", ("vp engineering", "vp of engineering", "vice president engineering",
                            "vice president of engineering")),
        ("engineering_head", ("head of engineering", "head of technology", "head of software")),
        ("engineering_director", ("director of engineering", "engineering director",
                                  "director of technology", "technology director")),
        ("quality_leader", ("head of qa", "qa director", "quality")),
        ("engineering_manager", ("engineering manager",)),
        ("product_leader", ("head of product",)),
    ]
    for category, needles in keywords:
        if any(re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", normalized) for n in needles):
            return category
    # Small companies: founders / CEO / COO are the buying decision-makers. Non-buying
    # functions (HR, talent, finance, sales, marketing) are never selected.
    if re.search(r"(?<!\w)(talent|people|hr|human resources|recruit\w*|finance|sales|"
                 r"marketing|accountant)(?!\w)", normalized):
        return None
    for category, needles in [
        ("executive_founder", ("founder", "co founder", "cofounder", "ceo", "chief executive",
                               "company owner", "business owner", "managing director",
                               "managing partner", "president")),
        ("operations_executive", ("coo", "chief operating")),
    ]:
        if any(re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", normalized) for n in needles):
            return category
    return None


ROLE_PRIORITY = [r["category"] for r in apollo_config()["roles"]] + [
    "executive_founder", "operations_executive"]
DECISION_SENIORITIES = ["owner", "founder", "c_suite", "partner", "vp", "head", "director"]


class ApolloClient(ApiClient):
    provider = "apollo"
    base_url = "https://api.apollo.io"

    def __init__(self, run_id=None):
        super().__init__(run_id, min_interval=1.0)

    def headers(self):
        return {"x-api-key": settings().apollo_api_key, "accept": "application/json",
                "Content-Type": "application/json", "Cache-Control": "no-cache"}

    # ---- organizations / accounts -------------------------------------------------
    def enrich_organization(self, domain: str) -> dict | None:
        body = self.request("GET", "/api/v1/organizations/enrich", "organizations_enrich",
                            params={"domain": domain}, entity=domain)
        return body.get("organization")

    def find_account(self, name: str, domain: str | None) -> dict | None:
        body = self.request("POST", "/api/v1/accounts/search", "accounts_search",
                            json={"q_organization_name": name, "page": 1, "per_page": 25},
                            entity=domain or name)
        for account in body.get("accounts", []):
            acc_domain = (account.get("domain") or account.get("primary_domain") or "").lower()
            acc_domain = acc_domain.removeprefix("www.")
            if domain and acc_domain == domain:
                return account
        return None

    def create_account(self, fields: dict) -> dict:
        body = self.request("POST", "/api/v1/accounts", "accounts_create", json=fields,
                            entity=fields.get("domain"), retries=0)
        return body.get("account") or body

    def update_account(self, account_id: str, fields: dict) -> dict:
        body = self.request("PUT", f"/api/v1/accounts/{account_id}", "accounts_update",
                            json=fields, entity=account_id, retries=0)
        return body.get("account") or body

    def get_account(self, account_id: str) -> dict:
        body = self.request("GET", f"/api/v1/accounts/{account_id}", "accounts_get",
                            entity=account_id)
        return body.get("account") or body

    # ---- people / contacts --------------------------------------------------------
    def search_people(self, domain: str, titles: list[str], similar: bool,
                      per_page: int = 10, seniorities: list[str] | None = None) -> list[dict]:
        params = [("page", 1), ("per_page", per_page),
                  ("q_organization_domains_list[]", domain)]
        if titles:
            params.append(("include_similar_titles", "true" if similar else "false"))
        params += [("person_titles[]", t) for t in titles]
        params += [("person_seniorities[]", s) for s in seniorities or []]
        body = self.request("POST", "/api/v1/mixed_people/api_search", "people_search",
                            params=params, entity=domain)
        return body.get("people", [])

    def enrich_person(self, person_id: str) -> dict | None:
        # Never replayed automatically: enrichment can consume credits even if a response
        # is lost. Personal emails and phone numbers are never requested.
        body = self.request("POST", "/api/v1/people/match", "people_match", retries=0,
                            params={"id": person_id, "reveal_personal_emails": "false",
                                    "reveal_phone_number": "false"}, entity=person_id)
        return body.get("person")

    def find_contact(self, keywords: str) -> list[dict]:
        body = self.request("POST", "/api/v1/contacts/search", "contacts_search",
                            json={"q_keywords": keywords, "page": 1, "per_page": 10},
                            entity=keywords)
        return body.get("contacts", [])

    def create_contact(self, fields: dict) -> dict:
        body = self.request("POST", "/api/v1/contacts", "contacts_create",
                            json={**fields, "run_dedupe": True}, entity=fields.get("email")
                            or f"{fields.get('first_name')} {fields.get('last_name')}",
                            retries=0)
        return body.get("contact") or body

    def update_contact(self, contact_id: str, fields: dict) -> dict:
        body = self.request("PUT", f"/api/v1/contacts/{contact_id}", "contacts_update",
                            json=fields, entity=contact_id, retries=0)
        return body.get("contact") or body

    def get_contact(self, contact_id: str) -> dict:
        body = self.request("GET", f"/api/v1/contacts/{contact_id}", "contacts_get",
                            entity=contact_id)
        return body.get("contact") or body


def configured_titles() -> list[str]:
    return [t for role in apollo_config()["roles"] for t in role["titles"]]


def pick_people(people: list[dict], limit: int) -> list[tuple[dict, str]]:
    ranked = []
    for person in people:
        role = role_for(person.get("title") or "")
        if role:
            ranked.append((ROLE_PRIORITY.index(role) if role in ROLE_PRIORITY else 99,
                           person, role))
    ranked.sort(key=lambda item: item[0])
    # First pass: one person per role category (e.g. CTO, then VP Engineering); then fill.
    seen, roles, picked = set(), set(), []
    for diverse in (True, False):
        for _, person, role in ranked:
            if len(picked) >= limit:
                return picked
            if person.get("id") in seen or (diverse and role in roles):
                continue
            seen.add(person.get("id"))
            roles.add(role)
            picked.append((person, role))
    return picked


__all__ = ["ApolloClient", "ApiError", "configured_titles", "pick_people", "role_for"]
