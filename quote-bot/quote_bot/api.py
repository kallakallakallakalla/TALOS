"""HTTP client for the 5U hackathon API (mailbox + rate portal + gateway models)."""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 502, 503, 504}
DEFAULT_MAX_ATTEMPTS = 5  # used when no deadline is given
MAX_WAIT_S = 60.0


class ApiError(Exception):
    def __init__(self, status: int, path: str, detail: Any):
        super().__init__(f"{status} {path}: {detail}")
        self.status = status
        self.path = path
        self.detail = detail


def _num(x: float | int | str) -> str:
    """Format a number for query params without float noise."""
    if isinstance(x, str):
        return x
    s = f"{float(x):.3f}".rstrip("0").rstrip(".")
    return s or "0"


def _detail(r: requests.Response) -> Any:
    try:
        body = r.json()
    except ValueError:
        return r.text[:500]
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body


def _retry_after(r: requests.Response) -> float | None:
    value = r.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), MAX_WAIT_S)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return min(max(dt.timestamp() - time.time(), 0.0), MAX_WAIT_S)
    except (TypeError, ValueError):
        return None


def _backoff(attempt: int) -> float:
    return min(0.5 * 2**attempt, 8.0) + random.uniform(0, 0.3)


class FiveUClient:
    """Thread-safe client. Retries 429/5xx; `deadline` is a time.monotonic() value."""

    def __init__(self, base_url: str, api_key: str, timeout_s: float = 15.0):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout_s
        self._local = threading.local()
        self._lock = threading.Lock()
        self._blocked_until = 0.0  # shared pause after a 429
        self.on_sent = None  # callback(resp, payload) after a mail was sent
        self.rate_limit: str | None = None
        self.rate_limit_remaining: str | None = None

    # -- plumbing -----------------------------------------------------------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"X-API-Key": self.api_key, "Accept": "application/json"})
            self._local.session = s
        return s

    def _block_for(self, seconds: float) -> None:
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    def _wait_if_blocked(self) -> None:
        with self._lock:
            wait = self._blocked_until - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _can_retry(deadline: float | None, attempt: int, wait: float) -> bool:
        if deadline is None:
            return attempt + 1 < DEFAULT_MAX_ATTEMPTS
        return time.monotonic() + wait < deadline

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        deadline: float | None = None,
        raw: bool = False,
        idempotent: bool = False,
    ) -> Any:
        url = self.base + path
        attempt = 0
        while True:
            self._wait_if_blocked()
            try:
                r = self._session().request(
                    method, url, params=params, json=json_body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                wait = _backoff(attempt)
                if (method != "GET" and not idempotent) or not self._can_retry(deadline, attempt, wait):
                    raise ApiError(0, path, str(exc)) from exc
                log.warning("network error on %s (%s), retry in %.1fs", path, exc, wait)
                time.sleep(wait)
                attempt += 1
                continue

            if "X-RateLimit-Remaining" in r.headers:
                self.rate_limit = r.headers.get("X-RateLimit-Limit")
                self.rate_limit_remaining = r.headers.get("X-RateLimit-Remaining")

            retryable = RETRY_STATUSES if (method == "GET" or idempotent) else {429}  # POST: only 429
            if r.status_code in retryable:
                wait = _retry_after(r) or _backoff(attempt)
                if not self._can_retry(deadline, attempt, wait):
                    raise ApiError(r.status_code, path, _detail(r))
                log.info("%s on %s, retry in %.1fs", r.status_code, path, wait)
                if r.status_code == 429:
                    self._block_for(wait)  # pause all threads
                else:
                    time.sleep(wait)
                attempt += 1
                continue

            if r.status_code >= 400:
                raise ApiError(r.status_code, path, _detail(r))
            if raw:
                return r.content
            if not r.content:
                return None
            try:
                return r.json()
            except ValueError:
                return r.text

    # -- endpoints ----------------------------------------------------------
    def me(self) -> Any:
        return self.request("GET", "/v1/me")

    def poll(self, since_id: int = 0, limit: int = 100) -> Any:
        return self.request(
            "GET", "/v1/mailbox/messages", params={"since_id": int(since_id), "limit": int(limit)}
        )

    def message(self, message_id: int) -> Any:
        return self.request("GET", f"/v1/mailbox/messages/{int(message_id)}")

    def ports(self) -> Any:
        return self.request("GET", "/v1/portal/ports")

    def brackets(self, airline: str, deadline: float | None = None) -> Any:
        return self.request(
            "GET", "/v1/portal/brackets", params={"airline": airline}, deadline=deadline
        )

    def rate(
        self,
        origin: str,
        destination: str,
        airline: str,
        weight_kg: float,
        volume_cbm: float = 0.0,
        deadline: float | None = None,
    ) -> Any:
        params = {
            "origin": origin,
            "destination": destination,
            "airline": airline,
            "weight_kg": _num(weight_kg),
            "volume_cbm": _num(volume_cbm or 0),
        }
        return self.request("GET", "/v1/portal/rates", params=params, deadline=deadline)

    def models(self) -> Any:
        return self.request("GET", "/v1/gateway/models")

    def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: int | None = None,
        deadline: float | None = None,
    ) -> Any:
        from .maillog import log_outgoing

        payload: dict[str, Any] = {"to": to, "subject": subject[:1000], "body": body[:200000]}
        if in_reply_to is not None:
            payload["in_reply_to"] = int(in_reply_to)
        try:
            resp = self.request("POST", "/v1/mailbox/messages", json_body=payload, deadline=deadline)
        except ApiError as exc:
            log_outgoing(to, subject, body, in_reply_to, f"FAILED {exc}")
            raise
        log_outgoing(to, subject, body, in_reply_to, f"ok {json.dumps(resp, default=str)[:200]}")
        if self.on_sent:
            try:
                self.on_sent(resp, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_sent hook failed: %s", exc)
        return resp

    def book(self, rate_id: str, team_reference: str, deadline: float | None = None) -> Any:
        """POST /v1/portal/bookings (idempotent per team_reference -> safe to retry)."""
        return self.request(
            "POST", "/v1/portal/bookings",
            json_body={"rate_id": rate_id, "team_reference": team_reference[:128]},
            deadline=deadline, idempotent=True,
        )


def messages_from_poll(data: Any) -> list[dict]:
    """Poll response shape is not pinned down yet: accept list or dict wrapper."""
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    if isinstance(data, dict):
        for key in ("messages", "items", "data", "results", "mails"):
            value = data.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
    return []


def next_since_id(data: Any, messages: list[dict], current: int) -> int:
    if isinstance(data, dict) and isinstance(data.get("next_since_id"), int):
        return max(current, data["next_since_id"])
    ids = [m["id"] for m in messages if isinstance(m.get("id"), int)]
    return max([current, *ids])
