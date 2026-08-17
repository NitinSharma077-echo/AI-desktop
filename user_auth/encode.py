"""A runnable tour of what user_auth/jwt.py actually does to a token.

Run it from the project root:

    python -m user_auth.encode

Not `python user_auth/encode.py` -- that puts `user_auth/` on sys.path, where the
neighbouring jwt.py starts shadowing the real JWT library for the whole process.

This file is a demo, not part of the auth path. Nothing imports it.
"""

import base64
import json
import os
import secrets
from datetime import timedelta

from jose import jwt as jose_jwt

from user_auth.jwt import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_settings,
    verify_access_token,
)

# So the demo runs on a checkout with no .env. load_dotenv() has already run
# inside user_auth.jwt, so a real JWT_SECRET_KEY is present here if one is
# configured and setdefault leaves it alone.
if not os.getenv("JWT_SECRET_KEY"):
    os.environ["JWT_SECRET_KEY"] = secrets.token_urlsafe(64)
    print("No JWT_SECRET_KEY configured -- using a throwaway one for this run.\n")


def show(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 62 - len(title)))


def tamper(token: str, **changes) -> str:
    """Rewrite claims in a token's payload while keeping its original signature.

    This is the attack the signature exists to stop: the payload is base64url,
    not encryption, so anyone holding a token can read and edit it freely. What
    they cannot do is produce a matching signature without the secret.
    """
    header_b64, payload_b64, signature = token.split(".")
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    payload.update(changes)
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header_b64}.{forged}.{signature}"


settings = get_settings()

show("1. Encoding")
token = create_access_token("user-42", role="admin")
print(f"algorithm : {settings.algorithm}")
print(f"lifetime  : {settings.access_ttl}")
print(f"token     : {token}")

show("2. The three parts")
# header.payload.signature -- the first two are just base64url'd JSON.
for name, part in zip(("header", "payload", "signature"), token.split(".")):
    print(f"{name:>9} : {part}")

show("3. Reading it without the secret")
# No key involved, no verification done. Useful for debugging, never for
# deciding who the caller is -- an attacker controls every byte of this.
print("header :", jose_jwt.get_unverified_header(token))
print("claims :", jose_jwt.get_unverified_claims(token))

show("4. Verifying it properly")
claims = decode_token(token)
print("claims :", claims)
print("user id:", verify_access_token(token))

show("5. A tampered token")
forged = tamper(token, sub="user-1", role="superadmin")
print("forged claims:", jose_jwt.get_unverified_claims(forged))
try:
    verify_access_token(forged)
except TokenError as exc:
    print(f"rejected: {exc}")

show("6. A token signed with the wrong secret")
impostor = jose_jwt.encode({"sub": "user-1", "typ": "access", "exp": 9999999999}, "wrong-secret")
try:
    verify_access_token(impostor)
except TokenError as exc:
    print(f"rejected: {exc}")

show("7. An expired token")
# Past the configured leeway, otherwise clock-skew slack would still accept it.
stale = create_access_token("user-42", expires_in=timedelta(seconds=-60))
try:
    verify_access_token(stale)
except TokenError as exc:
    print(f"rejected: {exc}")

show("8. Refresh tokens are not access tokens")
refresh = create_refresh_token("user-42")
try:
    verify_access_token(refresh)
except TokenError as exc:
    print(f"rejected: {exc}")
print("but as a refresh token :", decode_token(refresh, expected_type="refresh")["sub"])

show("9. Reserved claims cannot be overridden")
try:
    create_access_token("user-42", sub="admin")
except TokenError as exc:
    print(f"rejected: {exc}")
