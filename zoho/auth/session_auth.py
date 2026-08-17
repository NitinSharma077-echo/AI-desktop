"""Self-client auth: the user brings their own credentials, once per chat.

The flow this supports:

1. The user creates a **Self Client** at api-console.zoho.com, picks the CRM
   scopes, and generates a grant code.
2. They paste client id, client secret and that code into your app once.
3. `connect()` exchanges them for tokens, and every command for the rest of the
   chat runs against that connection.
4. `end()` revokes and deletes everything when the chat closes.

Why this is a separate module from `zoho_oauth`: there, *this app* owns one
registered Zoho client and users authorize it. Here, each user owns their own
client, so the client id and secret are per-connection data rather than
deployment config -- and the secret has to be stored (encrypted) because
refreshing an access token an hour later needs it again.

Two properties matter for safety:

* Credentials are never exposed to the model. `connect()` is called by your
  application code, not by an agent tool, so the secret and grant code never
  enter the LLM's context.
* Connections expire on their own. A Mongo TTL index reaps sessions whose chat
  was abandoned rather than closed, so credentials don't outlive their use.
"""

import time
from datetime import datetime, timedelta, timezone

from pymongo import ASCENDING

from zoho.auth.common import decrypt, encrypt, revoke, token_request
from zoho.config import ACCOUNTS_DOMAINS, API_DOMAINS, get_db, get_settings
from zoho.errors import ZohoAuthError, ZohoNotConnected

REFRESH_MARGIN_SECONDS = 120
REFRESH_LOCK_SECONDS = 30

_indexes_ready = False


def _sessions():
    return get_db()["zoho_sessions"]


def _ensure_indexes() -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    _sessions().create_index([("session_id", ASCENDING)], unique=True)
    # expireAfterSeconds=0 means "delete when the date in this field passes",
    # which lets each session carry its own deadline. Abandoned chats clean
    # themselves up instead of leaving live credentials in the database.
    _sessions().create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    _indexes_ready = True


def _deadline() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=get_settings().session_ttl_seconds)


def connect(
    session_id: str,
    client_id: str,
    client_secret: str,
    grant_code: str,
    region: str | None = None,
    accounts_domain: str | None = None,
) -> dict:
    """
    Exchange a user's self-client credentials for tokens and open a session.

    Call this once when the user submits the form. Everything afterwards keys
    off `session_id` alone.

    Args:
        session_id: Your chat/session identifier. Must be unguessable -- anyone
            holding it can act as this Zoho connection for the session's life.
        client_id: From the user's self client.
        client_secret: From the user's self client.
        grant_code: The generated code. Single-use, expires in minutes.
        region: Data centre the client belongs to (us, eu, in, au, ...).
        accounts_domain: Overrides `region` if you already know the domain.

    Returns a status dict safe to show the user. Never returns credentials.
    """
    _ensure_indexes()
    settings = get_settings()

    if not all([session_id, client_id, client_secret, grant_code]):
        raise ZohoAuthError("session_id, client_id, client_secret and grant_code are all required.")

    region = (region or settings.default_region).strip().lower()
    if region not in ACCOUNTS_DOMAINS:
        raise ZohoAuthError(
            f"{region!r} is not a Zoho data centre. Valid values: "
            f"{', '.join(sorted(ACCOUNTS_DOMAINS))}."
        )
    domain = accounts_domain or ACCOUNTS_DOMAINS[region]

    # No redirect_uri here: a self client has none registered, and sending one
    # anyway is what produces a redirect_uri_mismatch on this flow.
    payload = token_request(
        domain,
        {
            "grant_type": "authorization_code",
            "client_id": client_id.strip(),
            "client_secret": client_secret.strip(),
            "code": grant_code.strip(),
        },
    )
    if not payload.get("refresh_token"):
        raise ZohoAuthError(
            "Zoho returned no refresh token for this grant code. In the API console, "
            "generate the code from the Self Client tab with the CRM scopes selected."
        )

    now = time.time()
    record = {
        "session_id": session_id,
        "client_id": client_id.strip(),
        "client_secret": encrypt(client_secret.strip()),
        "access_token": encrypt(payload["access_token"]),
        "refresh_token": encrypt(payload["refresh_token"]),
        "access_token_expires_at": now + float(payload.get("expires_in", 3600)),
        "api_domain": payload.get("api_domain") or API_DOMAINS.get(region, ""),
        "accounts_domain": domain,
        "region": region,
        "scope": payload.get("scope", ""),
        "connected_at": now,
        "expires_at": _deadline(),
        "refresh_lock_until": 0.0,
    }
    _sessions().update_one({"session_id": session_id}, {"$set": record}, upsert=True)
    return status(session_id)


def has_session(session_id: str) -> bool:
    if not session_id:
        return False
    _ensure_indexes()
    return _sessions().count_documents({"session_id": session_id}, limit=1) > 0


def _get(session_id: str) -> dict:
    _ensure_indexes()
    record = _sessions().find_one({"session_id": session_id}, {"_id": 0})
    if not record:
        raise ZohoNotConnected(session_id)
    return record


def status(session_id: str) -> dict:
    """Safe-to-display summary of a session. Never returns credentials."""
    _ensure_indexes()
    record = _sessions().find_one({"session_id": session_id}, {"_id": 0})
    if not record:
        return {"connected": False}
    return {
        "connected": True,
        "region": record.get("region"),
        "api_domain": record.get("api_domain"),
        "scope": record.get("scope"),
        "connected_at": record.get("connected_at"),
        "session_expires_at": record.get("expires_at"),
        "access_token_expires_in": max(
            0, int(record.get("access_token_expires_at", 0) - time.time())
        ),
    }


def get_access_token(session_id: str) -> tuple[str, str]:
    """Return `(access_token, api_domain)` for a session, refreshing near expiry."""
    record = _get(session_id)

    # Each use pushes the session deadline out, so an active chat is never cut
    # off mid-conversation by the TTL while an abandoned one still expires.
    _sessions().update_one({"session_id": session_id}, {"$set": {"expires_at": _deadline()}})

    if time.time() < record.get("access_token_expires_at", 0) - REFRESH_MARGIN_SECONDS:
        return decrypt(record["access_token"]), record["api_domain"]
    return refresh_access_token(session_id)


def refresh_access_token(session_id: str, force: bool = False) -> tuple[str, str]:
    """
    Get a fresh access token using the session's stored refresh token.

    Locked the same way as the redirect flow: concurrent requests in one chat
    produce a single refresh call rather than one per request.
    """
    _ensure_indexes()
    now = time.time()
    locked = _sessions().find_one_and_update(
        {"session_id": session_id, "refresh_lock_until": {"$lt": now}},
        {"$set": {"refresh_lock_until": now + REFRESH_LOCK_SECONDS}},
    )

    if locked is None:
        record = _get(session_id)
        if not force:
            fresh = _await_refresh(session_id)
            if fresh:
                return fresh
        locked = record

    refresh_token = decrypt(locked.get("refresh_token"))
    client_secret = decrypt(locked.get("client_secret"))
    if not (refresh_token and client_secret):
        raise ZohoNotConnected(session_id)

    try:
        payload = token_request(
            locked["accounts_domain"],
            {
                "grant_type": "refresh_token",
                "client_id": locked["client_id"],
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
    except ZohoAuthError:
        _sessions().update_one({"session_id": session_id}, {"$set": {"refresh_lock_until": 0.0}})
        raise

    api_domain = payload.get("api_domain") or locked["api_domain"]
    _sessions().update_one(
        {"session_id": session_id},
        {
            "$set": {
                "access_token": encrypt(payload["access_token"]),
                "access_token_expires_at": time.time() + float(payload.get("expires_in", 3600)),
                "api_domain": api_domain,
                "expires_at": _deadline(),
                "refresh_lock_until": 0.0,
            }
        },
    )
    return payload["access_token"], api_domain


def _await_refresh(session_id: str, timeout: float = 5.0) -> tuple[str, str] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.25)
        record = _sessions().find_one({"session_id": session_id}, {"_id": 0})
        if record and time.time() < record.get("access_token_expires_at", 0) - REFRESH_MARGIN_SECONDS:
            return decrypt(record["access_token"]), record["api_domain"]
    return None


def end(session_id: str, revoke_token: bool = True) -> bool:
    """
    Close a session: revoke the refresh token at Zoho, then delete the record.

    Call this when the chat ends. Revoking matters because a self client is
    capped at 20 live refresh tokens -- a user who reconnects every chat without
    this will start seeing their earlier tokens silently invalidated.
    """
    _ensure_indexes()
    record = _sessions().find_one({"session_id": session_id}, {"_id": 0})
    if not record:
        return False

    if revoke_token:
        refresh_token = decrypt(record.get("refresh_token"))
        if refresh_token:
            revoke(record["accounts_domain"], refresh_token)

    _sessions().delete_one({"session_id": session_id})
    return True
