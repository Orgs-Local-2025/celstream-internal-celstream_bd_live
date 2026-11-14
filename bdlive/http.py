"""Shared HTTP client: timeouts, bounded retries, rate-limit handling, request logging.

Every provider call goes through `ApiClient.request`, which writes one row per
attempt to `integration_logs`. Secrets are scrubbed from any stored error text.
"""

from __future__ import annotations

import logging
import random
import re
import time
from email.utils import parsedate_to_datetime

import httpx

from bdlive import db
from bdlive.settings import settings

log = logging.getLogger("bdlive")
logging.getLogger("httpx").setLevel(logging.WARNING)  # our own integration log is canonical


class ApiError(RuntimeError):
    """Provider failure with a sanitized message. Never carries credentials."""

    def __init__(self, provider: str, category: str, message: str, status: int | None = None,
                 body=None):
        self.provider, self.category, self.status, self.body = provider, category, status, body
        super().__init__(f"{provider}: {category} (HTTP {status}) {message}".strip())


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def scrub(text: str | None, limit: int = 600) -> str:
    if not text:
        return ""
    text = str(text)
    for secret in settings().secrets():
        if secret and secret in text:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key[\"'=:\s]+)[A-Za-z0-9._-]{8,}", r"\1[REDACTED]", text)
    return text[:limit]


def _category(status: int) -> str:
    if status in (401, 403):
        return "auth_error"
    if status == 429:
        return "rate_limited"
    if status == 404:
        return "not_found"
    if status == 409:
        return "conflict"
    if status in (400, 422):
        return "bad_request"
    if status >= 500:
        return "server_error"
    return "http_error"


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def log_event(provider, operation, *, run_id=None, method=None, endpoint=None, attempt=None,
              http_status=None, success=True, duration_ms=None, entity=None, external_id=None,
              error=None):
    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO integration_logs (run_id, provider, operation, method, endpoint,"
                " attempt, http_status, success, duration_ms, entity, external_id, error,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, provider, operation, method, endpoint, attempt, http_status,
                 int(bool(success)), duration_ms, entity, external_id, scrub(error), db.now()),
            )
    except Exception:  # logging must never break the workflow
        log.exception("failed to write integration log")


class ApiClient:
    provider = "generic"
    base_url = ""
    # Mutating calls are not blindly replayed after an ambiguous failure.
    retry_mutations_on = {429}

    def __init__(self, run_id: int | None = None, min_interval: float = 0.0):
        self.run_id = run_id
        self.min_interval = min_interval
        self._last = 0.0
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(settings().http_timeout_seconds, connect=10),
            follow_redirects=False,
        )

    def headers(self) -> dict:
        return {}

    def close(self):
        self._client.close()

    def request(self, method: str, path: str, operation: str, *, params=None, json=None,
                entity: str | None = None, retries: int | None = None,
                ok_statuses: tuple[int, ...] = ()) -> dict:
        retries = settings().http_max_retries if retries is None else retries
        mutation = method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not operation.endswith(
            ("search", "enrich", "match", "list", "get")
        )
        last_error: ApiError | None = None
        for attempt in range(1, retries + 2):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            started = time.monotonic()
            self._last = started
            status = None
            try:
                response = self._client.request(
                    method, path, params=params, json=json, headers=self.headers()
                )
                status = response.status_code
                try:
                    body = response.json() if response.content else {}
                except ValueError:
                    body = {"_raw": response.text[:500]}
                duration = int((time.monotonic() - started) * 1000)
                if response.is_success or status in ok_statuses:
                    log_event(self.provider, operation, run_id=self.run_id, method=method,
                              endpoint=path, attempt=attempt, http_status=status, success=True,
                              duration_ms=duration, entity=entity)
                    return body if isinstance(body, dict) else {"data": body}
                category = _category(status)
                message = scrub(response.text, 400)
                last_error = ApiError(self.provider, category, message, status, body)
                log_event(self.provider, operation, run_id=self.run_id, method=method,
                          endpoint=path, attempt=attempt, http_status=status, success=False,
                          duration_ms=duration, entity=entity, error=f"{category}: {message}")
                retryable = status == 429 or (status >= 500 and not mutation)
                if mutation and status in self.retry_mutations_on:
                    retryable = True
                if not retryable or attempt > retries:
                    raise last_error
                delay = _retry_after(response.headers.get("Retry-After")) or (
                    2 ** attempt + random.random()
                )
                if delay > settings().max_retry_wait_seconds:
                    raise last_error
                time.sleep(delay)
            except httpx.TimeoutException:
                duration = int((time.monotonic() - started) * 1000)
                last_error = ApiError(self.provider, "timeout", "request timed out")
                log_event(self.provider, operation, run_id=self.run_id, method=method,
                          endpoint=path, attempt=attempt, success=False, duration_ms=duration,
                          entity=entity, error="timeout")
                if mutation or attempt > retries:
                    raise last_error from None
                time.sleep(2 ** attempt)
            except httpx.RequestError as exc:
                duration = int((time.monotonic() - started) * 1000)
                last_error = ApiError(self.provider, "connectivity_error", type(exc).__name__)
                log_event(self.provider, operation, run_id=self.run_id, method=method,
                          endpoint=path, attempt=attempt, success=False, duration_ms=duration,
                          entity=entity, error=f"connectivity_error: {type(exc).__name__}")
                if attempt > retries:
                    raise last_error from None
                time.sleep(2 ** attempt)
        raise last_error or ApiError(self.provider, "unknown", "request failed")
