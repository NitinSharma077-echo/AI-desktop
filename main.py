"""AI Desktop -- the whole application: HTTP API, OpenAPI docs, and the web UI.

Run it:

    uvicorn main:app --reload

    http://127.0.0.1:8000/        the app
    http://127.0.0.1:8000/docs    Swagger UI
    http://127.0.0.1:8000/openapi.json

One process serves both the API and the frontend that consumes it. That keeps
the browser same-origin, so no CORS configuration is needed for the app's own UI
and the token never crosses an origin boundary. `CORS_ALLOW_ORIGINS` exists only
for a *different* frontend hosted elsewhere.

Two deliberate structural choices:

* The heavy modules (chat, RAG, Zoho) are imported inside their handlers, not at
  the top. tools/search.py reads os.environ["TAVILY_API_KEY"] at import time and
  the model clients build eagerly, so importing them here would mean one missing
  key takes down the whole app -- including the UI and /auth, which do not need
  it. Deferred, a missing key is a 503 on one endpoint.
* Conversation threads and Zoho sessions are namespaced by the caller's user id.
  Both take a client-supplied id, and without namespacing one user could pass
  another's and read their conversation.

Which model answers a request is providers.py's decision: Ollama by default for
local work, OpenAI when LLM_PROVIDER says so. Nothing in this file knows which.
"""

import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import providers
from user_auth import jwt as auth_config
from user_auth.jwt import auth_required, current_user_id
from user_auth.routes import router as auth_router

# Vite's build output. Absent until `npm run build` has run, which is why every
# use of it is guarded -- the API must still start on a checkout that has never
# built the UI.
FRONTEND_DIR = Path(__file__).resolve().parent / "frontend" / "my-react-app" / "dist"

DESCRIPTION = """
An assistant API: chat with tool use, PDF question-answering, and Zoho CRM.
The web UI at `/` is built on exactly these endpoints.

### Getting a token

1. `POST /auth/register` with a username and password.
2. Click **Authorize** above and log in, or `POST /auth/token` and send the
   `access_token` as `Authorization: Bearer <token>`.
3. Access tokens are short-lived. When one expires, `POST /auth/refresh` with
   the refresh token rather than logging in again.

### Model provider

`GET /health` reports which provider is configured and whether its credentials
are present. Ollama is the default for local development; deployments set
`LLM_PROVIDER=openai`.
"""

TAGS = [
    {"name": "auth", "description": "Register, log in, and refresh tokens."},
    {"name": "chat", "description": "Talk to the assistant. Tool use and CRM routing included."},
    {"name": "documents", "description": "Upload PDFs for the assistant to search."},
    {"name": "zoho", "description": "Connect a Zoho account and run CRM commands."},
    {"name": "system", "description": "Health, readiness, and model configuration."},
]

app = FastAPI(
    title="AI Desktop API",
    description=DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS,
)

# Only for a frontend served from somewhere other than this process. The bundled
# UI is same-origin, so leaving this unset keeps the default surface minimal.
_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
if _origins:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(auth_router)


# -- system ----------------------------------------------------------------


@app.get("/health", tags=["system"])
def health() -> dict:
    """
    Liveness probe, model configuration, and whether authentication is on.

    Does no I/O, so it stays up when a backend is down. Reports which provider
    is configured and whether its credentials are present -- the first thing
    worth checking when /chat starts failing -- and never returns the key
    itself. The UI reads `auth.required` to decide whether to show its
    open-access banner.
    """
    return {
        "status": "ok",
        "llm": providers.describe(),
        "auth": auth_config.describe(),
        # Echoed so a split deployment can be diagnosed with one curl instead of
        # log access: an empty list here is why the browser is blocking calls.
        # These are public origins, not a secret.
        "cors": {"allow_origins": _origins},
    }


# -- chat ------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, examples=["What is the weather in Pune?"])
    thread_id: str = Field(
        default="default",
        description=(
            "Conversation to continue. Scoped to your account, so the same id "
            "used by another user is a different conversation."
        ),
    )
    crm_session_id: str | None = Field(
        default=None,
        description="Zoho session to use for `/crm ...` messages. See POST /zoho/session/connect.",
    )


class ChatResponse(BaseModel):
    reply: str
    thread_id: str


def _scoped(user_id: str, value: str) -> str:
    """Namespace a client-supplied id under its owner. See the module docstring."""
    return f"{user_id}::{value}"


def _chat_stream(body: ChatRequest, user_id: str):
    """Resolve the chat module and return its generator, as one 503-able step."""
    try:
        from chat import chat as chat_module
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The chat engine is not available: {exc}",
        ) from exc

    return chat_module.stream_chat(
        body.message,
        _scoped(user_id, body.thread_id),
        crm_session_id=_scoped(user_id, body.crm_session_id) if body.crm_session_id else None,
    )


@app.post("/chat", response_model=ChatResponse, tags=["chat"])
def chat(body: ChatRequest, user_id: str = Depends(current_user_id)) -> ChatResponse:
    """
    Send a message and wait for the whole reply.

    Prefix with `/coding` to route to Gemini, or `/crm` to reach the CRM agent.
    """
    try:
        pieces = list(_chat_stream(body, user_id))
    except HTTPException:
        raise
    except Exception as exc:
        # stream_chat is a generator, so a dead model backend does not fail until
        # something drains it. An unreachable Ollama or a rejected OpenAI key both
        # land here, and both deserve better than a bare 500.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"The model backend failed: {exc}",
        ) from exc
    return ChatResponse(reply="".join(pieces), thread_id=body.thread_id)


@app.post("/chat/stream", tags=["chat"])
def chat_stream(body: ChatRequest, user_id: str = Depends(current_user_id)) -> StreamingResponse:
    """
    The same thing, streamed as plain text chunks as the model produces them.

    This is what the web UI calls. Swagger UI buffers the whole response before
    showing it, so the streaming is only visible from a real client.
    """
    stream = _chat_stream(body, user_id)

    def generate():
        try:
            yield from stream
        except Exception as exc:
            # The status line is long gone by the time a mid-stream failure
            # happens, so the only place left to report it is the body.
            yield f"\n\n[stream failed: {exc}]"

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


# -- documents -------------------------------------------------------------


class IngestResponse(BaseModel):
    filename: str
    chunks: int


@app.post("/documents", response_model=IngestResponse, tags=["documents"])
async def upload_document(
    file: UploadFile = File(description="A PDF to index."),
    user_id: str = Depends(current_user_id),
) -> IngestResponse:
    """
    Index a PDF so `/chat` can answer questions about it.

    Note that the vector store is currently shared by every user of the
    deployment -- RAG/rag.py keeps one collection with no owner on its
    documents, so anything uploaded here is searchable by anyone else logged in.
    Per-user isolation means adding a user filter to that collection.
    """
    if file.content_type not in (None, "application/pdf", "application/octet-stream"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Expected a PDF, got {file.content_type}.",
        )
    try:
        from RAG.rag import add_pdf
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The document store is not available: {exc}",
        ) from exc

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The file is empty.")

    try:
        chunks = add_pdf(payload, filename=file.filename or "upload.pdf")
    except Exception as exc:
        # Two causes land here and the exception text is what tells them apart:
        # a PDF pypdf cannot parse, or an embedding backend that would not answer.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not index the PDF: {exc}",
        ) from exc

    return IngestResponse(filename=file.filename or "upload.pdf", chunks=chunks)


@app.delete("/documents", tags=["documents"])
def clear_documents(user_id: str = Depends(current_user_id)) -> dict:
    """Empty the vector store. Affects every user, for the reason above."""
    try:
        from RAG.rag import clear_all

        clear_all()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not clear the document store: {exc}",
        ) from exc
    return {"cleared": True}


# -- Zoho CRM --------------------------------------------------------------

# Mounted defensively: zoho.routes pulls in the CRM agent and its 18 tools at
# import time, and a deployment with no Zoho configuration should still serve
# the UI, chat and auth rather than failing to boot.
try:
    from zoho.routes import build_zoho_router

    app.include_router(build_zoho_router(current_user_id))
except Exception as exc:  # pragma: no cover - depends on deployment config
    logging.getLogger(__name__).warning(
        "Zoho routes are not mounted (%s: %s). The rest of the app is unaffected.",
        type(exc).__name__,
        exc,
    )


# -- the web UI ------------------------------------------------------------

# Platform-set variables that mean "this is not someone's laptop". Used to keep
# the warnings below quiet during local development, where the same settings are
# the correct ones.
PLATFORM_VARS = ("RENDER", "DYNO", "FLY_APP_NAME", "K_SERVICE", "WEBSITE_INSTANCE_ID")


def _is_hosted() -> bool:
    return any(os.getenv(v) for v in PLATFORM_VARS)


def _startup_diagnostics() -> None:
    """
    Say plainly, at boot, which settings will make this deployment fail.

    Every one of these produces a runtime failure far from its cause: CORS shows
    up as a browser console error the server never sees, an unreachable Ollama as
    "Connection refused" inside a 502, a missing key as a 401 from OpenAI. Naming
    them here turns a debugging session into a log line.
    """
    log = logging.getLogger(__name__)

    auth = auth_config.describe()
    if not auth["required"]:
        # The UI shows a banner too, but whoever deploys this may only ever look
        # at the logs.
        log.warning(
            "AUTH_REQUIRED is off: every endpoint is open and runs as a shared "
            "development user. Set AUTH_REQUIRED=true before exposing this."
        )
    elif not auth["ready"]:
        # AUTH_REQUIRED=true with a broken secret is the worst of both worlds:
        # /auth/register still returns 201, so the app looks alive, but nobody
        # can obtain a token and every login is a bare 500.
        log.error("AUTH_REQUIRED=true but tokens cannot be issued. %s", auth["detail"])

    if _is_hosted() and not _origins:
        log.warning(
            "CORS_ALLOW_ORIGINS is not set. Same-origin requests still work, but a "
            "frontend served from another host will have every response blocked by "
            "the browser -- and the preflight will 405, because the CORS middleware "
            "is not even mounted. Set it to the frontend's origin, scheme included, "
            "no trailing slash."
        )

    llm = providers.describe()
    if not llm["ready"]:
        log.warning("Model provider not ready: %s", llm["detail"])
    elif _is_hosted() and llm["provider"] == "ollama":
        base = providers.ollama_base_url()
        if "localhost" in base or "127.0.0.1" in base:
            log.warning(
                "LLM_PROVIDER=ollama pointing at %s, on what looks like a hosted "
                "environment. There is no Ollama in this container, so every chat "
                "and every PDF embedding will fail with 'Connection refused'. Set "
                "LLM_PROVIDER=openai (plus OPENAI_API_KEY), or point OLLAMA_BASE_URL "
                "at a reachable host.",
                base,
            )


_startup_diagnostics()

# Mounted last, so the API routes registered above always win a path collision.
# Vite emits hashed files into dist/assets and copies public/ to the dist root,
# so both need serving; `html=True` is what makes "/" resolve to index.html.
#
# An unknown path still 404s rather than falling back to index.html, which is
# right while the UI is a single screen with no client-side router. Add a
# catch-all route here if one is ever introduced.
if (FRONTEND_DIR / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")
else:
    logging.getLogger(__name__).warning(
        "No UI build at %s -- serving the API only. Run: "
        "npm --prefix frontend/my-react-app install && npm --prefix frontend/my-react-app run build",
        FRONTEND_DIR,
    )

    @app.get("/", include_in_schema=False)
    def no_ui() -> dict:
        return {"detail": "UI not built. See /docs for the API.", "docs": "/docs"}
