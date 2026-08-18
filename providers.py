"""Which model provider the app talks to, decided in one place.

OpenAI in deployment, Ollama on a dev machine. `LLM_PROVIDER` picks; everything
else follows from it.

Both the chat model and the embedding function are built here rather than in the
modules that use them, because the two must not drift apart. Embeddings decide
the geometry of the vector store -- nomic-embed-text writes 768-dimension
vectors, text-embedding-3-small writes 1536 -- and a collection written by one
provider cannot be read by the other. Deriving both from a single switch is what
keeps a provider change from silently corrupting search.

Nothing here is constructed at import time. A dev machine with no OpenAI key
should be able to import this module, and a container with no Ollama should be
able to serve everything that does not need a model.

    LLM_PROVIDER=ollama            # default; local testing
    LLM_PROVIDER=openai            # deployment

    OPENAI_API_KEY=sk-...          # or OPEN_API, which this repo's .env uses
    OPENAI_CHAT_MODEL=gpt-4o-mini
    OPENAI_EMBEDDING_MODEL=text-embedding-3-small

    OLLAMA_BASE_URL=http://localhost:11434
    OLLAMA_CHAT_MODEL=phi4-mini
    OLLAMA_EMBEDDING_MODEL=nomic-embed-text
"""

import os
import re
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

OLLAMA = "ollama"
OPENAI = "openai"

DEFAULT_MODELS = {
    OPENAI: {"chat": "gpt-4o-mini", "embedding": "text-embedding-3-small"},
    OLLAMA: {"chat": "phi4-mini", "embedding": "nomic-embed-text"},
}

TEMPERATURE = 0.7


def provider() -> str:
    """
    The active provider.

    Defaults to Ollama so that cloning the repo and running it needs no API key
    and sends nothing to a paid endpoint. Deployment opts in explicitly.
    """
    name = os.getenv("LLM_PROVIDER", OLLAMA).strip().lower()
    if name not in DEFAULT_MODELS:
        raise RuntimeError(
            f"LLM_PROVIDER={name!r} is not a provider. Valid values: "
            f"{', '.join(sorted(DEFAULT_MODELS))}."
        )
    return name


def is_local() -> bool:
    """Whether the active model runs on a local Ollama rather than a hosted API."""
    return provider() == OLLAMA


def ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


def openai_api_key() -> str:
    """
    The OpenAI key, under either name.

    OPENAI_API_KEY is the conventional one that every OpenAI SDK picks up on its
    own; OPEN_API is what this repo's existing .env calls it. Accepting both
    means switching providers does not also mean renaming a variable.
    """
    key = (os.getenv("OPENAI_API_KEY") or os.getenv("OPEN_API") or "").strip()
    if not key:
        raise RuntimeError(
            "LLM_PROVIDER=openai needs an API key. Set OPENAI_API_KEY (or OPEN_API) "
            "in the environment. For local development without one, use "
            "LLM_PROVIDER=ollama instead."
        )
    return key


def chat_model() -> str:
    key = "OPENAI_CHAT_MODEL" if provider() == OPENAI else "OLLAMA_CHAT_MODEL"
    return os.getenv(key, DEFAULT_MODELS[provider()]["chat"]).strip()


def embedding_model() -> str:
    key = "OPENAI_EMBEDDING_MODEL" if provider() == OPENAI else "OLLAMA_EMBEDDING_MODEL"
    return os.getenv(key, DEFAULT_MODELS[provider()]["embedding"]).strip()


def collection_name() -> str:
    """
    Vector collection for the active embedding model.

    Chroma fixes a collection's vector width when it is created, and the two
    providers disagree about it. Sharing one name across a provider switch means
    every query fails on a dimension mismatch until someone wipes the store by
    hand; keying the name to the model makes the switch a no-op, and switching
    back finds the old vectors still intact.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", embedding_model().lower()).strip("_")
    return f"documents_{slug}"


def build_chat_model(*, model: str | None = None, temperature: float | None = None):
    """
    Construct a chat model on the active provider.

    Uncached and parameterised, for callers that need something other than the
    default. The Zoho CRM agent is the reason: it drives ~18 tools with detailed
    schemas and wants temperature 0, where general chat wants 0.7.

    Tools are bound by the caller -- the tool list lives in chat.py and
    importing it here would be a cycle.
    """
    if provider() == OPENAI:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model or chat_model(),
            api_key=openai_api_key(),
            temperature=TEMPERATURE if temperature is None else temperature,
            # A hung request otherwise holds a worker open until the platform
            # kills it, which on a single-worker deployment means total silence.
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        )

    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=model or chat_model(),
        base_url=ollama_base_url(),
        temperature=TEMPERATURE if temperature is None else temperature,
        verbose=True,
    )


@lru_cache(maxsize=1)
def get_chat_model():
    """The default chat model, built once per process."""
    return build_chat_model()


@lru_cache(maxsize=1)
def get_embedding_function():
    """The Chroma embedding function matching the active provider."""
    if provider() == OPENAI:
        from chromadb.utils.embedding_functions import OpenAIEmbeddingFunction

        # Passed explicitly: left to itself this reads CHROMA_OPENAI_API_KEY,
        # which is not a name anything else in this project uses.
        return OpenAIEmbeddingFunction(api_key=openai_api_key(), model_name=embedding_model())

    from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

    return OllamaEmbeddingFunction(url=ollama_base_url(), model_name=embedding_model())


def describe() -> dict:
    """
    A summary safe to show in a UI or a health endpoint.

    Reports whether a key is present, never the key itself, and never raises --
    a status display that throws when misconfigured is worse than useless.
    """
    # Every branch returns the same keys. A status dict whose shape depends on
    # whether the thing is healthy makes callers crash exactly when something is
    # already wrong, which is the worst possible moment.
    try:
        name = provider()
    except RuntimeError as exc:
        return {
            "provider": "invalid",
            "chat_model": "",
            "embedding_model": "",
            "ready": False,
            "detail": str(exc),
        }

    if name == OPENAI:
        ready = bool((os.getenv("OPENAI_API_KEY") or os.getenv("OPEN_API") or "").strip())
        detail = "" if ready else "OPENAI_API_KEY (or OPEN_API) is not set."
    else:
        ready = True
        detail = f"Expects Ollama at {ollama_base_url()}."

    return {
        "provider": name,
        "chat_model": chat_model(),
        "embedding_model": embedding_model(),
        "ready": ready,
        "detail": detail,
    }
