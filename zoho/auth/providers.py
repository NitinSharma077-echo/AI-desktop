"""Token providers -- the seam between the two auth flows and the HTTP client.

`ZohoCRMClient` only needs to know how to get a token and how to force a fresh
one. Putting that behind a tiny interface means adding a third flow later
(service accounts, a shared org connection) touches nothing but this file.
"""

from typing import Protocol, runtime_checkable

from zoho.auth import session_auth, zoho_oauth


@runtime_checkable
class TokenProvider(Protocol):
    kind: str

    @property
    def cache_key(self) -> str:
        """Identity for per-connection caches (field metadata differs per org)."""

    def get_token(self) -> tuple[str, str]:
        """Return `(access_token, api_domain)`, refreshing if near expiry."""

    def force_refresh(self) -> tuple[str, str]:
        """Force a refresh, e.g. after Zoho rejected a token with 401."""


class UserTokenProvider:
    """Redirect OAuth flow: this app owns the Zoho client, tokens persist per user."""

    kind = "user"

    def __init__(self, user_id: str):
        self.user_id = str(user_id)

    @property
    def cache_key(self) -> str:
        return f"user:{self.user_id}"

    def get_token(self) -> tuple[str, str]:
        return zoho_oauth.get_access_token(self.user_id)

    def force_refresh(self) -> tuple[str, str]:
        return zoho_oauth.refresh_access_token(self.user_id, force=True)


class SessionTokenProvider:
    """Self-client flow: the user's own credentials, scoped to one chat session."""

    kind = "session"

    def __init__(self, session_id: str):
        self.session_id = str(session_id)

    @property
    def cache_key(self) -> str:
        return f"session:{self.session_id}"

    def get_token(self) -> tuple[str, str]:
        return session_auth.get_access_token(self.session_id)

    def force_refresh(self) -> tuple[str, str]:
        return session_auth.refresh_access_token(self.session_id, force=True)
