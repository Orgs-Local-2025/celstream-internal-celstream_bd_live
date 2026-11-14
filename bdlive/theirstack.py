"""TheirStack job search client + normalization (reuses the reference project's
endpoint, auth scheme, search criteria and field mapping)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bdlive.http import ApiClient, ApiError
from bdlive.settings import load_yaml, search_config, settings


class TheirStackClient(ApiClient):
    provider = "theirstack"
    base_url = "https://api.theirstack.com"

    def headers(self):
        return {"Authorization": "Bearer " + settings().theirstack_api_key}

    def search_jobs(self, max_jobs: int, extra_filters: dict | None = None,
                    page_size: int = 10, required_filters: dict | None = None) -> list[dict]:
        """extra_filters are optional and dropped once if the provider rejects them;
        required_filters (job ID include/exclude lists) are never dropped."""
        criteria = dict(search_config()["criteria"])
        criteria = {k: v for k, v in criteria.items() if v not in ([], None, "")}
        criteria.update(extra_filters or {})
        criteria.update(required_filters or {})
        jobs: list[dict] = []
        page = 0
        while len(jobs) < max_jobs:
            limit = min(page_size, max_jobs - len(jobs))
            body = {**criteria, "page": page, "limit": limit}
            try:
                payload = self.request("POST", "/v1/jobs/search", "jobs_search", json=body,
                                       entity=f"page:{page}")
            except ApiError as error:
                # Drop optional extra filters once if the provider rejects them.
                if error.status == 422 and extra_filters and page == 0:
                    return self.search_jobs(max_jobs, None, page_size, required_filters)
                raise
            data = payload.get("data") or []
            jobs.extend(data)
            if len(data) < limit:
                break
            page += 1
        return jobs[:max_jobs]


def normalize_domain(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip().lower()
    if "://" not in value:
        value = "https://" + value
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return None
    host = host.removeprefix("www.").strip(".")
    if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    return host


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{8,}\d(?!\w)")


def sanitize_text(text: str) -> str:
    """Remove personal contact details from posting text before AI analysis."""
    text = _EMAIL.sub("[email]", text or "")
    text = _PHONE.sub("[phone]", text)
    return " ".join(text.split())


def normalize_job(raw: dict) -> dict | None:
    company = raw.get("company_object") or {}
    title = (raw.get("job_title") or "").strip()
    name = (raw.get("company") or company.get("name") or "").strip()
    description = sanitize_text(raw.get("description") or "")
    if not raw.get("id") or not title or not name or not description:
        return None
    domain = normalize_domain(raw.get("company_domain") or company.get("domain"))
    company_key = domain or f"theirstack:{company.get('id') or name.lower()}"
    location = (
        raw.get("long_location") or raw.get("location") or raw.get("short_location")
        or ", ".join(raw.get("cities") or []) or raw.get("country") or "Unspecified"
    )
    employees = company.get("employee_count")
    return {
        "id": f"theirstack:{raw['id']}",
        "theirstack_id": str(raw["id"]),
        "title": title,
        "company_name": name,
        "company_domain": domain,
        "company_key": company_key,
        "location": location,
        "country": raw.get("country"),
        "source_url": raw.get("final_url") or raw.get("source_url") or raw.get("url"),
        "posted_at": (raw.get("date_posted") or "")[:10] or None,
        "seniority": raw.get("seniority"),
        "employment_type": ", ".join(raw.get("employment_statuses") or []) or None,
        "remote": 1 if raw.get("remote") else 0,
        "technologies": sorted(set(raw.get("technology_slugs") or [])),
        "description": description[: settings().max_description_chars],
        "company": {
            "key": company_key,
            "name": name,
            "domain": domain,
            "country": company.get("country") or raw.get("country"),
            "industry": company.get("industry"),
            "employees": employees if isinstance(employees, int) else None,
            "linkedin_url": company.get("linkedin_url"),
            "theirstack_company_id": str(company.get("id")) if company.get("id") else None,
        },
    }


def run_config() -> dict:
    return load_yaml("live_run.yaml")
