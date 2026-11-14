"""OpenAI capability analysis (reuses the reference prompt, catalogue, model and pricing).

Uses the Responses API with a strict JSON schema identical in shape to the
reference project's JobAnalysis model.
"""

from __future__ import annotations

import json
import re
import time

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from bdlive.http import ApiError, log_event, scrub
from bdlive.settings import PROMPT_VERSION, analysis_prompt, catalogue, settings

_STR_LIST = {"type": "array", "items": {"type": "string"}}
JOB_ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "job_title": {"type": "string"},
        "company_name": {"type": "string"},
        "technologies": _STR_LIST,
        "engineering_skills": _STR_LIST,
        "responsibilities": _STR_LIST,
        "seniority": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "capability_matches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "capability_id": {"type": "string"},
                    "evidence": {"type": "string"},
                    "explanation": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["capability_id", "evidence", "explanation", "confidence"],
            },
        },
        "confidence": {"type": "number"},
        "suggested_personas": _STR_LIST,
        "caveats": _STR_LIST,
    },
    "required": [
        "job_title", "company_name", "technologies", "engineering_skills", "responsibilities",
        "seniority", "summary", "capability_matches", "confidence", "suggested_personas",
        "caveats",
    ],
}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[*_`#>]+", " ", text or "").casefold().split())


def alias_matches(description: str, aliases: dict) -> list[dict]:
    text = _norm(description)
    found = []
    for canonical, values in (aliases or {}).items():
        for term in [canonical, *values]:
            if re.search(r"(?<!\w)" + re.escape(_norm(term)) + r"(?!\w)", text):
                found.append({"canonical": canonical, "matched": term})
                break
    return found


def capability_score(matches: list[dict]) -> float:
    """Job-level capability matching score (0-100): confidence-weighted matches against the
    scoring.yaml capability_match_target."""
    from bdlive.settings import scoring_rules

    target = scoring_rules().get("capability_match_target", 3)
    total = sum(max(0.0, min(1.0, float(m.get("confidence", 0)))) for m in matches)
    return round(100 * min(1.0, total / target), 1)


def cost_usd(usage) -> float | None:
    s = settings()
    if usage is None or s.openai_input_usd_per_million is None:
        return None
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cached_price = s.openai_cached_input_usd_per_million or s.openai_input_usd_per_million
    return round(
        ((input_tokens - cached) * s.openai_input_usd_per_million
         + cached * cached_price
         + output_tokens * (s.openai_output_usd_per_million or 0)) / 1_000_000,
        6,
    )


class OpenAIAnalyzer:
    provider = "openai"
    _max_tokens: int | None = None

    def __init__(self, run_id: int | None = None):
        s = settings()
        self.run_id = run_id
        self.model = s.openai_model
        self.client = OpenAI(
            api_key=s.openai_api_key,
            timeout=httpx.Timeout(s.openai_timeout_seconds, connect=10),
            max_retries=0,  # retries are bounded and logged here
        )
        cat = catalogue()
        self.capabilities = [
            {k: c.get(k) for k in ("id", "name", "description", "technologies", "provenance",
                                   "validation_status")}
            for c in cat["capabilities"]
        ]
        self.capability_ids = {c["id"] for c in cat["capabilities"]}
        self.aliases = cat.get("technology_aliases", {})
        self.spent_usd = 0.0
        self.requests = self.input_tokens = self.output_tokens = 0

    def _call(self, job: dict, max_output_tokens: int):
        payload = {
            "job_title": job["title"],
            "company_name": job["company_name"],
            "description": job["description"],
            "technology_alias_matches": alias_matches(job["description"], self.aliases),
            "capabilities": self.capabilities,
        }
        return self.client.responses.create(
            model=self.model,
            store=False,
            instructions=analysis_prompt(),
            input=json.dumps(payload, ensure_ascii=False),
            max_output_tokens=max_output_tokens,
            text={"format": {"type": "json_schema", "name": "JobAnalysis", "strict": True,
                             "schema": JOB_ANALYSIS_SCHEMA}},
        )

    def analyse(self, job: dict) -> dict:
        s = settings()
        if self.spent_usd >= s.ai_run_budget_usd:
            raise ApiError("openai", "budget_exhausted", f"run budget {s.ai_run_budget_usd} USD")
        max_tokens = OpenAIAnalyzer._max_tokens or s.openai_max_output_tokens
        last_error: ApiError | None = None
        attempts = s.openai_max_retries + 1
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = self._call(job, max_tokens)
            except APITimeoutError:
                last_error = ApiError("openai", "timeout", "request timed out")
            except APIConnectionError:
                last_error = ApiError("openai", "connectivity_error", "connection failed")
            except APIStatusError as error:
                category = {401: "auth_error", 403: "auth_error", 429: "rate_limited"}.get(
                    error.status_code, "server_error" if error.status_code >= 500 else "bad_request"
                )
                last_error = ApiError("openai", category, scrub(str(error.body), 300),
                                      error.status_code)
            else:
                duration = int((time.monotonic() - started) * 1000)
                usage = response.usage
                cost = cost_usd(usage) or 0.0
                self.spent_usd += cost
                self.requests += 1
                self.input_tokens += getattr(usage, "input_tokens", 0) or 0
                self.output_tokens += getattr(usage, "output_tokens", 0) or 0
                status = getattr(response, "status", None)
                reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
                if status == "incomplete" and reason == "max_output_tokens":
                    log_event("openai", "responses.create", run_id=self.run_id, method="POST",
                              endpoint="/v1/responses", attempt=attempt, http_status=200,
                              success=False, duration_ms=duration, entity=job["id"],
                              error=f"incomplete: max_output_tokens={max_tokens}")
                    last_error = ApiError("openai", "output_token_limit", f"limit {max_tokens}")
                    max_tokens = max(max_tokens, s.openai_retry_max_output_tokens)
                    OpenAIAnalyzer._max_tokens = max_tokens  # sticky for this process
                    continue
                try:
                    if status != "completed":
                        raise ValueError(f"response status {status} ({reason})")
                    data = json.loads(response.output_text)
                except (ValueError, TypeError) as exc:
                    log_event("openai", "responses.create", run_id=self.run_id, method="POST",
                              endpoint="/v1/responses", attempt=attempt, http_status=200,
                              success=False, duration_ms=duration, entity=job["id"],
                              error=f"invalid_response: {exc}")
                    last_error = ApiError("openai", "invalid_response", str(exc))
                    continue
                log_event("openai", "responses.create", run_id=self.run_id, method="POST",
                          endpoint="/v1/responses", attempt=attempt, http_status=200,
                          success=True, duration_ms=duration, entity=job["id"],
                          external_id=getattr(response, "id", None))
                return self._finalize(job, data, usage, cost)
            duration = int((time.monotonic() - started) * 1000)
            log_event("openai", "responses.create", run_id=self.run_id, method="POST",
                      endpoint="/v1/responses", attempt=attempt, http_status=last_error.status,
                      success=False, duration_ms=duration, entity=job["id"], error=str(last_error))
            if last_error.category == "auth_error" or last_error.category == "bad_request":
                break
            if attempt < attempts:
                time.sleep(2 ** attempt)
        raise last_error or ApiError("openai", "unknown", "analysis failed")

    def _finalize(self, job: dict, data: dict, usage, cost: float) -> dict:
        """Keep only matches whose capability exists and whose evidence appears verbatim
        (whitespace/markdown-insensitive) in the posting, as the reference scorer requires."""
        description = _norm(job["description"])
        valid, rejected = [], []
        best: dict[str, dict] = {}
        for match in data.get("capability_matches", []):
            evidence = _norm(match.get("evidence", ""))
            if match.get("capability_id") not in self.capability_ids:
                rejected.append({**match, "reason": "unknown capability"})
            elif not evidence or evidence not in description:
                rejected.append({**match, "reason": "evidence not found in posting"})
            else:
                match["confidence"] = max(0.0, min(1.0, float(match.get("confidence", 0))))
                current = best.get(match["capability_id"])
                if current is None or match["confidence"] > current["confidence"]:
                    best[match["capability_id"]] = match
        valid = sorted(best.values(), key=lambda m: -m["confidence"])
        names = {c["id"]: c["name"] for c in self.capabilities}
        for match in valid:
            match["capability_name"] = names.get(match["capability_id"], match["capability_id"])
        return {
            "model": getattr(usage, "model", None) or self.model,
            "prompt_version": PROMPT_VERSION,
            "summary": data.get("summary", ""),
            "seniority": data.get("seniority"),
            "technologies": data.get("technologies", []),
            "skills": data.get("engineering_skills", []),
            "responsibilities": data.get("responsibilities", []),
            "matches": valid,
            "rejected_matches": rejected,
            "personas": data.get("suggested_personas", []),
            "caveats": data.get("caveats", []),
            "confidence": max(0.0, min(1.0, float(data.get("confidence", 0)))),
            "capability_score": capability_score(valid),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cost_usd": cost,
        }
