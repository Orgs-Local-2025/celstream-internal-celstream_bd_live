"""Idempotent creation of the celstream_* HubSpot properties this app populates.

Only creates properties that are missing; never modifies or deletes existing ones.
Run:  python -m bdlive.hubspot_schema
"""

from __future__ import annotations

import json

from bdlive.http import ApiError
from bdlive.hubspot import HubSpotClient

TEXT = {"type": "string", "fieldType": "text"}
TEXTAREA = {"type": "string", "fieldType": "textarea"}
NUMBER = {"type": "number", "fieldType": "number"}
DATE = {"type": "date", "fieldType": "date"}

PROPERTIES = {
    "companies": [
        ("celstream_lead_source", "Celstream Lead Source", TEXT,
         "Origin of the lead (e.g. TheirStack hiring signal)."),
        ("celstream_job_references", "Celstream Job References", TEXTAREA,
         "TheirStack job IDs and posting URLs that produced this opportunity."),
        ("celstream_last_discovery_date", "Celstream Last Discovery Date", DATE,
         "Date the Celstream BD workflow last processed this company."),
        ("celstream_source_system", "Celstream Source System", TEXT,
         "System that wrote the Celstream BD fields."),
        ("celstream_capability_score", "Celstream Capability Score", NUMBER,
         "Best job-level capability matching score (0-100) from the OpenAI analysis."),
        ("celstream_matched_capability_count", "Celstream Matched Capability Count", NUMBER,
         "Number of distinct Celstream capabilities evidenced in the company's job postings."),
        ("celstream_matched_capabilities", "Celstream Matched Capabilities", TEXTAREA,
         "Matched Celstream capabilities with AI confidence."),
    ],
    "contacts": [
        ("celstream_source_system", "Celstream Source System", TEXT,
         "System that wrote the Celstream BD fields."),
        ("celstream_last_discovery_date", "Celstream Last Discovery Date", DATE,
         "Date the Celstream BD workflow last processed this contact."),
    ],
}
GROUPS = {"companies": "companyinformation", "contacts": "contactinformation"}


def ensure_properties(client: HubSpotClient | None = None) -> dict:
    own = client is None
    client = client or HubSpotClient()
    result = {"created": [], "existing": [], "failed": []}
    try:
        for kind, specs in PROPERTIES.items():
            existing = client.properties(kind)
            for name, label, typ, description in specs:
                if name in existing:
                    result["existing"].append(f"{kind}.{name}")
                    continue
                try:
                    client.request("POST", f"/crm/v3/properties/{kind}", "property_create",
                                   json={"name": name, "label": label, "description": description,
                                         "groupName": GROUPS[kind], **typ},
                                   entity=f"{kind}.{name}", retries=0)
                    result["created"].append(f"{kind}.{name}")
                except ApiError as exc:
                    if exc.status == 409:  # created concurrently
                        result["existing"].append(f"{kind}.{name}")
                    else:
                        result["failed"].append(f"{kind}.{name}: {exc}")
            client._properties.pop(kind, None)  # refresh cache
    finally:
        if own:
            client.close()
    return result


if __name__ == "__main__":
    print(json.dumps(ensure_properties(), indent=2))
