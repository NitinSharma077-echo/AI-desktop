"""JWTs for this app's own users -- minting them at login, verifying them on the way back in.

Built on python-jose, which is what requirements.txt already declares under
"# Authentication" (PyJWT is not installed). Importing `jose` rather than `jwt`
also means this module can never accidentally import itself -- see the filename
note at the end of this docstring.

Wiring the caller's identity into the Zoho router:

    from fastapi import FastAPI
    from user_auth.jwt import current_user_id
    from zoho.routes import build_zoho_router

    app = FastAPI()
    app.include_router(build_zoho_router(current_user_id))

Settings are read lazily, following zoho/config.py: importing this module on a
machine that has not set JWT_SECRET_KEY yet is fine, and the error shows up when
someone actually tries to mint or verify a token, naming the missing variable.

On the filename: this file is `user_auth/jwt.py`, so running it as a script
(`python user_auth/jwt.py`) puts `user_auth/` on sys.path, and from that point a
plain `import jwt` anywhere in the process finds *this* file instead of the real
library. Nothing here does that, but it is why the demo in encode.py imports
`user_auth.jwt` by its full dotted path and is run with `-m` from the project
root. Rename this module to tokens.py if that trap ever costs you an afternoon.
"""

import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError, JWTError

load_dotenv()

# Access tokens are short-lived and sent on every request; refresh tokens are
# long-lived and sent only to the refresh endpoint. They are told apart by the
# `typ` claim, which is checked on decode -- without it a stolen refresh token
# would be accepted as an access token for its entire multi-week lifetime.
ACCESS_TOKEN = "access"
REFRESH_TOKEN = "refresh"

# HMAC only. RS256 and friends would work with python-jose, but they need a key
# pair rather than the single shared secret this module is built around, so
# accepting them here would just produce a confusing failure at signing time.
SUPPORTED_ALGORITHMS = ("HS256", "HS384", "HS512")

# Anything under this is brute-forceable offline: an attacker with one expired
# token can grind the secret and then mint tokens for any user, forever.
MIN_SECRET_LENGTH = 32


class TokenError(Exception):
    """A token was malformed, expired, signed by someone else, or the wrong kind."""


@dataclass(frozen=True)
class JWTSettings:
    secret_key: str
    algorithm: str
    access_ttl: timedelta
    refresh_ttl: timedelta
    # Both optional. Set them once you have more than one service signing with
    # related secrets -- `aud`/`iss` are what stop a token minted for another
    # service being replayed against this one.
    issuer: str | None = None
    audience: str | None = None
    # Server clocks drift. Without a little slack, a token minted on one box is
    # briefly "not yet valid" on another.
    leeway_seconds: int = 10


@lru_cache(maxsize=1)
def get_settings() -> JWTSettings:
    """Load and validate the JWT settings once per process."""
    secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError(
            "JWT_SECRET_KEY is not set. Generate one with\n"
            '    python -c "import secrets; print(secrets.token_urlsafe(64))"\n'
            "and put it in .env. Anyone who learns it can mint a valid token for "
            "any user, so keep it out of git and use a different value per "
            "environment."
        )
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET_KEY is only {len(secret)} characters; HMAC signing needs at "
            f"least {MIN_SECRET_LENGTH} to resist an offline brute force. Generate one "
            'with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
        )

    algorithm = os.getenv("JWT_ALGORITHM", "HS256").strip().upper()
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise RuntimeError(
            f"JWT_ALGORITHM={algorithm!r} is not supported. "
            f"Valid values: {', '.join(SUPPORTED_ALGORITHMS)}."
        )

    return JWTSettings(
        secret_key=secret,
        algorithm=algorithm,
        access_ttl=timedelta(minutes=int(os.getenv("JWT_ACCESS_TOKEN_MINUTES", "30"))),
        refresh_ttl=timedelta(days=int(os.getenv("JWT_REFRESH_TOKEN_DAYS", "14"))),
        issuer=os.getenv("JWT_ISSUER") or None,
        audience=os.getenv("JWT_AUDIENCE") or None,
        leeway_seconds=int(os.getenv("JWT_LEEWAY_SECONDS", "10")),
    )


def _mint(subject: str, token_type: str, ttl: timedelta, extra_claims: dict | None = None) -> str:
    """
    Sign one token.

    A JWT payload is base64url, not encryption: every claim in here is readable
    by anyone holding the token. Put identifiers and roles in it, never
    passwords, API keys, or anything you would not print in a log.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)

    claims: dict = {
        "sub": str(subject),
        "typ": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        # A unique id per token, so a logout or "revoke all sessions" feature has
        # something to blacklist. Nothing reads it yet; issuing it now means old
        # tokens are still revocable when that feature arrives.
        "jti": uuid.uuid4().hex,
    }
    if settings.issuer:
        claims["iss"] = settings.issuer
    if settings.audience:
        claims["aud"] = settings.audience

    if extra_claims:
        # Silently letting a caller pass sub="admin" or exp=<far future> through
        # the custom-claims kwargs would be a privilege-escalation bug.
        clashes = sorted(set(claims) & set(extra_claims))
        if clashes:
            raise TokenError(
                f"Custom claims cannot overwrite the reserved claims: {', '.join(clashes)}."
            )
        claims.update(extra_claims)

    return jwt.encode(claims, settings.secret_key, algorithm=settings.algorithm)


def create_access_token(subject: str, *, expires_in: timedelta | None = None, **extra_claims) -> str:
    """
    Mint the short-lived token the client sends on every request.

    Args:
        subject: The user id this token speaks for. Ends up in the `sub` claim.
        expires_in: Override the configured lifetime. Keep it short -- an access
            token cannot be recalled before it expires.
        **extra_claims: Extra public claims, e.g. `role="admin"`. Readable by the
            client, so nothing secret.
    """
    ttl = expires_in if expires_in is not None else get_settings().access_ttl
    return _mint(subject, ACCESS_TOKEN, ttl, extra_claims)


def create_refresh_token(subject: str, *, expires_in: timedelta | None = None) -> str:
    """
    Mint the long-lived token used only to obtain new access tokens.

    Carries no custom claims on purpose: roles and permissions should be re-read
    at refresh time, otherwise a demoted user keeps their old role until this
    token expires.
    """
    ttl = expires_in if expires_in is not None else get_settings().refresh_ttl
    return _mint(subject, REFRESH_TOKEN, ttl)


def decode_token(token: str, *, expected_type: str | None = ACCESS_TOKEN) -> dict:
    """
    Verify a token's signature and reserved claims, and return its payload.

    Args:
        token: The encoded JWT, without the "Bearer " prefix.
        expected_type: Reject the token unless its `typ` matches. Pass None to
            accept either kind (only useful for debugging).

    Raises:
        TokenError: For every rejection reason, so callers have one thing to
            catch and the caller never has to know python-jose's exceptions.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.secret_key,
            # Explicitly listing the algorithm is the fix for alg-confusion
            # attacks: without it, a token whose header claims "alg": "none"
            # would be taken at its word.
            algorithms=[settings.algorithm],
            audience=settings.audience,
            issuer=settings.issuer,
            options={
                # python-jose verifies exp when present but does not require it.
                # A token minted without one would otherwise be valid forever.
                "require_exp": True,
                "require_sub": True,
                "leeway": settings.leeway_seconds,
            },
        )
    except ExpiredSignatureError as exc:
        # Distinguishable from the rest so the client knows to refresh rather
        # than send the user back to the login screen.
        raise TokenError("Token has expired.") from exc
    except JWTClaimsError as exc:
        raise TokenError(f"Token claims are not valid for this app: {exc}") from exc
    except JWTError as exc:
        # Deliberately vague: telling a caller whether the signature or the
        # encoding was wrong hands them a decryption oracle.
        raise TokenError("Token is malformed or its signature does not match.") from exc

    if expected_type is not None and claims.get("typ") != expected_type:
        raise TokenError(
            f"Expected a token of type {expected_type!r}, "
            f"got {claims.get('typ', 'an untyped token')!r}."
        )
    return claims


def verify_access_token(token: str) -> str:
    """Verify an access token and return the user id it speaks for."""
    return decode_token(token, expected_type=ACCESS_TOKEN)["sub"]


def refresh_access_token(refresh_token: str) -> str:
    """
    Trade a valid refresh token for a fresh access token.

    The refresh token itself is not rotated here. Rotating it (and revoking the
    old `jti`) is what turns a stolen refresh token into a detectable event, so
    add that once there is a store to record revocations in.
    """
    claims = decode_token(refresh_token, expected_type=REFRESH_TOKEN)
    return create_access_token(claims["sub"])


# -- FastAPI wiring --------------------------------------------------------

# OAuth2PasswordBearer rather than HTTPBearer purely for the docs: naming the
# token URL is what makes Swagger UI's Authorize button show a username/password
# form and fill the header in for you. On the wire the two are identical -- both
# read `Authorization: Bearer <token>`.
#
# auto_error=False so a missing header lands in our handler below and gets the
# same shape of 401 as a malformed one, instead of FastAPI's own bare 403.
_bearer_scheme = OAuth2PasswordBearer(tokenUrl="auth/token", auto_error=False)

# Every request runs as this user while authentication is switched off, so a
# single-user session still gets its own conversation threads and CRM sessions
# rather than sharing a blank id.
DEV_USER_ID = "dev-user"


def auth_required() -> bool:
    """
    Whether endpoints demand a valid token.

    Defaults to FALSE so the system can be exercised end to end without first
    creating an account -- every request then runs as DEV_USER_ID, which still
    gets its own conversation threads and CRM sessions.

    This is a testing default, not a security posture. While it holds, chat,
    uploads, CRM and the model spend behind them are open to anyone who can
    reach the process. The sign-in UI and the token machinery are both built and
    tested, so turning this on is one variable plus a JWT_SECRET_KEY of 32+
    characters -- do that before this is reachable from anywhere but localhost.
    """
    return os.getenv("AUTH_REQUIRED", "false").strip().lower() in {"1", "true", "yes", "on"}


def describe() -> dict:
    """
    Auth configuration, safe to expose and guaranteed not to raise.

    Exists because the failure it reports is otherwise invisible until someone
    tries to log in: hashing a password needs no secret, so /auth/register
    happily returns 201, and only /auth/token discovers JWT_SECRET_KEY is
    missing -- as an unhandled RuntimeError, which FastAPI serves as a bare 500
    with no clue what went wrong. Reporting it here turns that into one line at
    startup and one field in /health. Never returns the key itself.
    """
    required = auth_required()
    try:
        get_settings()
    except RuntimeError as exc:
        # Only meaningful when auth is on: with it off, no token is ever minted
        # or checked, so a missing secret changes nothing.
        return {
            "required": required,
            "ready": not required,
            "detail": str(exc).replace("\n", " "),
        }
    return {"required": required, "ready": True, "detail": ""}


def current_user_id(token: str | None = Depends(_bearer_scheme)) -> str:
    """
    FastAPI dependency resolving the `Authorization: Bearer ...` header to a user id.

    This is the `get_user_id` that zoho.routes.build_zoho_router asks for:

        app.include_router(build_zoho_router(current_user_id))

    With AUTH_REQUIRED unset or false it hands back a fixed development identity
    without looking at the header at all -- deliberately not "accept a token if
    one happens to be present", which would make behaviour depend on what the
    caller chose to send and hide the fact that nothing is being checked.
    """
    if not auth_required():
        return DEV_USER_ID

    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verify_access_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
