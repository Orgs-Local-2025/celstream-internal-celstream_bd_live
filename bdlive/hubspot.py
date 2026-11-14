"""HubSpot CRM client: company/contact upsert and company->contact association
(reuses the reference project's private-app token, CRM v3 objects, v4 default
association endpoint and celstream_* property mapping)."""

from __future__ import annotations

from bdlive.http import ApiClient
from bdlive.settings import settings

COMPANY_READ = ["name", "domain", "website", "numberofemployees", "country", "city",
                "linkedin_company_page", "celstream_opportunity_score",
                "celstream_opportunity_priority", "celstream_service_interest",
                "celstream_hiring_signals", "celstream_ai_recommendation",
                "celstream_capability_score", "celstream_matched_capability_count",
                "celstream_matched_capabilities", "celstream_job_references",
                "celstream_lead_source", "celstream_source_system",
                "celstream_last_discovery_date"]
CONTACT_READ = ["firstname", "lastname", "email", "jobtitle", "company", "hs_linkedin_url",
                "celstream_linkedin_url", "celstream_apollo_id", "celstream_source_system",
                "celstream_last_discovery_date"]


class HubSpotClient(ApiClient):
    provider = "hubspot"
    base_url = "https://api.hubapi.com"

    def __init__(self, run_id=None):
        super().__init__(run_id, min_interval=0.25)
        self._properties: dict[str, set[str]] = {}

    def headers(self):
        return {"Authorization": "Bearer " + settings().hubspot_access_token,
                "Content-Type": "application/json"}

    def properties(self, kind: str) -> set[str]:
        if kind not in self._properties:
            body = self.request("GET", f"/crm/v3/properties/{kind}", "properties_list")
            self._properties[kind] = {p["name"] for p in body.get("results", [])}
        return self._properties[kind]

    def supported(self, kind: str, props: dict) -> dict:
        """Drop properties that do not exist in this portal (no schema changes are made)."""
        names = self.properties(kind)
        return {k: v for k, v in props.items() if k in names and v not in (None, "")}

    def search_one(self, kind: str, prop: str, value: str, read: list[str]) -> dict | None:
        body = self.request(
            "POST", f"/crm/v3/objects/{kind}/search", f"{kind}_search",
            json={"filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ",
                                                  "value": value}]}],
                  "properties": read, "limit": 2},
            entity=value,
        )
        results = body.get("results", [])
        return results[0] if results else None

    def get(self, kind: str, object_id: str, read: list[str], associations: str | None = None):
        params = {"properties": ",".join(read)}
        if associations:
            params["associations"] = associations
        return self.request("GET", f"/crm/v3/objects/{kind}/{object_id}", f"{kind}_get",
                            params=params, entity=object_id)

    def create(self, kind: str, props: dict) -> dict:
        return self.request("POST", f"/crm/v3/objects/{kind}", f"{kind}_create",
                            json={"properties": props}, retries=0,
                            entity=props.get("domain") or props.get("email")
                            or props.get("celstream_apollo_id"))

    def patch(self, kind: str, object_id: str, props: dict) -> dict:
        return self.request("PATCH", f"/crm/v3/objects/{kind}/{object_id}", f"{kind}_update",
                            json={"properties": props}, entity=object_id)

    def upsert(self, kind: str, match: list[tuple[str, str]], fill_empty: dict,
               app_owned: dict, read: list[str]) -> tuple[str, str]:
        """Find by the first matching identifier, then fill empty standard fields and refresh
        app-owned celstream_* fields. Returns (object id, 'created'|'updated'|'unchanged')."""
        fill_empty = self.supported(kind, fill_empty)
        app_owned = self.supported(kind, app_owned)
        existing = None
        for prop, value in match:
            if value and prop in self.properties(kind):
                existing = self.search_one(kind, prop, value, read)
                if existing:
                    break
        if existing is None:
            created = self.create(kind, {**fill_empty, **app_owned})
            return created["id"], "created"
        current = existing.get("properties", {})
        changes = {k: v for k, v in fill_empty.items() if not current.get(k)}
        changes.update({k: v for k, v in app_owned.items() if str(current.get(k) or "") != str(v)})
        if changes:
            self.patch(kind, existing["id"], changes)
            return existing["id"], "updated"
        return existing["id"], "unchanged"

    def associate(self, company_id: str, contact_id: str) -> None:
        self.request(
            "PUT",
            f"/crm/v4/objects/companies/{company_id}/associations/default/contacts/{contact_id}",
            "association_create", entity=f"{company_id}->{contact_id}",
        )

    def associated_contacts(self, company_id: str) -> set[str]:
        body = self.request("GET", f"/crm/v4/objects/companies/{company_id}/associations/contacts",
                            "association_list", entity=company_id)
        return {str(r.get("toObjectId")) for r in body.get("results", [])}
