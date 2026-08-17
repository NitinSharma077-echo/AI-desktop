"""FastAPI routes for connecting Zoho accounts and issuing CRM commands.

The router takes your app's "who is calling" dependency rather than defining
one, so it works with whatever `user_auth` ends up being (JWT, session cookie,
API key) without this package having an opinion about it.

    from fastapi import Depends, FastAPI
    from zoho.routes import build_zoho_router

    app = FastAPI()
    app.include_router(build_zoho_router(current_user_id))

Note that /zoho/callback is deliberately *not* behind your auth dependency:
Zoho redirects the browser there and won't carry your Authorization header. The
user's identity comes from the one-time `state` value that was stored when the
flow started, which is why that state must never be accepted twice.
"""

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from zoho.auth import session_auth, zoho_oauth
from zoho.crm.crm_agent import run_command
from zoho.errors import ZohoAuthError, ZohoError, ZohoNotConnected


class SessionConnectRequest(BaseModel):
    """What the user pastes once, from their Zoho API console self client."""

    session_id: str = Field(description="Identifier for this chat. Must be unguessable.")
    client_id: str
    client_secret: str
    grant_code: str
    region: str | None = Field(default=None, description="us, eu, in, au, jp, uk, ca, sa, cn")


class CommandRequest(BaseModel):
    command: str
    session_id: str | None = None
    thread_id: str | None = None


class CommandResponse(BaseModel):
    reply: str


def build_zoho_router(
    get_user_id: Callable[..., str],
    *,
    prefix: str = "/zoho",
    success_redirect: str | None = None,
) -> APIRouter:
    """
    Build the Zoho router.

    Args:
        get_user_id: FastAPI dependency returning the caller's user id.
        prefix: URL prefix for the routes.
        success_redirect: Where to send the browser after a successful connect.
            Defaults to returning a small JSON body instead.
    """
    router = APIRouter(prefix=prefix, tags=["zoho"])

    # -- self-client flow: the user brings their own credentials -----------

    @router.post("/session/connect")
    def session_connect(body: SessionConnectRequest, user_id: str = Depends(get_user_id)):
        """
        Open a Zoho connection for one chat from user-supplied credentials.

        Serve this over HTTPS only -- the client secret and grant code are in
        the request body. Neither is ever returned, logged, or passed to the
        model.
        """
        try:
            return session_auth.connect(
                session_id=_scoped(user_id, body.session_id),
                client_id=body.client_id,
                client_secret=body.client_secret,
                grant_code=body.grant_code,
                region=body.region,
            )
        except ZohoAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/session/status")
    def session_status(session_id: str, user_id: str = Depends(get_user_id)):
        """Whether this chat has a live connection. Never returns credentials."""
        return session_auth.status(_scoped(user_id, session_id))

    @router.post("/session/end")
    def session_end(session_id: str, user_id: str = Depends(get_user_id)):
        """Close the chat: revoke the tokens at Zoho and delete them."""
        return {"ended": session_auth.end(_scoped(user_id, session_id))}

    # -- redirect flow: this app owns the Zoho client ----------------------

    @router.get("/connect")
    def connect(user_id: str = Depends(get_user_id)):
        """Start the OAuth flow: redirects the user to Zoho's consent screen."""
        return RedirectResponse(zoho_oauth.build_authorize_url(user_id), status_code=307)

    @router.get("/callback")
    def callback(
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        location: str | None = Query(default=None),
        accounts_server: str | None = Query(default=None, alias="accounts-server"),
        error: str | None = Query(default=None),
    ):
        """Zoho redirects here after the user accepts or declines."""
        if error:
            raise HTTPException(status_code=400, detail=f"Zoho authorization failed: {error}")
        if not code or not state:
            raise HTTPException(status_code=400, detail="Missing code or state in callback")

        try:
            user_id = zoho_oauth.complete_authorization(code, state, location, accounts_server)
        except ZohoAuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if success_redirect:
            return RedirectResponse(success_redirect, status_code=303)
        return {"connected": True, "user_id": user_id}

    @router.get("/status")
    def status(user_id: str = Depends(get_user_id)):
        """Whether this user has a live Zoho connection. Never returns tokens."""
        return zoho_oauth.connection_status(user_id)

    @router.post("/disconnect")
    def disconnect(user_id: str = Depends(get_user_id)):
        """Revoke this user's Zoho tokens and forget the connection."""
        return {"disconnected": zoho_oauth.disconnect(user_id)}

    @router.post("/crm/command", response_model=CommandResponse)
    def crm_command(body: CommandRequest, user_id: str = Depends(get_user_id)):
        """
        Run a natural-language CRM command.

        Uses this chat's self-client session when `session_id` is given,
        otherwise the user's stored redirect-flow connection.
        """
        try:
            reply = run_command(
                body.command,
                session_id=_scoped(user_id, body.session_id) if body.session_id else None,
                user_id=user_id,
                thread_id=body.thread_id,
            )
            return CommandResponse(reply=reply)
        except ZohoNotConnected as exc:
            raise HTTPException(status_code=428, detail=str(exc)) from exc
        except ZohoError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return router


def _scoped(user_id: str, session_id: str) -> str:
    """
    Namespace a client-supplied session id under the authenticated user.

    Without this, a caller could pass someone else's session_id and borrow
    their Zoho connection -- the ids come from the request body, so they cannot
    be trusted on their own.
    """
    return f"{user_id}::{session_id}"
