"""Exception types for the Zoho integration.

These exist so callers can tell apart the three failure modes that need
different handling in a multi-user app: "this user never connected Zoho"
(send them through OAuth), "our tokens are bad" (re-auth), and "Zoho said no"
(surface the message, maybe retry).
"""


class ZohoError(Exception):
    """Base class for every error raised by this package."""


class ZohoNotConnected(ZohoError):
    """The user has no usable Zoho connection -- they need to run the OAuth flow."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(
            f"User {user_id!r} has not connected a Zoho account. "
            "Send them through the Zoho OAuth flow first."
        )


class ZohoAuthError(ZohoError):
    """An OAuth token exchange, refresh, or revoke call failed."""


class ZohoAPIError(ZohoError):
    """A CRM API call returned a non-success response.

    `code` is Zoho's machine-readable error code (INVALID_DATA,
    MANDATORY_NOT_FOUND, ...), `details` is its per-field breakdown -- both are
    far more useful to the agent than the HTTP status alone.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: dict | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.code:
            parts.append(f"(code={self.code})")
        if self.details:
            parts.append(f"details={self.details}")
        return " ".join(parts)


class ZohoRateLimited(ZohoAPIError):
    """Zoho rejected the call for exceeding API credits or concurrency limits."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs):
        self.retry_after = retry_after
        super().__init__(message, **kwargs)
