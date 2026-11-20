"""Minimal JSON-over-HTTPS transport (stdlib only, so the Lambda zip has no deps)."""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Optional

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, method: str, url: str, status: int, body: str):
        # Strip the query string from the message: it can carry auth codes.
        safe_url = url.split("?", 1)[0]
        super().__init__(f"{method} {safe_url} -> HTTP {status}: {body[:500]}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class Response:
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body

    def json(self) -> Any:
        if not self.body:
            return {}
        return json.loads(self.body)


def basic_auth(user: str, password: str) -> str:
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


class HttpTransport:
    """Small wrapper around urllib with retry/backoff on 429/5xx and network errors."""

    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._sleep = sleep

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
        form: Optional[Mapping[str, str]] = None,
        retry: bool = True,
    ) -> Response:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, safe=",")
        hdrs = {"Accept": "application/json"}
        hdrs.update(headers or {})
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"

        attempts = (self.retries + 1) if retry else 1
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return Response(resp.status, resp.read().decode("utf-8", errors="replace"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code in RETRYABLE_STATUS and attempt < attempts:
                    self._backoff(method, url, attempt, f"HTTP {exc.code}")
                    continue
                raise ApiError(method, url, exc.code, body) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < attempts:
                    self._backoff(method, url, attempt, str(exc))
                    continue
                raise ApiError(method, url, 0, str(exc)) from None
        raise AssertionError("unreachable")

    def _backoff(self, method: str, url: str, attempt: int, why: str) -> None:
        delay = self.backoff * (2 ** (attempt - 1))
        log.warning("%s %s failed (%s); retry %d in %.1fs", method, url.split("?", 1)[0], why, attempt, delay)
        self._sleep(delay)


class ClientCredentialsToken:
    """OAuth2 client-credentials token with in-memory caching (reused across warm Lambda invocations)."""

    def __init__(
        self,
        http: HttpTransport,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: Optional[str] = None,
        auth_style: str = "basic",
        clock: Callable[[], float] = time.time,
    ):
        if auth_style not in ("basic", "post"):
            raise ValueError("auth_style must be 'basic' or 'post'")
        self.http = http
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.auth_style = auth_style
        self._clock = clock
        self._token: Optional[str] = None
        self._expires_at = 0.0

    def get(self) -> str:
        if self._token and self._clock() < self._expires_at - 60:
            return self._token
        form = {"grant_type": "client_credentials"}
        if self.scope:
            form["scope"] = self.scope
        headers = {}
        if self.auth_style == "basic":
            headers["Authorization"] = basic_auth(self.client_id, self.client_secret)
        else:
            form["client_id"] = self.client_id
            form["client_secret"] = self.client_secret
        payload = self.http.request("POST", self.token_url, headers=headers, form=form).json()
        token = payload.get("access_token")
        if not token:
            raise ApiError("POST", self.token_url, 200, "no access_token in token response")
        self._token = token
        self._expires_at = self._clock() + float(payload.get("expires_in", 900))
        return token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0
