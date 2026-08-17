"""Typed wrappers over the Zoho CRM v8 REST endpoints.

This layer knows the shape of the API and nothing about agents: each function
takes a `ZohoCRMClient` and returns plain Python. The agent tools in `tools.py`
are a thin presentation layer on top.

Two Zoho behaviours are worth knowing before reading further:

* From v7 onward `GET /{module}` requires an explicit `fields` list. When a
  caller doesn't supply one we fill it in from module metadata (cached), so the
  agent doesn't have to describe every module before it can read from it.
* Write calls return HTTP 200 even when individual records fail -- the real
  outcome is per-record inside `data[]`. `summarize_write` unpacks that.
"""

import time

from zoho.client import ZohoCRMClient
from zoho.errors import ZohoAPIError

# Zoho rejects an unbounded field list, and dumping 200 fields per record into
# an LLM's context is wasteful anyway.
MAX_DEFAULT_FIELDS = 50
MAX_PER_PAGE = 200

_field_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_FIELD_CACHE_TTL = 900


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------


def get_modules(client: ZohoCRMClient) -> list[dict]:
    """List the org's modules, including custom ones."""
    payload = client.get("settings/modules") or {}
    return [
        {
            "api_name": m.get("api_name"),
            "display_name": m.get("plural_label") or m.get("module_name"),
            "supports_create": m.get("creatable"),
            "supports_edit": m.get("editable"),
            "supports_delete": m.get("deletable"),
            "is_custom": m.get("generated_type") == "custom",
        }
        for m in payload.get("modules", [])
        if m.get("api_supported")
    ]


def get_fields(client: ZohoCRMClient, module: str) -> list[dict]:
    """Field definitions for a module, cached per (connection, module) for 15 minutes."""
    key = (client.cache_key, module)
    cached = _field_cache.get(key)
    if cached and time.time() - cached[0] < _FIELD_CACHE_TTL:
        return cached[1]

    payload = client.get("settings/fields", params={"module": module}) or {}
    fields = [
        {
            "api_name": f.get("api_name"),
            "label": f.get("field_label"),
            "data_type": f.get("data_type"),
            "required": f.get("system_mandatory", False),
            "read_only": f.get("read_only", False),
            "picklist_values": [
                v.get("actual_value") for v in (f.get("pick_list_values") or [])
            ][:30]
            or None,
        }
        for f in payload.get("fields", [])
    ]
    _field_cache[key] = (time.time(), fields)
    return fields


def default_fields(client: ZohoCRMClient, module: str) -> list[str]:
    """A sensible `fields` list for a module when the caller didn't name any."""
    names = [f["api_name"] for f in get_fields(client, module) if f.get("api_name")]
    return names[:MAX_DEFAULT_FIELDS] or ["id"]


def get_users(client: ZohoCRMClient, user_type: str = "AllUsers") -> list[dict]:
    """CRM users -- needed to resolve an owner name to the id assignments require."""
    payload = client.get("users", params={"type": user_type}) or {}
    return [
        {
            "id": u.get("id"),
            "name": u.get("full_name"),
            "email": u.get("email"),
            "role": (u.get("role") or {}).get("name"),
            "profile": (u.get("profile") or {}).get("name"),
            "status": u.get("status"),
        }
        for u in payload.get("users", [])
    ]


def get_org(client: ZohoCRMClient) -> dict:
    payload = client.get("org") or {}
    orgs = payload.get("org") or [{}]
    return orgs[0]


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def list_records(
    client: ZohoCRMClient,
    module: str,
    *,
    fields: list[str] | None = None,
    page: int = 1,
    per_page: int = 50,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> dict:
    params = {
        "fields": ",".join(fields or default_fields(client, module)),
        "page": page,
        "per_page": min(per_page, MAX_PER_PAGE),
    }
    if sort_by:
        params["sort_by"] = sort_by
    if sort_order:
        params["sort_order"] = sort_order

    payload = client.get(module, params=params) or {}
    return {"records": payload.get("data", []), "info": payload.get("info", {})}


def get_record(
    client: ZohoCRMClient, module: str, record_id: str, fields: list[str] | None = None
) -> dict | None:
    params = {"fields": ",".join(fields or default_fields(client, module))}
    payload = client.get(f"{module}/{record_id}", params=params) or {}
    records = payload.get("data", [])
    return records[0] if records else None


def search_records(
    client: ZohoCRMClient,
    module: str,
    *,
    criteria: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    word: str | None = None,
    fields: list[str] | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """
    Search a module. Exactly one of criteria/email/phone/word applies.

    `criteria` uses Zoho's own grammar, e.g.
    `(Last_Name:equals:Smith)` or `((City:equals:Pune)and(Annual_Revenue:greater_than:100000))`.
    """
    if not any([criteria, email, phone, word]):
        raise ValueError("search_records needs one of: criteria, email, phone, word")

    params = {
        "fields": ",".join(fields or default_fields(client, module)),
        "page": page,
        "per_page": min(per_page, MAX_PER_PAGE),
    }
    for name, value in (("criteria", criteria), ("email", email), ("phone", phone), ("word", word)):
        if value:
            params[name] = value

    payload = client.get(f"{module}/search", params=params) or {}
    return {"records": payload.get("data", []), "info": payload.get("info", {})}


def coql(client: ZohoCRMClient, select_query: str) -> dict:
    """
    Run a COQL query -- Zoho's SQL-like read API.

    This is the right tool for filtering, aggregating and joining beyond what
    module search expresses, e.g.
    `select Deal_Name, Amount, Stage from Deals where Stage != 'Closed Won' limit 20`.
    COQL caps results at 200 rows per call and 2000 via offset paging.
    """
    payload = client.post("coql", json={"select_query": select_query}) or {}
    return {"records": payload.get("data", []), "info": payload.get("info", {})}


def related_records(
    client: ZohoCRMClient,
    module: str,
    record_id: str,
    related_list: str,
    *,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """Fetch a related list (Deals under an Account, Contacts under a Deal, ...)."""
    payload = (
        client.get(
            f"{module}/{record_id}/{related_list}",
            params={"page": page, "per_page": min(per_page, MAX_PER_PAGE)},
        )
        or {}
    )
    return {"records": payload.get("data", []), "info": payload.get("info", {})}


def list_notes(client: ZohoCRMClient, module: str, record_id: str, per_page: int = 50) -> list[dict]:
    payload = (
        client.get(f"{module}/{record_id}/Notes", params={"per_page": min(per_page, MAX_PER_PAGE)})
        or {}
    )
    return payload.get("data", [])


def list_attachments(
    client: ZohoCRMClient, module: str, record_id: str, per_page: int = 50
) -> list[dict]:
    payload = (
        client.get(
            f"{module}/{record_id}/Attachments", params={"per_page": min(per_page, MAX_PER_PAGE)}
        )
        or {}
    )
    return payload.get("data", [])


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def create_records(
    client: ZohoCRMClient, module: str, records: list[dict], trigger: list[str] | None = None
) -> dict:
    """
    Create up to 100 records.

    `trigger` controls which automation fires -- pass `[]` to suppress
    workflows, approvals and blueprints entirely, or omit it to let them run.
    """
    body: dict = {"data": records}
    if trigger is not None:
        body["trigger"] = trigger
    return summarize_write(client.post(module, json=body), "create")


def update_records(client: ZohoCRMClient, module: str, records: list[dict]) -> dict:
    """Update records. Every entry must carry its `id`."""
    missing = [i for i, r in enumerate(records) if not r.get("id")]
    if missing:
        raise ValueError(f"update_records: entries at positions {missing} have no 'id'")
    return summarize_write(client.put(module, json={"data": records}), "update")


def upsert_records(
    client: ZohoCRMClient,
    module: str,
    records: list[dict],
    duplicate_check_fields: list[str] | None = None,
) -> dict:
    """Insert or update depending on whether a match exists on the given fields."""
    body: dict = {"data": records}
    if duplicate_check_fields:
        body["duplicate_check_fields"] = duplicate_check_fields
    return summarize_write(client.post(f"{module}/upsert", json=body), "upsert")


def delete_records(client: ZohoCRMClient, module: str, ids: list[str], wf_trigger: bool = False) -> dict:
    payload = client.delete(
        module, params={"ids": ",".join(ids), "wf_trigger": str(wf_trigger).lower()}
    )
    return summarize_write(payload, "delete")


def convert_lead(
    client: ZohoCRMClient,
    lead_id: str,
    *,
    assign_to: str | None = None,
    notify_lead_owner: bool = False,
    notify_new_entity_owner: bool = False,
    deal: dict | None = None,
) -> dict:
    """
    Convert a lead into an account/contact, optionally creating a deal.

    `deal` is a record body for the new Deal (Deal_Name, Closing_Date and Stage
    are mandatory when present).
    """
    entry: dict = {
        "notify_lead_owner": notify_lead_owner,
        "notify_new_entity_owner": notify_new_entity_owner,
    }
    if assign_to:
        entry["assign_to"] = assign_to
    if deal:
        entry["Deals"] = deal

    payload = client.post(f"Leads/{lead_id}/actions/convert", json={"data": [entry]}) or {}
    results = payload.get("data", [])
    if results and results[0].get("status") == "error":
        raise ZohoAPIError(
            f"Lead conversion failed: {results[0].get('message')}",
            code=results[0].get("code"),
            details=results[0].get("details"),
        )
    return results[0] if results else {}


def add_note(
    client: ZohoCRMClient, module: str, record_id: str, title: str, content: str
) -> dict:
    body = {"data": [{"Note_Title": title, "Note_Content": content}]}
    return summarize_write(client.post(f"{module}/{record_id}/Notes", json=body), "add note")


def upload_attachment(
    client: ZohoCRMClient, module: str, record_id: str, filename: str, content: bytes
) -> dict:
    payload = client.post(
        f"{module}/{record_id}/Attachments", files={"file": (filename, content)}
    )
    return summarize_write(payload, "upload attachment")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def summarize_write(payload: dict | None, action: str) -> dict:
    """
    Flatten Zoho's per-record write response into successes and failures.

    Zoho returns 200 for a batch where some rows failed validation, so callers
    that only check the HTTP status silently lose data. Everything downstream
    reads `failed` to decide whether the command actually worked.
    """
    if not payload:
        return {"action": action, "succeeded": [], "failed": [], "raw_empty": True}

    succeeded, failed = [], []
    for entry in payload.get("data", []):
        if entry.get("status") == "success":
            succeeded.append(
                {
                    "id": (entry.get("details") or {}).get("id"),
                    "message": entry.get("message"),
                }
            )
        else:
            failed.append(
                {
                    "code": entry.get("code"),
                    "message": entry.get("message"),
                    "details": entry.get("details"),
                }
            )

    return {
        "action": action,
        "succeeded": succeeded,
        "failed": failed,
        "summary": f"{len(succeeded)} succeeded, {len(failed)} failed",
    }
