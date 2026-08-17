"""Authenticated HTTP client for the Zoho CRM REST API.

One client instance is scoped to one connection. It holds a `TokenProvider`
rather than credentials, so the same client works whether the tokens came from
the redirect OAuth flow or from a user's own self-client session -- and nothing
about the caller's identity leaks between requests in a multi-user process.
"""

import logging
import random
import time

import requests

from zoho.auth.providers import TokenProvider, UserTokenProvider
from zoho.config import get_settings
from zoho.errors import ZohoAPIError, ZohoRateLimited

logger = logging.getLogger(__name__)

# Zoho answers "found nothing" with 204 and an empty body, and conditional
# reads with 304. Neither is an error, and neither has JSON to parse.
EMPTY_STATUSES = {204, 304}

RETRY_STATUSES = {429, 500, 502, 503, 504}


class ZohoCRMClient:
    """Thin transport layer: auth headers, retries, and error translation."""

    def __init__(self, provider: TokenProvider | str):
        # A bare string is read as a user id, so callers on the redirect flow
        # can keep passing one.
        self.provider = UserTokenProvider(provider) if isinstance(provider, str) else provider
        self.cache_key = self.provider.cache_key
        self.settings = get_settings()
        # A Session per client rather than a shared module-level one: requests'
        # Session isn't safe to share across threads, and these are cheap and
        # short-lived (one per agent invocation).
        self.session = requests.Session()

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "ZohoCRMClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- verbs ------------------------------------------------------------

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs):
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs):
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)

    # -- core -------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        files: dict | None = None,
        absolute_url: str | None = None,
    ) -> dict | None:
        """
        Call the CRM API and return the decoded body (None for empty responses).

        `path` is relative to the versioned CRM root, e.g. "Leads" or
        "Leads/123/Notes". Pass `absolute_url` to follow a URL Zoho handed back
        (attachment downloads, for instance) instead of building one.
        """
        access_token, api_domain = self.provider.get_token()
        url = absolute_url or f"{api_domain}/crm/{self.settings.api_version}/{path.lstrip('/')}"

        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            headers = {
                "Authorization": f"Zoho-oauthtoken {access_token}",
                "Accept": "application/json",
            }
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                    files=files,
                    timeout=self.settings.request_timeout,
                )
            except requests.RequestException as exc:
                if attempt <= self.settings.max_retries:
                    time.sleep(self._backoff(attempt))
                    continue
                raise ZohoAPIError(f"Zoho CRM request failed: {exc}") from exc

            # An access token can die before its stored expiry -- an admin
            # revoking the app, or a scope change. Refresh once and retry
            # before deciding the user is actually unauthorized.
            if response.status_code == 401 and not refreshed:
                refreshed = True
                access_token, api_domain = self.provider.force_refresh()
                if not absolute_url:
                    url = f"{api_domain}/crm/{self.settings.api_version}/{path.lstrip('/')}"
                continue

            if response.status_code in RETRY_STATUSES and attempt <= self.settings.max_retries:
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "Zoho CRM %s %s -> %s, retrying in %.1fs (attempt %s/%s)",
                    method,
                    path,
                    response.status_code,
                    delay,
                    attempt,
                    self.settings.max_retries,
                )
                time.sleep(delay)
                continue

            return self._handle(response, method, path)

    def _handle(self, response: requests.Response, method: str, path: str) -> dict | None:
        if response.status_code in EMPTY_STATUSES:
            return None

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.ok:
            return payload

        code = (payload or {}).get("code")
        message = (payload or {}).get("message") or response.text[:300]
        details = (payload or {}).get("details") or {}

        if response.status_code == 429 or code == "TOO_MANY_REQUESTS":
            raise ZohoRateLimited(
                f"Zoho CRM rate limit hit on {method} {path}: {message}",
                retry_after=self._parse_retry_after(response),
                status_code=response.status_code,
                code=code,
                details=details,
            )

        raise ZohoAPIError(
            f"Zoho CRM {method} {path} failed ({response.status_code}): {message}",
            status_code=response.status_code,
            code=code,
            details=details,
        )

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        retry_after = self._parse_retry_after(response)
        if retry_after is not None:
            return min(retry_after, 60.0)
        return self._backoff(attempt)

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> float | None:
        """Read whichever cooldown hint Zoho attached, if any."""
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        # On credit exhaustion Zoho sends a reset timestamp in milliseconds
        # instead of a Retry-After.
        reset = response.headers.get("X-RATELIMIT-RESET")
        if reset:
            try:
                return max(0.0, float(reset) / 1000.0 - time.time())
            except ValueError:
                pass
        return None

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with jitter, so retrying workers don't resynchronize."""
        return min(2.0 ** attempt, 30.0) * (0.5 + random.random() / 2)
