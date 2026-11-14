"""Deterministic company opportunity scoring, ported unchanged from the reference
project's processing/scoring.py::score_opportunity and driven by config/scoring.yaml."""

from __future__ import annotations

from datetime import date

from bdlive.settings import catalogue, scoring_rules


def score_company(company: dict, jobs: list[dict], analyses: dict[str, dict],
                  reference_date: date | None = None) -> dict:
    rules = scoring_rules()
    weights = rules["weights"]
    reference_date = reference_date or date.today()
    matched_ids: set[str] = set()
    relevant_jobs = []
    for job in jobs:
        analysis = analyses.get(job["id"])
        if analysis and analysis.get("matches"):
            matched_ids.update(m["capability_id"] for m in analysis["matches"])
            relevant_jobs.append(job)
    capability_fit = weights["capability_fit"] * min(
        len(matched_ids) / rules["capability_match_target"], 1
    )
    hiring_volume = weights["hiring_volume"] * min(len(relevant_jobs) / rules["relevant_job_cap"], 1)
    ages = []
    for job in relevant_jobs:
        try:
            ages.append((reference_date - date.fromisoformat(job["posted_at"])).days)
        except (TypeError, ValueError):
            pass
    valid_ages = [age for age in ages if age >= 0]
    freshness = max(0, 1 - min(valid_ages) / rules["recency_days"]) if valid_ages else 0
    recency = weights["recency"] * freshness
    country_fit = (company.get("country") or "").casefold() in {
        c.casefold() for c in rules["target_countries"]
    }
    employees = company.get("employees")
    size_fit = employees is not None and rules["employee_min"] <= employees <= rules["employee_max"]
    company_fit = (
        weights["company_fit"] * (int(country_fit) + int(size_fit)) / 2 if matched_ids else 0
    )
    components = {
        "Capability fit": round(capability_fit, 2),
        "Relevant hiring": round(hiring_volume, 2),
        "Recency": round(recency, 2),
        "Company profile": round(company_fit, 2),
    }
    total = round(sum(components.values()), 2)
    band = (
        "High" if total >= rules["high_threshold"]
        else "Medium" if total >= rules["medium_threshold"] else "Low"
    )
    rationale = [
        f"{len(matched_ids)} distinct capabilities matched; full capability-fit points require "
        f"{rules['capability_match_target']} matches (points capped at {weights['capability_fit']}).",
        f"{len(relevant_jobs)} distinct relevant jobs; volume capped at {rules['relevant_job_cap']}.",
        f"Freshness uses a {rules['recency_days']}-day linear window at {reference_date.isoformat()}.",
        "Company profile: geography and employee range each contribute half of profile points.",
        "Hiring signals suggest service relevance; outsourcing intent is unverified.",
    ]
    if company.get("country") is None or employees is None:
        rationale.append("Missing company attributes receive zero corresponding profile points.")
    provenance = {c["id"]: c.get("provenance") for c in catalogue()["capabilities"]}
    for cap in sorted(matched_ids):
        if provenance.get(cap) == "thinxtream_partner":
            rationale.append(f"{cap}: Thinxtream partner capability (not Celstream-only).")
    return {
        "total": total,
        "band": band,
        "components": components,
        "rationale": rationale,
        "matched_capabilities": sorted(matched_ids),
        "relevant_job_ids": [j["id"] for j in relevant_jobs],
        "rules_version": rules.get("version"),
    }
