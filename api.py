"""The HTTP face of AI Desktop: the same chat, documents and CRM the Streamlit UI
drives, behind a documented, token-authenticated API.

Run it locally:

    uvicorn api:app --reload

Then open http://127.0.0.1:8000/docs for Swagger UI, /redoc for ReDoc, or
/openapi.json for the raw spec to feed a client generator.

The quickest path through it: POST /auth/register, then click **Authorize** at
the top right of /docs and log in -- Swagger fills the bearer header into every
later call for you.

Two deliberate structural choices:

* The heavy modules (chat, RAG, Zoho) are imported inside their handlers, not at
  the top. tools/search.py reads os.environ["TAVILY_API_KEY"] at import time and
  the LLM clients build their graphs eagerly, so importing them here would mean
  one missing key takes down the whole API -- including /health and /auth, which
  do not need it. Deferred, a missing key is a 503 on one endpoint.
* Conversation threads and Zoho sessions are namespaced by the caller's user id.
  Both take a client-supplied id, and without namespacing one user could pass
  another's and read their conversation.
"""

import os

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from user_auth.jwt import current_user_id
from user_auth.routes import router as auth_router

DESCRIPTION = """
An assistant API: chat with tool use, PDF question-answering, and Zoho CRM.

### Getting a token

1. `POST /auth/register` with a username and password.
2. Click **Authorize** above and log in, or `POST /auth/token` and copy the
   `access_token` into an `Authorization: Bearer <token>` header.
3. Access tokens are short-lived. When one expires, `POST /auth/refresh` with
   the refresh token rather than logging in again.

### What needs what

| Endpoint | Needs |
| --- | --- |
| `/chat` | An Ollama server (`OLLAMA_BASE_URL`), or prefix the message with `/coding` to route to Gemini (`GOOGLE_API_KEY`) |
| `/documents` | An Ollama server for embeddings |
| `/zoho/*` | A Zoho connection opened via `/zoho/session/connect` |
"""

TAGS = [
    {"name": "auth", "description": "Register, log in, and refresh tokens."},
    {"name": "chat", "description": "Talk to the assistant. Tool use and CRM routing included."},
    {"name": "documents", "description": "Upload PDFs for the assistant to search."},
    {"name": "zoho", "description": "Connect a Zoho account and run CRM commands."},
    {"name": "system", "description": "Health and readiness."},
]

app = FastAPI(
    title="AI Desktop API",
    description=DESCRIPTION,
    version="0.1.0",
    openapi_tags=TAGS,
)

# Only mounted when explicitly configured. A browser frontend on another origin
# needs this; Swagger UI, being same-origin, does not -- so leaving it unset
# keeps the default surface as small as possible.
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
    """Liveness probe. Deliberately does no I/O, so it stays up when a backend is down."""
    return {"status": "ok"}


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
        # something drains it. Unreachable Ollama is the common case here, and it
        # deserves better than a bare 500.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"The model backend failed (is OLLAMA_BASE_URL reachable?): {exc}",
        ) from exc
    return ChatResponse(reply="".join(pieces), thread_id=body.thread_id)


@app.post("/chat/stream", tags=["chat"])
def chat_stream(body: ChatRequest, user_id: str = Depends(current_user_id)) -> StreamingResponse:
    """
    The same thing, streamed as plain text chunks as the model produces them.

    Swagger UI buffers the whole response before showing it, so the streaming is
    only visible from a real client (`curl -N`, fetch, httpx.stream).
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
        # a PDF pypdf cannot parse, or an unreachable Ollama when the chunks go
        # off to be embedded.
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
# chat and auth rather than failing to boot.
try:
    from zoho.routes import build_zoho_router

    app.include_router(build_zoho_router(current_user_id))
except Exception as exc:  # pragma: no cover - depends on deployment config
    import logging

    logging.getLogger(__name__).warning(
        "Zoho routes are not mounted (%s: %s). The rest of the API is unaffected.",
        type(exc).__name__,
        exc,
    )
