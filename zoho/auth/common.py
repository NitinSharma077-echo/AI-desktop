"""Pieces shared by both auth flows: credential encryption and token calls.

Both `zoho_oauth` (this app owns the Zoho client) and `session_auth` (each user
brings their own) store credentials and talk to the same token endpoint, so
that logic lives here rather than being duplicated and drifting.
"""

import logging

import requests
from cryptography.fernet import Fernet, InvalidToken

from zoho.config import get_settings
from zoho.errors import ZohoAuthError

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        settings = get_settings()
        key = settings.token_encryption_key
        if not key and not settings.persistent:
            # Nothing outlives the process on the in-memory store, so a
            # throwaway key loses nothing and saves a setup step. Encryption
            # still runs, which keeps this path identical to the persistent one.
            logger.info("ZOHO_TOKEN_ENCRYPTION_KEY unset; using an ephemeral key (memory store).")
            key = Fernet.generate_key().decode()
        if not key:
            raise ZohoAuthError(
                "ZOHO_TOKEN_ENCRYPTION_KEY is required when ZOHO_STORE=mongo. Zoho refresh "
                "tokens never expire and client secrets are user-supplied, so both are "
                'encrypted before being stored. Generate a key with: python -c "from '
                'cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        try:
            _fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ZohoAuthError(
                f"ZOHO_TOKEN_ENCRYPTION_KEY is not a valid Fernet key: {exc}"
            ) from exc
    return _fernet


def encrypt(value: str | None) -> str | None:
    return _cipher().encrypt(value.encode()).decode() if value else None


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ZohoAuthError(
            "A stored Zoho credential could not be decrypted -- ZOHO_TOKEN_ENCRYPTION_KEY "
            "has changed since it was written. Affected users must reconnect."
        ) from exc


def token_request(accounts_domain: str, data: dict) -> dict:
    """
    POST to Zoho's token endpoint and unwrap its errors.

    Zoho reports OAuth failures as HTTP 200 with an `error` key, so checking the
    status code alone would let `invalid_code` sail through as success.
    """
    try:
        response = requests.post(
            f"{accounts_domain}/oauth/v2/token",
            data=data,
            headers={"Accept": "application/json"},
            timeout=get_settings().request_timeout,
        )
    except requests.RequestException as exc:
        raise ZohoAuthError(f"Could not reach Zoho accounts server: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        raise ZohoAuthError(
            f"Zoho token endpoint returned non-JSON (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        ) from None

    if response.status_code >= 400 or "error" in payload:
        raise ZohoAuthError(_explain(payload.get("error"), response.text[:300]))
    if "access_token" not in payload:
        raise ZohoAuthError(f"Zoho token response had no access_token: {payload}")
    return payload


def revoke(accounts_domain: str, refresh_token: str) -> bool:
    """Revoke a refresh token. Returns False if Zoho was unreachable."""
    try:
        requests.post(
            f"{accounts_domain}/oauth/v2/token/revoke",
            params={"token": refresh_token},
            timeout=get_settings().request_timeout,
        )
        return True
    except requests.RequestException:
        return False


def _explain(error: str | None, fallback: str) -> str:
    """Turn Zoho's terse OAuth error codes into something a user can act on."""
    hints = {
        "invalid_code": (
            "the grant code is wrong, already used, or expired. Self-client codes are "
            "single-use and last only the duration chosen in the API console (3-10 "
            "minutes) -- generate a fresh one and paste it straight away."
        ),
        "invalid_client": (
            "the client id or secret is wrong, or belongs to a different Zoho data "
            "centre than the one selected."
        ),
        "invalid_client_secret": "the client secret does not match the client id.",
        "redirect_uri_mismatch": (
            "the redirect URI does not exactly match the one registered in the API "
            "console (including scheme, port and trailing slash)."
        ),
        "invalid_scope": "one of the requested scopes is not valid for this client.",
        "access_denied": "the user declined the authorization request.",
    }
    if error and error in hints:
        return f"Zoho OAuth error '{error}': {hints[error]}"
    return f"Zoho OAuth error: {error or fallback}"
