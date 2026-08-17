"""LangChain tools that expose Zoho CRM to the agent.

Two design decisions carry the multi-user requirement:

* **Identity comes from the run config, never from module state.** Every tool
  declares a `config: RunnableConfig` parameter -- LangChain injects it and
  hides it from the model's schema -- and reads `session_id` / `user_id` from
  it. One agent instance therefore serves every user concurrently without a
  request from one ever picking up another's tokens.
* **Complex arguments are JSON strings, not nested dicts.** Gemini and small
  local models produce far more reliable calls against flat string/int schemas
  than against nested object schemas, and a malformed string is recoverable
  (the tool returns a parse error the model can fix) where a rejected schema is
  not.

Tools return JSON strings and never raise: an error the model can read is worth
more than a traceback that kills the run.

Note what is deliberately absent: there is no tool for connecting a Zoho
account. Client secrets and grant codes are handed to `session_auth.connect()`
by your application code, so they never enter the model's context and never get
written to a checkpoint.
"""

import json

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from zoho.auth import session_auth, zoho_oauth
from zoho.auth.providers import SessionTokenProvider, UserTokenProvider
from zoho.client import ZohoCRMClient
from zoho.config import get_settings
from zoho.crm import api
from zoho.errors import ZohoError, ZohoNotConnected

# Keeps a single tool result from swallowing the model's context window.
MAX_RESULT_CHARS = 12000


def _provider(config: RunnableConfig | None):
    """
    Pick the connection this run acts through.

    A live self-client session wins over a stored user connection: if the user
    pasted credentials for this chat, that is the org they mean, even if they
    also linked an account through the redirect flow earlier.
    """
    cfg = (config or {}).get("configurable") or {}
    session_id = cfg.get("session_id")
    user_id = cfg.get("user_id")

    if session_id and session_auth.has_session(str(session_id)):
        return SessionTokenProvider(str(session_id))
    if user_id:
        return UserTokenProvider(str(user_id))
    if session_id:
        raise ZohoNotConnected(str(session_id))
    raise ZohoError(
        "No Zoho connection in the run config. Invoke the agent with "
        'config={"configurable": {"session_id": ...}} after connecting, or '
        '{"user_id": ...} for the redirect OAuth flow.'
    )


def _dump(value) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        return (
            text[:MAX_RESULT_CHARS]
            + f"... [truncated at {MAX_RESULT_CHARS} chars -- narrow the fields or lower per_page]"
        )
    return text


def _run(config: RunnableConfig | None, operation) -> str:
    """Resolve the caller's client, run `operation`, and render the outcome as JSON."""
    try:
        provider = _provider(config)
    except ZohoNotConnected:
        return _dump(
            {
                "error": "not_connected",
                "message": "This chat has no Zoho connection. Ask the user to connect "
                "Zoho CRM, then retry.",
            }
        )
    except ZohoError as exc:
        return _dump({"error": "no_user_context", "message": str(exc)})

    try:
        with ZohoCRMClient(provider) as client:
            return _dump(operation(client))
    except ZohoNotConnected:
        return _dump(
            {
                "error": "not_connected",
                "message": "The Zoho connection for this chat has expired or been closed. "
                "Ask the user to reconnect, then retry.",
            }
        )
    except ZohoError as exc:
        return _dump({"error": type(exc).__name__, "message": str(exc)})
    except (ValueError, TypeError) as exc:
        return _dump({"error": "bad_arguments", "message": str(exc)})


def _writes_allowed() -> str | None:
    if not get_settings().allow_writes:
        return _dump(
            {
                "error": "writes_disabled",
                "message": "This deployment is read-only (ZOHO_CRM_ALLOW_WRITES=false). "
                "Report the intended change to the user instead of performing it.",
            }
        )
    return None


def _parse_json(raw: str, label: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc


def _field_list(fields: str) -> list[str] | None:
    names = [f.strip() for f in (fields or "").split(",") if f.strip()]
    return names or None


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@tool
def crm_connection_status(config: RunnableConfig = None) -> str:
    """Check whether this chat has a live Zoho CRM connection, and to which org.

    Call this first if another tool reports 'not_connected'.
    """
    cfg = (config or {}).get("configurable") or {}
    session_id, user_id = cfg.get("session_id"), cfg.get("user_id")
    try:
        if session_id and session_auth.has_session(str(session_id)):
            return _dump({"auth": "session", **session_auth.status(str(session_id))})
        if user_id:
            return _dump({"auth": "user", **zoho_oauth.connection_status(str(user_id))})
        return _dump({"connected": False, "message": "No Zoho connection for this chat."})
    except ZohoError as exc:
        return _dump({"error": type(exc).__name__, "message": str(exc)})


@tool
def crm_list_modules(config: RunnableConfig = None) -> str:
    """List the CRM modules available in this org, including custom modules.

    Use this when you are unsure of a module's API name (e.g. whether deals are
    called 'Deals' or something custom). Module API names are case-sensitive.
    """
    return _run(config, lambda client: api.get_modules(client))


@tool
def crm_describe_module(module: str, config: RunnableConfig = None) -> str:
    """List a module's fields: API name, label, data type, whether required, picklist values.

    Call this before creating or updating records in a module you have not
    written to yet -- Zoho rejects payloads that use field labels ('Last Name')
    instead of API names ('Last_Name').

    Args:
        module: Module API name, e.g. 'Leads', 'Contacts', 'Deals'.
    """
    return _run(config, lambda client: api.get_fields(client, module))


@tool
def crm_list_users(user_type: str = "AllUsers", config: RunnableConfig = None) -> str:
    """List CRM users with their ids, emails and roles.

    Use this to resolve a person's name to the user id that record ownership
    and lead assignment require.

    Args:
        user_type: One of AllUsers, ActiveUsers, DeactiveUsers, AdminUsers.
    """
    return _run(config, lambda client: api.get_users(client, user_type))


@tool
def crm_org_info(config: RunnableConfig = None) -> str:
    """Get the connected org's details: company name, id, currency, licence plan."""
    return _run(config, lambda client: api.get_org(client))


# --------------------------------------------------------------------------
# Reading records
# --------------------------------------------------------------------------


@tool
def crm_list_records(
    module: str,
    fields: str = "",
    page: int = 1,
    per_page: int = 50,
    sort_by: str = "",
    sort_order: str = "",
    config: RunnableConfig = None,
) -> str:
    """List records from a module, newest first by default.

    For anything involving a filter, a condition, or a count, prefer crm_query
    (COQL) over listing everything and filtering yourself.

    Args:
        module: Module API name, e.g. 'Leads'.
        fields: Comma-separated field API names. Empty means a default subset.
        page: 1-based page number.
        per_page: Records per page, max 200.
        sort_by: Field API name to sort on, e.g. 'Created_Time'.
        sort_order: 'asc' or 'desc'.
    """
    return _run(
        config,
        lambda client: api.list_records(
            client,
            module,
            fields=_field_list(fields),
            page=page,
            per_page=per_page,
            sort_by=sort_by or None,
            sort_order=sort_order or None,
        ),
    )


@tool
def crm_get_record(
    module: str, record_id: str, fields: str = "", config: RunnableConfig = None
) -> str:
    """Fetch one record by its Zoho id.

    Args:
        module: Module API name, e.g. 'Contacts'.
        record_id: The record's Zoho id (a long numeric string).
        fields: Comma-separated field API names. Empty means a default subset.
    """
    return _run(
        config,
        lambda client: api.get_record(client, module, record_id, fields=_field_list(fields)),
    )


@tool
def crm_search_records(
    module: str,
    criteria: str = "",
    email: str = "",
    phone: str = "",
    word: str = "",
    fields: str = "",
    per_page: int = 50,
    config: RunnableConfig = None,
) -> str:
    """Find records in one module by criteria, email, phone, or free text.

    Supply exactly one of criteria/email/phone/word.

    Criteria grammar is Zoho's own, with parentheses required around each
    condition:
        (Last_Name:equals:Smith)
        ((City:equals:Pune)and(Annual_Revenue:greater_than:100000))
    Operators: equals, not_equal, contains, starts_with, greater_than,
    less_than, greater_equal, less_equal, between, in.

    Args:
        module: Module API name.
        criteria: Zoho criteria expression, as above.
        email: Match any email field exactly.
        phone: Match any phone field exactly.
        word: Free-text match across the record.
        fields: Comma-separated field API names to return.
        per_page: Max records to return, up to 200.
    """
    return _run(
        config,
        lambda client: api.search_records(
            client,
            module,
            criteria=criteria or None,
            email=email or None,
            phone=phone or None,
            word=word or None,
            fields=_field_list(fields),
            per_page=per_page,
        ),
    )


@tool
def crm_query(select_query: str, config: RunnableConfig = None) -> str:
    """Run a COQL query -- Zoho's SQL-like read API. The best tool for reporting questions.

    Rules that differ from SQL: the field list must be explicit (no `select *`),
    field API names are case-sensitive, string literals use single quotes, and
    every query needs a `where` clause. Max 200 rows per call; use `limit` and
    `offset` to page.

        select Full_Name, Email, Lead_Status from Leads where Lead_Status = 'Not Contacted' limit 20
        select Deal_Name, Amount, Stage, Closing_Date from Deals
            where Closing_Date between '2026-01-01' and '2026-03-31' order by Amount desc limit 50
        select count(id) from Contacts where Created_Time > '2026-01-01T00:00:00+05:30'

    Args:
        select_query: The COQL statement.
    """
    return _run(config, lambda client: api.coql(client, select_query))


@tool
def crm_related_records(
    module: str,
    record_id: str,
    related_list: str,
    per_page: int = 50,
    config: RunnableConfig = None,
) -> str:
    """List records related to one record, e.g. the Deals under an Account.

    Args:
        module: Parent module API name, e.g. 'Accounts'.
        record_id: Parent record id.
        related_list: Related list API name, e.g. 'Deals', 'Contacts', 'Tasks'.
        per_page: Max records to return, up to 200.
    """
    return _run(
        config,
        lambda client: api.related_records(
            client, module, record_id, related_list, per_page=per_page
        ),
    )


@tool
def crm_list_notes(
    module: str, record_id: str, per_page: int = 50, config: RunnableConfig = None
) -> str:
    """Read the notes attached to a record.

    Args:
        module: Module API name.
        record_id: Record id.
        per_page: Max notes to return.
    """
    return _run(config, lambda client: api.list_notes(client, module, record_id, per_page))


@tool
def crm_list_attachments(
    module: str, record_id: str, per_page: int = 50, config: RunnableConfig = None
) -> str:
    """List files attached to a record, with their ids, names and sizes.

    Args:
        module: Module API name.
        record_id: Record id.
        per_page: Max attachments to return.
    """
    return _run(config, lambda client: api.list_attachments(client, module, record_id, per_page))


# --------------------------------------------------------------------------
# Writing records
# --------------------------------------------------------------------------


@tool
def crm_create_records(
    module: str, records_json: str, suppress_workflows: bool = False, config: RunnableConfig = None
) -> str:
    """Create one or more records. Returns per-record success/failure.

    Use crm_describe_module first if you are unsure of field API names or which
    fields are mandatory (Leads need Last_Name; Deals need Deal_Name, Stage and
    Closing_Date; Accounts need Account_Name).

    Args:
        module: Module API name, e.g. 'Leads'.
        records_json: JSON array of record objects keyed by field API name, e.g.
            '[{"Last_Name": "Sharma", "Company": "Acme", "Email": "a@acme.com"}]'
        suppress_workflows: True to stop workflows, approvals and blueprints
            from firing for this write.
    """
    blocked = _writes_allowed()
    if blocked:
        return blocked

    try:
        records = _parse_json(records_json, "records_json")
    except ValueError as exc:
        return _dump({"error": "bad_arguments", "message": str(exc)})
    if isinstance(records, dict):
        records = [records]

    return _run(
        config,
        lambda client: api.create_records(
            client, module, records, trigger=[] if suppress_workflows else None
        ),
    )


@tool
def crm_update_record(
    module: str, record_id: str, updates_json: str, config: RunnableConfig = None
) -> str:
    """Update fields on one existing record.

    Only the fields you pass are changed; everything else is left alone.

    Args:
        module: Module API name.
        record_id: The record's Zoho id.
        updates_json: JSON object of field API names to new values, e.g.
            '{"Lead_Status": "Contacted", "Phone": "+91-9876543210"}'
    """
    blocked = _writes_allowed()
    if blocked:
        return blocked

    try:
        updates = _parse_json(updates_json, "updates_json")
    except ValueError as exc:
        return _dump({"error": "bad_arguments", "message": str(exc)})
    if not isinstance(updates, dict):
        return _dump({"error": "bad_arguments", "message": "updates_json must be a JSON object"})

    return _run(
        config,
        lambda client: api.update_records(client, module, [{**updates, "id": record_id}]),
    )


@tool
def crm_upsert_records(
    module: str, records_json: str, match_on: str = "", config: RunnableConfig = None
) -> str:
    """Insert records, updating instead of duplicating when a match already exists.

    Use this for imports and syncs where you do not know whether the record is
    already in CRM.

    Args:
        module: Module API name.
        records_json: JSON array of record objects keyed by field API name.
        match_on: Comma-separated field API names to match duplicates on, e.g.
            'Email'. Empty uses the module's configured duplicate check fields.
    """
    blocked = _writes_allowed()
    if blocked:
        return blocked

    try:
        records = _parse_json(records_json, "records_json")
    except ValueError as exc:
        return _dump({"error": "bad_arguments", "message": str(exc)})
    if isinstance(records, dict):
        records = [records]

    return _run(
        config,
        lambda client: api.upsert_records(
            client, module, records, duplicate_check_fields=_field_list(match_on)
        ),
    )


@tool
def crm_delete_records(
    module: str, record_ids: str, confirmed_by_user: bool = False, config: RunnableConfig = None
) -> str:
    """Delete records by id. Destructive and not undoable through the API.

    Only set confirmed_by_user=True after the user has explicitly approved
    deleting these specific records in their message. Never infer approval.

    Args:
        module: Module API name.
        record_ids: Comma-separated record ids.
        confirmed_by_user: Whether the user explicitly approved this deletion.
    """
    blocked = _writes_allowed()
    if blocked:
        return blocked

    ids = [i.strip() for i in record_ids.split(",") if i.strip()]
    if not ids:
        return _dump({"error": "bad_arguments", "message": "record_ids was empty"})
    if not confirmed_by_user:
        return _dump(
            {
                "error": "confirmation_required",
                "message": f"Deleting {len(ids)} record(s) from {module} needs explicit user "
                "approval. Show the user what would be deleted and ask before retrying.",
                "record_ids": ids,
            }
        )

    return _run(config, lambda client: api.delete_records(client, module, ids))


@tool
def crm_convert_lead(
    lead_id: str,
    deal_name: str = "",
    deal_stage: str = "",
    deal_closing_date: str = "",
    deal_amount: float = 0.0,
    assign_to_user_id: str = "",
    config: RunnableConfig = None,
) -> str:
    """Convert a lead into an account and contact, optionally creating a deal.

    Leave the deal_* arguments empty to convert without creating a deal. If you
    create one, deal_name, deal_stage and deal_closing_date are all required.

    Args:
        lead_id: The lead's Zoho id.
        deal_name: Name for the new deal.
        deal_stage: Deal stage, e.g. 'Qualification'.
        deal_closing_date: Expected close date as YYYY-MM-DD.
        deal_amount: Deal value; 0 to omit.
        assign_to_user_id: User id to own the converted records; empty keeps the
            lead's current owner.
    """
    blocked = _writes_allowed()
    if blocked:
        return blocked

    deal = None
    if deal_name or deal_stage or deal_closing_date:
        if not (deal_name and deal_stage and deal_closing_date):
            return _dump(
                {
                    "error": "bad_arguments",
                    "message": "Creating a deal during conversion needs deal_name, deal_stage "
                    "and deal_closing_date together.",
                }
            )
        deal = {
            "Deal_Name": deal_name,
            "Stage": deal_stage,
            "Closing_Date": deal_closing_date,
        }
        if deal_amount:
            deal["Amount"] = deal_amount

    return _run(
        config,
        lambda client: api.convert_lead(
            client, lead_id, assign_to=assign_to_user_id or None, deal=deal
        ),
    )


@tool
def crm_add_note(
    module: str, record_id: str, title: str, content: str, config: RunnableConfig = None
) -> str:
    """Attach a note to a record -- the usual way to log a call, meeting or decision.

    Args:
        module: Module API name.
        record_id: Record id.
        title: Short note title.
        content: Note body.
    """
    blocked = _writes_allowed()
    if blocked:
        return blocked
    return _run(config, lambda client: api.add_note(client, module, record_id, title, content))


CRM_TOOLS = [
    crm_connection_status,
    crm_list_modules,
    crm_describe_module,
    crm_list_users,
    crm_org_info,
    crm_list_records,
    crm_get_record,
    crm_search_records,
    crm_query,
    crm_related_records,
    crm_list_notes,
    crm_list_attachments,
    crm_create_records,
    crm_update_record,
    crm_upsert_records,
    crm_delete_records,
    crm_convert_lead,
    crm_add_note,
]

READ_ONLY_TOOL_NAMES = {
    "crm_connection_status",
    "crm_list_modules",
    "crm_describe_module",
    "crm_list_users",
    "crm_org_info",
    "crm_list_records",
    "crm_get_record",
    "crm_search_records",
    "crm_query",
    "crm_related_records",
    "crm_list_notes",
    "crm_list_attachments",
}


def get_crm_tools(read_only: bool = False) -> list:
    """The CRM toolset, optionally narrowed to read-only tools."""
    if read_only:
        return [t for t in CRM_TOOLS if t.name in READ_ONLY_TOOL_NAMES]
    return list(CRM_TOOLS)
