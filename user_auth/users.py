"""User records and password checking behind the API's login.

Storage follows the same switch the Zoho package uses: an in-process dict by
default, MongoDB when you ask for it. `USER_STORE=mongo` plus MONGODB_URI is the
whole difference, and nothing above this module knows which one it has.

What the in-memory backend costs while it is in use: accounts live in one
process, so two web workers cannot see each other's users and a restart or a
redeploy drops everyone. Fine for development, not for anything real -- which is
why it is a switch rather than a replacement.

On hashing: this uses `bcrypt` directly rather than passlib. passlib 1.7.4 (the
last release, from 2020) probes its bcrypt backend with a 73-byte test secret,
and bcrypt 5.x raises on anything over 72 bytes instead of truncating -- so
`CryptContext(schemes=["bcrypt"])` throws ValueError the first time you call it.
"""

import base64
import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

import bcrypt
from dotenv import load_dotenv

load_dotenv()

USERS_COLLECTION = "users"


class UserError(Exception):
    """Something was wrong with a user record or the request to create one."""


class UsernameTaken(UserError):
    """Registration hit an existing username."""


@dataclass(frozen=True)
class User:
    """A user as the rest of the app sees them -- deliberately no password hash."""

    user_id: str
    username: str
    disabled: bool = False
    created_at: datetime | None = None


# -- password hashing ------------------------------------------------------


def _prepare(password: str) -> bytes:
    """
    Reduce a password of any length to a fixed 44 bytes for bcrypt.

    bcrypt only reads the first 72 bytes of its input. Older versions truncated
    silently, which quietly makes every passphrase sharing a 72-byte prefix the
    same password; bcrypt 5.x raises instead. Hashing to SHA-256 first and
    base64-ing the digest means the whole passphrase contributes, and the input
    is always well under the limit.
    """
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # A malformed hash in the store is a corrupt record, not a valid login.
        return False


# A real hash of a throwaway password, used to burn the same ~100ms on a missing
# username as on a wrong password. Without it, response time tells an attacker
# which usernames exist.
_DUMMY_HASH = hash_password(uuid.uuid4().hex)


# -- storage ---------------------------------------------------------------


class _InMemoryUsers:
    """The two lookups this module needs, backed by a dict."""

    def __init__(self) -> None:
        self._by_username: dict[str, dict] = {}
        self._by_id: dict[str, dict] = {}
        self._lock = threading.RLock()

    def insert(self, doc: dict) -> None:
        with self._lock:
            if doc["username"] in self._by_username:
                raise UsernameTaken(f"Username {doc['username']!r} is already taken.")
            self._by_username[doc["username"]] = doc
            self._by_id[doc["_id"]] = doc

    def by_username(self, username: str) -> dict | None:
        with self._lock:
            doc = self._by_username.get(username)
            return dict(doc) if doc else None

    def by_id(self, user_id: str) -> dict | None:
        with self._lock:
            doc = self._by_id.get(user_id)
            return dict(doc) if doc else None


class _MongoUsers:
    """The same two lookups against a real collection."""

    def __init__(self, collection) -> None:
        self._c = collection
        # Two concurrent registrations for one name would both pass a
        # find-then-insert check; only the index actually prevents the duplicate.
        self._c.create_index("username", unique=True)

    def insert(self, doc: dict) -> None:
        from pymongo.errors import DuplicateKeyError

        try:
            self._c.insert_one(doc)
        except DuplicateKeyError as exc:
            raise UsernameTaken(f"Username {doc['username']!r} is already taken.") from exc

    def by_username(self, username: str) -> dict | None:
        return self._c.find_one({"username": username})

    def by_id(self, user_id: str) -> dict | None:
        return self._c.find_one({"_id": user_id})


@lru_cache(maxsize=1)
def get_store():
    """The user store this process reads and writes through."""
    backend = os.getenv("USER_STORE", "memory").strip().lower()
    if backend not in {"memory", "mongo"}:
        raise RuntimeError(f"USER_STORE must be 'memory' or 'mongo', not {backend!r}.")
    if backend == "memory":
        return _InMemoryUsers()

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("USER_STORE=mongo requires MONGODB_URI.")

    from pymongo import MongoClient

    # Same short timeout as zoho/config.py: pymongo's 30s default means an
    # unreachable cluster hangs a login for half a minute before failing.
    client = MongoClient(
        uri,
        tz_aware=True,
        serverSelectionTimeoutMS=int(os.getenv("MONGODB_TIMEOUT_MS", "5000")),
        connectTimeoutMS=int(os.getenv("MONGODB_TIMEOUT_MS", "5000")),
    )
    db = client[os.getenv("MONGODB_DB_NAME", "ai_desktop")]
    return _MongoUsers(db[USERS_COLLECTION])


# -- the operations the routes call ----------------------------------------


def _to_user(doc: dict) -> User:
    return User(
        user_id=doc["_id"],
        username=doc["username"],
        disabled=doc.get("disabled", False),
        created_at=doc.get("created_at"),
    )


def create_user(username: str, password: str) -> User:
    """
    Register a new account.

    Raises:
        UsernameTaken: if the name is already in use.
    """
    username = username.strip().lower()
    doc = {
        # Our own id rather than a Mongo ObjectId, so the value that ends up in
        # the token's `sub` claim is a plain JSON-safe string on both backends.
        "_id": uuid.uuid4().hex,
        "username": username,
        "password_hash": hash_password(password),
        "created_at": datetime.now(timezone.utc),
        "disabled": False,
    }
    get_store().insert(doc)
    return _to_user(doc)


def authenticate(username: str, password: str) -> User | None:
    """Return the user if the password checks out, otherwise None."""
    doc = get_store().by_username(username.strip().lower())
    if doc is None:
        # Still do the work, so a missing username and a wrong password take the
        # same amount of time.
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, doc["password_hash"]):
        return None
    if doc.get("disabled"):
        return None
    return _to_user(doc)


def get_user(user_id: str) -> User | None:
    """Look up the user a token's `sub` claim points at."""
    doc = get_store().by_id(user_id)
    return _to_user(doc) if doc else None
