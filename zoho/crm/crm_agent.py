"""The Zoho CRM command agent.

The usual flow -- the user brings their own Zoho credentials once, then works
until the chat ends:

    from zoho.auth import session_auth
    from zoho.crm.crm_agent import run_command

    # Once, when the user submits the connect form:
    session_auth.connect(
        session_id="chat-42",             # unguessable; identifies this chat
        client_id="1000.XXXX",
        client_secret="...",
        grant_code="1000.abc...",         # from the Self Client tab
        region="in",
    )

    # Then, for every message in that chat:
    run_command("how many open deals do we have?", session_id="chat-42")
    run_command("mark the Acme deal closed won", session_id="chat-42")

    # When the chat closes:
    session_auth.end("chat-42")           # revokes at Zoho, then deletes

Pass `user_id=` instead of `session_id=` to run against a connection made
through the redirect OAuth flow in `zoho.auth.zoho_oauth`.

One compiled agent serves every user in the process. The caller's connection
travels in the run config rather than in the agent, so nothing about chat A is
reachable while serving chat B -- see the note at the top of `tools.py`.
"""

import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from zoho.crm.tools import get_crm_tools

SYSTEM_PROMPT = """You are a Zoho CRM operator. You carry out the user's CRM \
requests by calling the CRM tools, then report what actually happened.

Today is {today}.

How to work:
- Field and module API names are case-sensitive and often differ from the \
labels users say. When you are unsure, call crm_list_modules or \
crm_describe_module before reading or writing. Do not guess field names twice \
in a row -- look them up instead.
- For any question involving filtering, counting, ranking or date ranges, use \
crm_query (COQL). Use crm_search_records for simple lookups by name, email or \
phone. Use crm_list_records only for plain "show me the latest N" requests.
- To act on a record the user described in words ("the Acme deal"), find its id \
first, and if more than one record matches, ask which one rather than picking.
- Before creating a record, check the module's mandatory fields. If the user \
has not given you one, ask for it instead of inventing a value.
- Deletions need explicit approval: show the user exactly which records would \
go, and only pass confirmed_by_user=True after they say yes in their own words.
- Tools return JSON with per-record results. A write can come back with \
failures inside an otherwise successful response -- read the "failed" list and \
report those honestly rather than claiming success.
- If a tool reports "not_connected", stop and tell the user to connect their \
Zoho account; do not retry other tools.

How to answer:
- Lead with the result. Summarise records in a short markdown table when there \
is more than one.
- Always include record ids when the user might want to act on a record next.
- State counts exactly as the API reported them; never estimate or extrapolate.
"""


def _default_llm():
    """
    Pick a chat model for the agent.

    This agent lives or dies on tool-calling accuracy across ~18 tools with
    detailed schemas, which is a different bar from the general chat in
    `chat/chat.py` -- so Gemini is preferred here when a key is configured.

    Without one it falls back to whatever providers.py selects, rather than to a
    hardcoded local Ollama: a deployment running on OpenAI would otherwise reach
    for a localhost that isn't there and fail every /crm command. Temperature is
    pinned to 0 either way, which is why this builds a model instead of reusing
    the cached default.
    """
    if os.getenv("GOOGLE_API_KEY"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=os.getenv("ZOHO_CRM_MODEL", "gemini-2.0-flash"), temperature=0
        )

    import providers

    # A bigger local model than general chat uses, for the same tool-accuracy
    # reason -- but only meaningful when the active provider is Ollama.
    override = os.getenv("ZOHO_CRM_OLLAMA_MODEL", "qwen2.5:7b") if providers.is_local() else None
    return providers.build_chat_model(model=override, temperature=0)


def _checkpointer():
    """
    Conversation memory for follow-up questions.

    MemorySaver is per-process: with more than one web worker, a user's follow-up
    can land on a worker that never saw the earlier turn. Once the store is on
    Mongo (ZOHO_STORE=mongo) and `langgraph-checkpoint-mongodb` is installed,
    this upgrades to shared state automatically.
    """
    from zoho.config import get_db, get_settings

    if get_settings().persistent:
        try:
            from langgraph.checkpoint.mongodb import MongoDBSaver

            return MongoDBSaver(get_db(), collection_name="zoho_crm_checkpoints")
        except Exception:
            pass
    return MemorySaver()


_agent = None


def build_crm_agent(llm=None, read_only: bool = False, fresh: bool = False):
    """Build (and cache) the compiled CRM agent."""
    global _agent
    if _agent is not None and not fresh and llm is None and not read_only:
        return _agent

    agent = create_react_agent(
        model=llm or _default_llm(),
        tools=get_crm_tools(read_only=read_only),
        prompt=SYSTEM_PROMPT.format(today=date.today().isoformat()),
        checkpointer=_checkpointer(),
    )
    if llm is None and not read_only:
        _agent = agent
    return agent


def _run_config(session_id: str | None, user_id: str | None, thread_id: str | None) -> dict:
    """
    Build the per-invocation config.

    Whichever of session_id/user_id is set is what the tools authenticate as.
    Thread ids are namespaced under it so that two chats passing the same
    thread_id (say "default") can never read each other's conversation out of
    the checkpointer.
    """
    if not (session_id or user_id):
        raise ValueError(
            "run_command needs session_id= (self-client flow) or user_id= (redirect flow)"
        )
    configurable = {"thread_id": f"{session_id or user_id}:{thread_id or 'default'}"}
    if session_id:
        configurable["session_id"] = str(session_id)
    if user_id:
        configurable["user_id"] = str(user_id)
    return {
        "configurable": configurable,
        # A single CRM request can legitimately take several tool calls
        # (describe -> search -> update); the default of 25 is generous, but a
        # confused model can otherwise loop until it burns API credits.
        "recursion_limit": int(os.getenv("ZOHO_CRM_RECURSION_LIMIT", "25")),
    }


def run_command(
    command: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    agent=None,
) -> str:
    """Run one CRM command and return the agent's final reply."""
    agent = agent or build_crm_agent()
    result = agent.invoke(
        {"messages": [HumanMessage(content=command)]},
        config=_run_config(session_id, user_id, thread_id),
    )
    for message in reversed(result["messages"]):
        if isinstance(message, AIMessage) and message.content:
            return message.content if isinstance(message.content, str) else str(message.content)
    return "The agent produced no reply."


def stream_command(
    command: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    agent=None,
):
    """Yield the agent's reply in chunks, matching `chat.stream_chat`'s contract."""
    agent = agent or build_crm_agent()
    for chunk, metadata in agent.stream(
        {"messages": [HumanMessage(content=command)]},
        config=_run_config(session_id, user_id, thread_id),
        stream_mode="messages",
    ):
        if isinstance(chunk, AIMessage) and chunk.content and metadata.get("langgraph_node") == "agent":
            yield chunk.content if isinstance(chunk.content, str) else str(chunk.content)


def stream_events(
    command: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    agent=None,
):
    """
    Yield `(kind, payload)` events for a UI that shows tool activity.

    `kind` is "tool" (a tool call started, payload is its name) or "text" (a
    chunk of the reply). Useful for rendering "Searching Deals..." spinners
    instead of a silent pause while the agent works.
    """
    agent = agent or build_crm_agent()
    for chunk, metadata in agent.stream(
        {"messages": [HumanMessage(content=command)]},
        config=_run_config(session_id, user_id, thread_id),
        stream_mode="messages",
    ):
        if isinstance(chunk, AIMessage):
            for call in chunk.tool_calls or []:
                if call.get("name"):
                    yield "tool", call["name"]
            if chunk.content and metadata.get("langgraph_node") == "agent":
                yield "text", chunk.content if isinstance(chunk.content, str) else str(chunk.content)


if __name__ == "__main__":
    demo_session = os.getenv("ZOHO_DEMO_SESSION_ID", "cli-session")
    prompt = " ".join(sys.argv[1:]) or "How many open deals do we have, and what are they worth?"
    for piece in stream_command(prompt, session_id=demo_session, thread_id="cli"):
        print(piece, end="", flush=True)
    print()
