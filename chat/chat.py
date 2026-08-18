import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uuid
from functools import lru_cache
from typing import Annotated, TypedDict
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

import providers
from tools.search import Tools
from RAG.rag import rag_search

load_dotenv()

tool_list = list(Tools.values()) + [rag_search]


@lru_cache(maxsize=1)
def get_llm():
    """
    The default chat model -- OpenAI or Ollama, whichever LLM_PROVIDER selects.

    Built on first use rather than at import. With LLM_PROVIDER=openai the
    constructor needs an API key, and merely importing this module should not
    require one: /health, /auth and the CRM routes never send a message.

    Which model and which provider is providers.py's decision, not this
    module's -- the embedding function has to agree with it, and one switch is
    the only way to guarantee that.
    """
    return providers.get_chat_model().bind_tools(tool_list)


_llm_coding: ChatOpenAI | None = None

def get_llm_coding() -> ChatOpenAI  :
    global _llm_coding
    if _llm_coding is None:
        _llm_coding = ChatOpenAI(model="gpt-3.5-turbo", temperature=0.7).bind_tools(
            tool_list
        )
    return _llm_coding

SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the `search` or `web_search` tools to look things up "
    "on the web, `weather_search` to check current weather, and `rag_search` to look things up "
    "in documents the user has uploaded, whenever they would help answer the user's question."
)

CRM_COMMAND_PREFIX = "/crm"

SEARCH_TRIGGER_WORDS = (
    "search",
    "latest",
    "news",
    "internet",
    "look up",
    "lookup",
    "google",
    "current",
)

DOCUMENT_TRIGGER_WORDS = (
    "document",
    "pdf",
    "uploaded",
    "the file",
    "my file",
)

# Phrases the model tends to hedge with when it thinks a question is beyond
# its training data, instead of realizing it has a search tool available.
REFUSAL_PHRASES = (
    "real-time",
    "real time",
    "my last update",
    "my knowledge cutoff",
    "knowledge cutoff",
    "as of my last",
    "i cannot browse",
    "i can't browse",
    "i don't have access to",
    "i do not have access to",
    "hypothetical year",
    "cannot predict future",
    "can't predict future",
)


class ChatState(TypedDict):
    messages: Annotated[list, add_messages]


def _force_tool_call(tool_name: str, args: dict) -> ChatState:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool_name,
                        "args": args,
                        "id": str(uuid.uuid4()),
                        "type": "tool_call",
                    }
                ],
            )
        ]
    }


def chat_node(state: ChatState) -> ChatState:
    messages = state["messages"]
    last_human = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
    model = get_llm_coding() if last_human.content.startswith("/coding") else get_llm()

    # Small local models are unreliable at deciding *when* to call a tool for
    # open-ended queries (they tend to just refuse instead). If the message
    # looks like a search/document request, force the matching tool call
    # directly instead of leaving that judgment call to the model. Skip this
    # if we've already forced a call for this turn (last message is its
    # result), to avoid looping.
    #
    # Only for local models. Keyword matching is blunt -- "what's the current
    # status of my order" forces a web search on a question the model should
    # just answer -- so a hosted model, which picks tools reliably on its own,
    # is better off without the crutch.
    just_called_tool = isinstance(messages[-1], ToolMessage)
    lowered = last_human.content.lower()
    force_tools = providers.is_local() and not last_human.content.startswith("/coding")
    if force_tools and not just_called_tool:
        if any(word in lowered for word in DOCUMENT_TRIGGER_WORDS):
            return _force_tool_call("rag_search", {"question": last_human.content})
        if any(word in lowered for word in SEARCH_TRIGGER_WORDS):
            return _force_tool_call("search", {"query": last_human.content})

    if not any(isinstance(m, SystemMessage) for m in messages):
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + messages

    response = model.invoke(messages)

    # Keyword triggers can't cover every phrasing of a question that actually
    # needs current information. As a fallback, if the model answered without
    # calling a tool but hedged with a knowledge-cutoff-style refusal, retry
    # once by forcing a real search on the original question. Guarded by
    # just_called_tool so this can't loop if the retry also comes up empty, and
    # by force_tools for the same reason as above -- a hosted model that says
    # "I don't have access to" usually means it, and searching anyway just
    # spends a Tavily call to answer a question that was never about the web.
    if force_tools and not just_called_tool and not response.tool_calls:
        content_lower = (response.content or "").lower()
        if any(phrase in content_lower for phrase in REFUSAL_PHRASES):
            return _force_tool_call("search", {"query": last_human.content})

    return {"messages": [response]}


graph = StateGraph(ChatState)
graph.add_node("chat", chat_node)
graph.add_node("tools", ToolNode(tool_list))
graph.add_edge(START, "chat")
graph.add_conditional_edges("chat", tools_condition)
graph.add_edge("tools", "chat")

app = graph.compile(checkpointer=MemorySaver())


def _stream_crm(command: str, thread_id: str, crm_session_id: str | None):
    """
    Hand a `/crm` command to the Zoho agent.

    That agent is kept separate from this graph rather than having its tools
    merged in: it has its own system prompt and 18 CRM tools, and giving those
    to the general chat model would both degrade everyday answers and make CRM
    work less reliable. Routing on the prefix is deterministic, so it also can't
    misfire the way the keyword triggers above can.
    """
    if not command:
        yield (
            "Tell me what to do in CRM, e.g. `/crm show me deals closing this month` "
            "or `/crm create a lead for Priya Nair at Zenith Labs`."
        )
        return

    # Imported lazily so the rest of the app still runs when Zoho isn't
    # configured -- a missing MONGODB_URI shouldn't break ordinary chat.
    try:
        from zoho.auth import session_auth
        from zoho.crm.crm_agent import stream_command
    except Exception as exc:
        yield f"The Zoho CRM integration isn't available: {exc}"
        return

    # The session lookup hits MongoDB, so infrastructure failures surface here
    # first. Report them as one readable line rather than letting a pymongo
    # topology dump land in the chat window.
    try:
        connected = bool(crm_session_id) and session_auth.has_session(crm_session_id)
    except Exception as exc:
        from zoho.config import describe_db_error

        yield f"Can't reach the CRM session store. {describe_db_error(exc)}"
        return

    if not connected:
        yield (
            "Zoho CRM isn't connected for this session. Connect it through the API "
            "(`POST /zoho/session/connect`, see `/docs`) -- the web UI shows "
            "connection status but deliberately never collects credentials."
        )
        return

    yield from stream_command(command, session_id=crm_session_id, thread_id=thread_id)


def stream_chat(user_input: str, thread_id: str, crm_session_id: str | None = None):
    """Yield response text chunks as they're generated by the chat node."""
    stripped = user_input.strip()
    if stripped == CRM_COMMAND_PREFIX or stripped.startswith(CRM_COMMAND_PREFIX + " "):
        yield from _stream_crm(
            stripped[len(CRM_COMMAND_PREFIX) :].strip(), thread_id, crm_session_id
        )
        return

    for chunk, metadata in app.stream(
        {"messages": [HumanMessage(content=user_input)]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="messages",
    ):
        if metadata.get("langgraph_node") == "chat" and chunk.content:
            yield chunk.content


if __name__ == "__main__":
    for piece in stream_chat("write me a task generation code?", "demo-session"):
        print(piece, end="", flush=True)
    print()
