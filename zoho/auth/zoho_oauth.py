"""Zoho OAuth 2.0 -- authorization, token storage, and refresh, per user.

Differences from the GitHub flow in `github_oauth.py` that drive the design here:

* Zoho is region-partitioned. The redirect carries `location` and
  `accounts-server` params, and the token response carries `api_domain`. All
  three must be stored per user; a token minted in the `in` DC is rejected by
  the `com` DC.
* Zoho refresh tokens do not expire and are not rotated on use. That makes them
  long-lived credentials, so they are encrypted at rest here rather than stored
  raw. Zoho also caps a user at 20 live refresh tokens per client and silently
  drops the oldest -- so re-running consent repeatedly for the same user will
  eventually invalidate their earliest tokens.
* Access tokens last one hour. With multiple app workers serving the same user,
  refreshes are guarded by a short lock in Mongo so a burst of requests doesn't
  fire a burst of refresh calls (each one burns an API credit and shortens the
  refresh-token pool above).
"""

import secrets
import time
from urllib.parse import urlencode

from pymongo import ASCENDING

from zoho.auth.common import decrypt as _decrypt
from zoho.auth.common import encrypt as _encrypt
from zoho.auth.common import revoke as _revoke
from zoho.auth.common import token_request as _token_request
from zoho.config import ACCOUNTS_DOMAINS, API_DOMAINS, get_db, get_settings
from zoho.errors import ZohoAuthError, ZohoNotConnected

# How long before actual expiry we treat an access token as stale. Covers clock
# skew plus the round trip of whatever call is about to use it.
REFRESH_MARGIN_SECONDS = 120

# How long one worker holds the right to refresh a given user's token.
REFRESH_LOCK_SECONDS = 30

STATE_TTL_SECONDS = 600

_indexes_ready = False


def _connections():
    return get_db()["zoho_connections"]


def _states():
    return get_db()["zoho_oauth_states"]


def _ensure_indexes() -> None:
    """Create indexes once per process. Mongo treats repeat calls as no-ops."""
    global _indexes_ready
    if _indexes_ready:
        return
    _connections().create_index([("user_id", ASCENDING)], unique=True)
    _states().create_index([("state", ASCENDING)], unique=True)
    # Mongo reaps abandoned OAuth attempts for us, so a user who starts consent
    # and walks away doesn't leave a usable state token lying around forever.
    _states().create_index([("created_at", ASCENDING)], expireAfterSeconds=STATE_TTL_SECONDS)
    _indexes_ready = True


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def build_authorize_url(user_id: str, scopes: tuple[str, ...] | None = None) -> str:
    """
    Build the URL to send `user_id` to in order to grant this app CRM access.

    The CSRF `state` is generated and stored server-side against the user, so
    the callback doesn't have to trust anything the browser hands back. Unlike
    the GitHub helper this returns only the URL -- there is no state for the
    caller to stash in a session.
    """
    settings = get_settings()
    client_id, _, redirect_uri = settings.require_app_credentials()
    _ensure_indexes()

    state = secrets.token_urlsafe(32)
    _states().insert_one({"state": state, "user_id": user_id, "created_at": time.time()})

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes or settings.scopes),
        # access_type=offline is what makes Zoho issue a refresh token at all,
        # and prompt=consent is what makes it issue one on *every* grant --
        # without it a user who has authorized before gets an access token only.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{settings.default_accounts_domain}/oauth/v2/auth?{urlencode(params)}"


def complete_authorization(
    code: str,
    state: str,
    location: str | None = None,
    accounts_server: str | None = None,
) -> str:
    """
    Finish the flow from the redirect callback and persist the connection.

    Pass through the `code`, `state`, `location` and `accounts-server` query
    params exactly as Zoho sent them. Returns the user_id the tokens were
    stored against. The state document is consumed atomically, so a replayed
    callback fails instead of minting a second connection.
    """
    _ensure_indexes()
    record = _states().find_one_and_delete({"state": state})
    if not record:
        raise ZohoAuthError("Invalid or expired OAuth state -- restart the Zoho connection flow.")

    user_id = record["user_id"]
    settings = get_settings()
    client_id, client_secret, redirect_uri = settings.require_app_credentials()
    region = (location or settings.default_region).strip().lower()
    accounts_domain = accounts_server or ACCOUNTS_DOMAINS.get(region, settings.default_accounts_domain)

    payload = _token_request(
        accounts_domain,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    if not payload.get("refresh_token"):
        raise ZohoAuthError(
            "Zoho returned no refresh token. The authorize URL must include "
            "access_type=offline and prompt=consent."
        )

    _store_connection(user_id, payload, region=region, accounts_domain=accounts_domain)
    return user_id


def _store_connection(user_id: str, payload: dict, *, region: str, accounts_domain: str) -> dict:
    """Persist a token payload. Only overwrites the refresh token if one was returned."""
    now = time.time()
    update = {
        "user_id": user_id,
        "access_token": _encrypt(payload["access_token"]),
        "access_token_expires_at": now + float(payload.get("expires_in", 3600)),
        "api_domain": payload.get("api_domain") or API_DOMAINS.get(region, ""),
        "accounts_domain": accounts_domain,
        "region": region,
        "scope": payload.get("scope", ""),
        "token_type": payload.get("token_type", "Zoho-oauthtoken"),
        "updated_at": now,
        "refresh_lock_until": 0.0,
    }
    # A refresh response contains no refresh_token -- Zoho reuses the original.
    # Writing None here would wipe the only long-lived credential we hold.
    if payload.get("refresh_token"):
        update["refresh_token"] = _encrypt(payload["refresh_token"])
        update["connected_at"] = now

    _connections().update_one({"user_id": user_id}, {"$set": update}, upsert=True)
    return update


# --------------------------------------------------------------------------
# Token access
# --------------------------------------------------------------------------


def get_connection(user_id: str) -> dict | None:
    """Raw stored connection for a user (tokens still encrypted), or None."""
    _ensure_indexes()
    return _connections().find_one({"user_id": user_id}, {"_id": 0})


def connection_status(user_id: str) -> dict:
    """Safe-to-display summary of a user's Zoho connection. Never returns tokens."""
    record = get_connection(user_id)
    if not record:
        return {"connected": False}
    expires_at = record.get("access_token_expires_at", 0)
    return {
        "connected": True,
        "region": record.get("region"),
        "api_domain": record.get("api_domain"),
        "scope": record.get("scope"),
        "connected_at": record.get("connected_at"),
        "access_token_expires_in": max(0, int(expires_at - time.time())),
    }


def get_access_token(user_id: str) -> tuple[str, str]:
    """
    Return `(access_token, api_domain)` for a user, refreshing if near expiry.

    Raises ZohoNotConnected if the user never linked an account.
    """
    record = get_connection(user_id)
    if not record:
        raise ZohoNotConnected(user_id)

    if time.time() < record.get("access_token_expires_at", 0) - REFRESH_MARGIN_SECONDS:
        return _decrypt(record["access_token"]), record["api_domain"]

    return refresh_access_token(user_id)


def refresh_access_token(user_id: str, force: bool = False) -> tuple[str, str]:
    """
    Exchange the stored refresh token for a fresh access token.

    Only one worker refreshes at a time: the lock is taken with a conditional
    update, so whoever loses the race waits briefly and picks up the winner's
    token instead of making a second, redundant call to Zoho.
    """
    _ensure_indexes()
    now = time.time()
    locked = _connections().find_one_and_update(
        {"user_id": user_id, "refresh_lock_until": {"$lt": now}},
        {"$set": {"refresh_lock_until": now + REFRESH_LOCK_SECONDS}},
    )

    if locked is None:
        # Either the user isn't connected, or someone else holds the lock.
        record = get_connection(user_id)
        if not record:
            raise ZohoNotConnected(user_id)
        if not force:
            fresh = _await_refresh(user_id)
            if fresh:
                return fresh
        locked = record

    refresh_token = _decrypt(locked.get("refresh_token"))
    if not refresh_token:
        raise ZohoNotConnected(user_id)

    settings = get_settings()
    client_id, client_secret, _ = settings.require_app_credentials()
    accounts_domain = locked.get("accounts_domain") or settings.default_accounts_domain
    try:
        payload = _token_request(
            accounts_domain,
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
    except ZohoAuthError:
        # Release the lock so the next request retries promptly rather than
        # waiting out the full lock window on a token that may be recoverable.
        _connections().update_one({"user_id": user_id}, {"$set": {"refresh_lock_until": 0.0}})
        raise

    stored = _store_connection(
        user_id,
        payload,
        region=locked.get("region", settings.default_region),
        accounts_domain=accounts_domain,
    )
    return payload["access_token"], stored["api_domain"]


def _await_refresh(user_id: str, timeout: float = 5.0) -> tuple[str, str] | None:
    """Poll briefly for another worker's refresh to land. None if it didn't."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        record = get_connection(user_id)
        if record and time.time() < record.get("access_token_expires_at", 0) - REFRESH_MARGIN_SECONDS:
            return _decrypt(record["access_token"]), record["api_domain"]
    return None


def disconnect(user_id: str) -> bool:
    """
    Revoke the user's refresh token at Zoho and delete the local record.

    Revoking matters beyond tidiness: each user is capped at 20 live refresh
    tokens per client, and abandoned ones count against that cap.
    """
    record = get_connection(user_id)
    if not record:
        return False

    refresh_token = _decrypt(record.get("refresh_token"))
    if refresh_token:
        # Local deletion happens either way -- leaving the row behind would let
        # the app keep acting as a user who asked to be disconnected.
        _revoke(record.get("accounts_domain") or get_settings().default_accounts_domain, refresh_token)

    _connections().delete_one({"user_id": user_id})
    return True
