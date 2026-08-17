import os
import time
import secrets
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
GITHUB_REDIRECT_URI = os.environ["GITHUB_REDIRECT_URI"]

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
DEFAULT_SCOPE = "repo read:user offline_access"

mongo_client = MongoClient(os.environ["MONGODB_URI"])
db = mongo_client[os.getenv("MONGODB_DB_NAME", "ai_desktop")]
tokens_collection = db["github_oauth_tokens"]


def generate_oauth_url(scope: str = DEFAULT_SCOPE) -> tuple[str, str]:
    """
    Build the GitHub authorization URL for a user to visit.

    Returns (url, state). Persist `state` yourself (e.g. in the user's web
    session) and check it against the `state` query param GitHub sends back
    to the redirect URI, to guard against CSRF. `offline_access` in the scope
    is what makes GitHub issue a refresh token alongside the access token.
    """
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "scope": scope,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}", state


def exchange_code(code: str) -> dict:
    """Exchange an authorization code (from the redirect callback) for a token payload."""
    response = requests.post(
        TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": GITHUB_REDIRECT_URI,
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise ValueError(f"GitHub OAuth error: {payload.get('error_description', payload['error'])}")
    return payload


def store_tokens(user_id: str, token_payload: dict) -> None:
    """Persist a token payload (from exchange_code or refresh_tokens) in MongoDB, keyed by user_id."""
    now = time.time()
    record = {
        "user_id": user_id,
        "access_token": token_payload["access_token"],
        "refresh_token": token_payload.get("refresh_token"),
        "scope": token_payload.get("scope", ""),
        "access_token_expires_at": (
            now + token_payload["expires_in"] if "expires_in" in token_payload else None
        ),
        "refresh_token_expires_at": (
            now + token_payload["refresh_token_expires_in"]
            if "refresh_token_expires_in" in token_payload
            else None
        ),
        "updated_at": now,
    }
    tokens_collection.update_one({"user_id": user_id}, {"$set": record}, upsert=True)


def get_tokens(user_id: str) -> dict | None:
    """Fetch the stored token record for a user, or None if there isn't one."""
    return tokens_collection.find_one({"user_id": user_id}, {"_id": 0})


def refresh_tokens(user_id: str) -> dict:
    """
    Use the stored refresh token to get a new access/refresh token pair, and
    persist the result. GitHub invalidates the old access token AND the old
    refresh token as soon as this is called (rotation) -- the new refresh
    token from the response is what must be stored and used next time.
    """
    record = get_tokens(user_id)
    if not record or not record.get("refresh_token"):
        raise ValueError(f"No refresh token stored for user {user_id!r}")

    response = requests.post(
        TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": record["refresh_token"],
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise ValueError(f"GitHub OAuth refresh error: {payload.get('error_description', payload['error'])}")

    store_tokens(user_id, payload)
    return payload


def validate_tokens(user_id: str) -> bool:
    """
    Check whether the stored access token is present, unexpired locally, and
    actually still valid according to GitHub (an app owner can revoke a
    token, or a user can revoke app access, without our local expiry ever
    catching it).
    """
    record = get_tokens(user_id)
    if not record:
        return False

    expires_at = record.get("access_token_expires_at")
    if expires_at is not None and time.time() >= expires_at:
        return False

    response = requests.post(
        f"https://api.github.com/applications/{GITHUB_CLIENT_ID}/token",
        auth=(GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET),
        json={"access_token": record["access_token"]},
        headers={"Accept": "application/vnd.github+json"},
        timeout=10,
    )
    return response.status_code == 200
