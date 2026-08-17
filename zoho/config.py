"""Configuration and shared resources for the Zoho integration.

Settings are read lazily (not at import time) so that importing anything under
`zoho.` never explodes on a machine that hasn't set the credentials yet -- the
error shows up when someone actually tries to talk to Zoho, and names exactly
which variables are missing.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

# Zoho is region-partitioned: an org created in the EU can only be authorized
# and called through the EU domains, and its tokens are meaningless everywhere
# else. Zoho tells us the region in the `location` query param it sends back to
# the redirect URI, so we key off that per user instead of hardcoding one DC.
ACCOUNTS_DOMAINS = {
    "us": "https://accounts.zoho.com",
    "eu": "https://accounts.zoho.eu",
    "in": "https://accounts.zoho.in",
    "au": "https://accounts.zoho.com.au",
    "jp": "https://accounts.zoho.jp",
    "uk": "https://accounts.zoho.uk",
    "ca": "https://accounts.zohocloud.ca",
    "sa": "https://accounts.zoho.sa",
    "cn": "https://accounts.zoho.com.cn",
}

# Only used as a fallback -- the token response carries an `api_domain` field,
# which is the authoritative answer for where that user's API calls go.
API_DOMAINS = {
    "us": "https://www.zohoapis.com",
    "eu": "https://www.zohoapis.eu",
    "in": "https://www.zohoapis.in",
    "au": "https://www.zohoapis.com.au",
    "jp": "https://www.zohoapis.jp",
    "uk": "https://www.zohoapis.uk",
    "ca": "https://www.zohoapis.ca",
    "sa": "https://www.zohoapis.sa",
    "cn": "https://www.zohoapis.com.cn",
}

# Everything the CRM agent's tools need. Scopes are granted once at consent
# time, so asking for a narrower set later means sending every user back
# through OAuth -- but asking for more than you use is what gets an app
# rejected in review. This set maps 1:1 to the tools in crm/tools.py.
DEFAULT_CRM_SCOPES = (
    "ZohoCRM.modules.ALL",
    "ZohoCRM.settings.modules.READ",
    "ZohoCRM.settings.fields.READ",
    "ZohoCRM.settings.related_lists.READ",
    "ZohoCRM.users.READ",
    "ZohoCRM.org.READ",
    "ZohoCRM.coql.READ",
)


@dataclass(frozen=True)
class ZohoSettings:
    # Only needed for the redirect OAuth flow, where this app owns the Zoho
    # client. In the self-client flow each user brings their own credentials,
    # so these stay empty.
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    # "memory" keeps connections in-process; "mongo" persists them. See store.py.
    store_backend: str
    mongodb_uri: str | None
    mongodb_db_name: str = "ai_desktop"
    default_region: str = "us"
    api_version: str = "v8"
    token_encryption_key: str | None = None
    allow_writes: bool = True
    request_timeout: float = 30.0
    max_retries: int = 3
    session_ttl_seconds: int = 43200
    db_timeout_ms: int = 5000
    scopes: tuple[str, ...] = field(default=DEFAULT_CRM_SCOPES)

    @property
    def persistent(self) -> bool:
        return self.store_backend == "mongo"

    def require_app_credentials(self) -> tuple[str, str, str]:
        """Assert this deployment registered its own Zoho client (redirect flow only)."""
        if not (self.client_id and self.client_secret and self.redirect_uri):
            raise RuntimeError(
                "The redirect OAuth flow needs ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and "
                "ZOHO_REDIRECT_URI in the environment. If each user brings their own "
                "credentials instead, use zoho.auth.session_auth.connect()."
            )
        return self.client_id, self.client_secret, self.redirect_uri

    @property
    def default_accounts_domain(self) -> str:
        return ACCOUNTS_DOMAINS.get(self.default_region, ACCOUNTS_DOMAINS["us"])

    @property
    def default_api_domain(self) -> str:
        return API_DOMAINS.get(self.default_region, API_DOMAINS["us"])


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> ZohoSettings:
    """
    Load and validate Zoho settings once per process.

    Nothing is required by default: the store runs in memory and the
    self-client flow gets Zoho credentials from each user at connect time.
    Setting ZOHO_STORE=mongo is what makes MONGODB_URI mandatory.
    """
    store_backend = os.getenv("ZOHO_STORE", "memory").strip().lower()
    if store_backend not in {"memory", "mongo"}:
        raise RuntimeError(f"ZOHO_STORE must be 'memory' or 'mongo', not {store_backend!r}.")
    if store_backend == "mongo" and not os.getenv("MONGODB_URI"):
        raise RuntimeError("ZOHO_STORE=mongo requires MONGODB_URI -- see zoho/README.md.")

    region = os.getenv("ZOHO_DEFAULT_REGION", "us").strip().lower()
    if region not in ACCOUNTS_DOMAINS:
        raise RuntimeError(
            f"ZOHO_DEFAULT_REGION={region!r} is not a Zoho data centre. "
            f"Valid values: {', '.join(sorted(ACCOUNTS_DOMAINS))}."
        )

    scopes_raw = os.getenv("ZOHO_CRM_SCOPES")
    scopes = (
        tuple(s.strip() for s in scopes_raw.split(",") if s.strip())
        if scopes_raw
        else DEFAULT_CRM_SCOPES
    )

    return ZohoSettings(
        client_id=os.getenv("ZOHO_CLIENT_ID") or None,
        client_secret=os.getenv("ZOHO_CLIENT_SECRET") or None,
        redirect_uri=os.getenv("ZOHO_REDIRECT_URI") or None,
        store_backend=store_backend,
        mongodb_uri=os.getenv("MONGODB_URI") or None,
        mongodb_db_name=os.getenv("MONGODB_DB_NAME", "ai_desktop"),
        default_region=region,
        api_version=os.getenv("ZOHO_CRM_API_VERSION", "v8"),
        token_encryption_key=os.getenv("ZOHO_TOKEN_ENCRYPTION_KEY"),
        allow_writes=_env_bool("ZOHO_CRM_ALLOW_WRITES", True),
        request_timeout=float(os.getenv("ZOHO_REQUEST_TIMEOUT", "30")),
        max_retries=int(os.getenv("ZOHO_MAX_RETRIES", "3")),
        session_ttl_seconds=int(os.getenv("ZOHO_SESSION_TTL_SECONDS", "43200")),
        db_timeout_ms=int(os.getenv("MONGODB_TIMEOUT_MS", "5000")),
        scopes=scopes,
    )


@lru_cache(maxsize=1)
def get_db():
    """
    The store the auth modules read and write through.

    Returns an in-process stand-in by default, or a real MongoDB database when
    ZOHO_STORE=mongo. Both expose the same small collection API, so no caller
    needs to know which one it has.
    """
    settings = get_settings()
    if not settings.persistent:
        from zoho.store import InMemoryDatabase

        return InMemoryDatabase()

    # MongoClient connects lazily, so an unreachable cluster isn't discovered
    # until the first real query -- and pymongo's 30s default means a UI that
    # reruns on every interaction freezes for half a minute before saying so.
    client = MongoClient(
        settings.mongodb_uri,
        tz_aware=True,
        serverSelectionTimeoutMS=settings.db_timeout_ms,
        connectTimeoutMS=settings.db_timeout_ms,
    )
    return client[settings.mongodb_db_name]


def describe_db_error(exc: Exception) -> str:
    """Turn a pymongo connection failure into one line a user can act on."""
    text = str(exc)
    if "TLSV1_ALERT_INTERNAL_ERROR" in text or "SSL handshake failed" in text:
        return (
            "MongoDB refused the TLS handshake. On Atlas this almost always means the "
            "cluster is paused or deleted -- check cloud.mongodb.com and resume it, and "
            "confirm your IP is on the Network Access list."
        )
    if "Authentication failed" in text or "bad auth" in text:
        return "MongoDB rejected the credentials in MONGODB_URI (wrong username or password)."
    if "ServerSelectionTimeoutError" in type(exc).__name__ or "timed out" in text:
        return (
            "MongoDB was unreachable before the timeout. Check the cluster is running and "
            "that your IP is on the Atlas Network Access list."
        )
    return f"MongoDB error: {text[:200]}"
