"""Register, log in, refresh, and "who am I" -- the endpoints that hand out tokens.

Mounted by api.py. Everything here is about obtaining a token; verifying one is
user_auth/jwt.py's job, and the rest of the API only ever sees `current_user_id`.
"""

import os

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from user_auth import users
from user_auth.jwt import (
    TokenError,
    create_access_token,
    create_refresh_token,
    current_user_id,
    get_settings,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _registration_open() -> bool:
    """
    Whether /auth/register accepts new accounts.

    Open by default, because otherwise there is no way to create the first user
    on a fresh deployment. Set AUTH_ALLOW_REGISTRATION=false once your account
    exists -- on a public URL an open endpoint is an invitation.
    """
    return os.getenv("AUTH_ALLOW_REGISTRATION", "true").strip().lower() in {"1", "true", "yes", "on"}


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, examples=["nitin"])
    # No maximum: users.hash_password reduces any length to a fixed 44 bytes
    # before bcrypt sees it, so long passphrases are hashed in full.
    password: str = Field(min_length=8, examples=["a-long-passphrase"])


class UserResponse(BaseModel):
    user_id: str
    username: str
    disabled: bool


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    # Lowercase "bearer" is what RFC 6750 specifies and what Swagger UI expects.
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest) -> UserResponse:
    """Create an account. Returns the user, not a token -- log in separately."""
    if not _registration_open():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is closed on this deployment.",
        )
    try:
        user = users.create_user(body.username, body.password)
    except users.UsernameTaken as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return UserResponse(user_id=user.user_id, username=user.username, disabled=user.disabled)


@router.post("/token", response_model=TokenResponse)
def login(form: OAuth2PasswordRequestForm = Depends()) -> TokenResponse:
    """
    Exchange a username and password for a token pair.

    Takes form-encoded fields rather than JSON because that is what the OAuth2
    password flow specifies -- which is also what lets Swagger UI's Authorize
    button log you in directly.
    """
    user = users.authenticate(form.username, form.password)
    if user is None:
        # One message for "no such user", "wrong password", and "disabled".
        # Distinguishing them tells an attacker which usernames are real.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    settings = get_settings()
    return TokenResponse(
        access_token=create_access_token(user.user_id, username=user.username),
        refresh_token=create_refresh_token(user.user_id),
        expires_in=int(settings.access_ttl.total_seconds()),
    )


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(body: RefreshRequest) -> AccessTokenResponse:
    """Trade a refresh token for a new access token."""
    from user_auth.jwt import decode_token

    try:
        claims = decode_token(body.refresh_token, expected_type="refresh")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Re-read the account rather than trusting the token: this is the one moment
    # a disabled or deleted user can be turned away before getting another
    # 30 minutes of access.
    user = users.get_user(claims["sub"])
    if user is None or user.disabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is no longer active."
        )

    settings = get_settings()
    return AccessTokenResponse(
        access_token=create_access_token(user.user_id, username=user.username),
        expires_in=int(settings.access_ttl.total_seconds()),
    )


@router.get("/me", response_model=UserResponse)
def me(user_id: str = Depends(current_user_id)) -> UserResponse:
    """The account the bearer token belongs to. Handy for checking a token works."""
    user = users.get_user(user_id)
    if user is None:
        # A valid signature for an account that no longer exists -- possible
        # after a restart on the in-memory store.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user.")
    return UserResponse(user_id=user.user_id, username=user.username, disabled=user.disabled)
