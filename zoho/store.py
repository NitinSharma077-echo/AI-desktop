"""An in-process stand-in for the MongoDB collections this package uses.

The point is that `session_auth` and `zoho_oauth` don't know which backend they
have. They call `get_db()["some_collection"]` and use a small slice of the
Mongo collection API; this module implements exactly that slice in memory, so
switching to a real database later is `ZOHO_STORE=mongo` and nothing else.

What that costs while it's in use:

* Data lives in one process. Two web workers do not see each other's sessions,
  and a restart drops every connection -- users reconnect.
* Queries support only what the auth modules actually issue: equality matching
  and `$lt`. Anything else raises rather than quietly returning wrong rows.

Both are fine for development and neither is fine for production, which is why
this is a switch rather than a replacement.
"""

import threading
from datetime import datetime, timezone
from typing import Any


def _epoch(value: Any) -> float | None:
    """Normalise a stored deadline to epoch seconds, whatever type it was written as."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


class DuplicateKeyError(Exception):
    """Raised when a unique index would be violated, mirroring pymongo."""


class InMemoryCollection:
    """The subset of pymongo's Collection API used by this package."""

    def __init__(self, name: str):
        self.name = name
        self._docs: list[dict] = []
        self._lock = threading.RLock()
        self._unique_fields: list[str] = []
        self._ttl: tuple[str, int] | None = None

    # -- index declarations ----------------------------------------------

    def create_index(self, keys, unique: bool = False, expireAfterSeconds: int | None = None):
        field = keys[0][0] if isinstance(keys, list) else keys
        if expireAfterSeconds is not None:
            # Mongo's reaper is a background job; here expiry is evaluated on
            # read, which is indistinguishable to callers and needs no thread.
            self._ttl = (field, expireAfterSeconds)
        elif unique and field not in self._unique_fields:
            self._unique_fields.append(field)
        return field

    # -- matching ---------------------------------------------------------

    def _is_expired(self, doc: dict, now: float) -> bool:
        if not self._ttl:
            return False
        field, after = self._ttl
        deadline = _epoch(doc.get(field))
        return deadline is not None and now > deadline + after

    def _purge(self) -> None:
        if not self._ttl:
            return
        now = datetime.now(timezone.utc).timestamp()
        self._docs = [d for d in self._docs if not self._is_expired(d, now)]

    @staticmethod
    def _matches(doc: dict, query: dict) -> bool:
        for key, condition in query.items():
            value = doc.get(key)
            if isinstance(condition, dict):
                for operator, operand in condition.items():
                    if operator == "$lt":
                        if not (value is not None and value < operand):
                            return False
                    else:
                        raise NotImplementedError(
                            f"InMemoryCollection does not support the {operator!r} operator. "
                            "Add it here, or run against MongoDB with ZOHO_STORE=mongo."
                        )
            elif value != condition:
                return False
        return True

    def _find(self, query: dict) -> dict | None:
        self._purge()
        for doc in self._docs:
            if self._matches(doc, query):
                return doc
        return None

    # -- the operations the auth modules call -----------------------------

    def find_one(self, query: dict, projection: dict | None = None) -> dict | None:
        with self._lock:
            doc = self._find(query)
            return dict(doc) if doc else None

    def insert_one(self, document: dict) -> None:
        with self._lock:
            self._purge()
            for field in self._unique_fields:
                if field in document and self._find({field: document[field]}):
                    raise DuplicateKeyError(f"duplicate value for unique field {field!r}")
            self._docs.append(dict(document))

    def update_one(self, query: dict, update: dict, upsert: bool = False) -> None:
        with self._lock:
            changes = update.get("$set", {})
            doc = self._find(query)
            if doc is not None:
                doc.update(changes)
            elif upsert:
                created = {k: v for k, v in query.items() if not isinstance(v, dict)}
                created.update(changes)
                self._docs.append(created)

    def find_one_and_update(self, query: dict, update: dict) -> dict | None:
        """Returns the document as it was *before* the update, like pymongo's default."""
        with self._lock:
            doc = self._find(query)
            if doc is None:
                return None
            before = dict(doc)
            doc.update(update.get("$set", {}))
            return before

    def find_one_and_delete(self, query: dict) -> dict | None:
        with self._lock:
            doc = self._find(query)
            if doc is None:
                return None
            self._docs.remove(doc)
            return dict(doc)

    def delete_one(self, query: dict) -> None:
        with self._lock:
            doc = self._find(query)
            if doc is not None:
                self._docs.remove(doc)

    def count_documents(self, query: dict, limit: int | None = None) -> int:
        with self._lock:
            self._purge()
            total = sum(1 for d in self._docs if self._matches(d, query))
            return min(total, limit) if limit else total


class InMemoryDatabase:
    """Hands out collections by name, the way a pymongo Database does."""

    def __init__(self):
        self._collections: dict[str, InMemoryCollection] = {}
        self._lock = threading.Lock()

    def __getitem__(self, name: str) -> InMemoryCollection:
        with self._lock:
            if name not in self._collections:
                self._collections[name] = InMemoryCollection(name)
            return self._collections[name]

    def clear(self) -> None:
        """Drop everything. Used by tests."""
        with self._lock:
            self._collections.clear()
