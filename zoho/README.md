# Zoho CRM agent

A natural-language command layer over Zoho CRM, built for multi-user use: one
agent instance in the process, one Zoho connection per user, and no shared
state between them.

```
zoho/
  config.py             settings, data-centre domains, store selection
  store.py              in-process stand-in for the Mongo collections
  errors.py             ZohoNotConnected / ZohoAuthError / ZohoAPIError / ZohoRateLimited
  client.py             authenticated HTTP: token refresh, retries, error translation
  routes.py             FastAPI routes for connect / status / end / command
  auth/common.py        credential encryption + the shared token endpoint call
  auth/session_auth.py  self-client flow: user brings credentials, scoped to one chat
  auth/zoho_oauth.py    redirect flow: this app owns the Zoho client
  auth/providers.py     the seam between those two flows and the HTTP client
  crm/api.py            typed wrappers over the CRM v8 endpoints
  crm/tools.py          the 18 LangChain tools the agent calls
  crm/crm_agent.py      the agent itself
```

## Two ways to connect

| | Self-client (`session_auth`) | Redirect OAuth (`zoho_oauth`) |
|---|---|---|
| Who registers with Zoho | each user | you, once |
| User hands over | client id, secret, grant code | nothing — they click Allow |
| Needs a public callback URL | no | yes |
| Connection lasts | the chat | until revoked |
| Best for | internal tools, onboarding one org at a time | a product with many customers |

Both are wired up. The self-client flow is described first because it's the one
you asked for.

## Setup

### 1. Environment

Nothing is required. Out of the box the store runs in memory:

```bash
ZOHO_STORE=memory                 # the default; no database needed
ZOHO_DEFAULT_REGION=in            # us | eu | in | au | jp | uk | ca | sa | cn
```

Optional:

```bash
ZOHO_TOKEN_ENCRYPTION_KEY=...     # auto-generated per process on the memory store
ZOHO_SESSION_TTL_SECONDS=43200    # idle chat connections self-destruct after this
ZOHO_CRM_API_VERSION=v8
ZOHO_CRM_ALLOW_WRITES=true        # false gives the agent a read-only toolset
ZOHO_CRM_MODEL=gemini-2.0-flash
ZOHO_CRM_RECURSION_LIMIT=25
ZOHO_MAX_RETRIES=3
ZOHO_REQUEST_TIMEOUT=30
```

Only for the redirect flow — leave unset if every user brings their own
credentials:

```bash
ZOHO_CLIENT_ID=1000.XXXXXXXX
ZOHO_CLIENT_SECRET=xxxxxxxx
ZOHO_REDIRECT_URI=http://localhost:8000/zoho/callback
```

### 1b. Turning on persistence later

```bash
ZOHO_STORE=mongo
MONGODB_URI=mongodb://...
ZOHO_TOKEN_ENCRYPTION_KEY=...     # now mandatory, and must stay stable
MONGODB_TIMEOUT_MS=5000
```

That is the whole migration — `store.py` implements the same collection API
that `session_auth` and `zoho_oauth` already call, so no other file changes.

While `ZOHO_STORE=memory`:

- connections live in one process, so multiple workers don't share them, and a
  restart means users reconnect;
- the encryption key is generated per process, since nothing outlives it —
  encryption still runs, so the persistent path behaves identically;
- TTL expiry is evaluated on read rather than by a background reaper, which is
  indistinguishable to callers;
- queries support equality and `$lt` only. Anything else raises loudly instead
  of quietly returning the wrong rows.

Refresh tokens are encrypted before they reach MongoDB, so a key is required:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Once you set a key (needed only with `ZOHO_STORE=mongo`), keep it out of the
repo and stable — rotating it invalidates every stored connection, and all
users have to reconnect.

### 2. What the user does, once per chat

They go to [api-console.zoho.com](https://api-console.zoho.com) → **Self
Client** → copy the client id and secret, then **Generate Code** with the CRM
scopes (`ZohoCRM.modules.ALL,ZohoCRM.settings.modules.READ,ZohoCRM.settings.fields.READ,ZohoCRM.users.READ,ZohoCRM.org.READ,ZohoCRM.coql.READ`)
and paste all three into your form.

The grant code is **single-use and expires in 3–10 minutes**, so exchange it
immediately. It only ever has to happen once per chat — everything afterwards
runs off the refresh token.

### 3. Connect, run, end

```python
from zoho.auth import session_auth
from zoho.crm.crm_agent import run_command

# once, when they submit the form
session_auth.connect(
    session_id="chat-42",          # your chat id; must be unguessable
    client_id="1000.XXXX",
    client_secret="...",
    grant_code="1000.abc...",
    region="in",
)

# then every message in that chat
run_command("which deals close this month and what are they worth?", session_id="chat-42")
run_command("create a lead for Priya Nair at Zenith Labs, priya@zenith.io", session_id="chat-42")
run_command("log a note on the Acme deal: pricing approved", session_id="chat-42")

# when the chat closes
session_auth.end("chat-42")        # revokes at Zoho, then deletes
```

`stream_command` and `stream_events` take the same keywords. Add `thread_id` for
follow-ups ("now show me only the closed ones"); pass `user_id=` instead of
`session_id=` to run against a redirect-flow connection.

### 4. Or wire up the HTTP routes

```python
from fastapi import Depends, FastAPI
from zoho.routes import build_zoho_router

app = FastAPI()
app.include_router(build_zoho_router(current_user_id))   # your own auth dependency
```

| Route | Purpose |
|---|---|
| `POST /zoho/session/connect` | the paste-credentials form |
| `GET  /zoho/session/status` | is this chat connected |
| `POST /zoho/session/end` | close the chat, revoke tokens |
| `POST /zoho/crm/command` | run a command |
| `GET  /zoho/connect` → `/zoho/callback` | redirect flow instead |

`current_user_id` is any FastAPI dependency returning your app's user id.
Session ids from request bodies are namespaced under it, so a caller can't pass
someone else's `session_id` and borrow their Zoho connection.

## How isolation works

The agent is built once and shared. The connection travels in the run config,
not in the agent or in module state:

```python
agent.invoke(
    {"messages": [...]},
    config={"configurable": {"session_id": "chat-42", "thread_id": "chat-42:default"}},
)
```

Every tool declares a `config: RunnableConfig` parameter (LangChain injects it
and hides it from the model's schema), resolves it to a `TokenProvider`, and
builds a `ZohoCRMClient` from that. A request for chat A therefore cannot pick
up chat B's tokens even under concurrency.

**Credentials never reach the model.** There is deliberately no `connect` tool —
`session_auth.connect()` is called by your application code, so the client
secret and grant code never enter the LLM's context or a saved checkpoint. The
only auth-aware tool is `crm_connection_status`, which reports whether a
connection exists and never what it contains.

## Tools

Read: `crm_connection_status`, `crm_list_modules`, `crm_describe_module`,
`crm_list_users`, `crm_org_info`, `crm_list_records`, `crm_get_record`,
`crm_search_records`, `crm_query` (COQL), `crm_related_records`,
`crm_list_notes`, `crm_list_attachments`

Write: `crm_create_records`, `crm_update_record`, `crm_upsert_records`,
`crm_delete_records`, `crm_convert_lead`, `crm_add_note`

Deletes refuse to run until the model passes `confirmed_by_user=True`, which the
system prompt only permits after the user approves in their own words. Setting
`ZOHO_CRM_ALLOW_WRITES=false` blocks every write tool regardless.

## Why not Zoho's Python SDK?

Zoho publishes [`zohocrmsdk8-0`](https://pypi.org/project/zohocrmsdk8-0/), but
its multi-user story is
[`Initializer.switch_user()`](https://www.zoho.com/crm/developer/docs/python-sdk/v2/python-multiuser.html)
— a static method that mutates SDK-global state. In a process serving many
chats concurrently, two overlapping requests can race and call Zoho as the
wrong connection; their own docs hand you a threading workaround. The REST API
has no such state, because the token rides on each request.

Second reason: the SDK wants typed model objects (`Record`, `Field`, `Choice`),
while the LLM emits loose JSON like `{"Last_Name": "Sharma"}` — you'd unwrap
straight back to key-value pairs.

We are using Zoho's prebuilt API: the v8 REST API. The SDK is one client
wrapper over it, and it's the wrong shape for an agent.

## Notes for production

- **Data centres.** A Zoho org lives in one region and its tokens work only
  there. `region` is stored per connection, so users across regions can share
  one deployment.
- **Session lifetime.** Connections carry a Mongo TTL deadline that each command
  pushes forward, so an active chat is never cut off mid-conversation while an
  abandoned one still expires on its own. Always call `end()` on a clean close —
  it revokes at Zoho rather than just deleting locally.
- **Refresh tokens don't expire** but are capped at 20 per client; Zoho silently
  drops the oldest. A user who reconnects every chat without `end()` will start
  seeing earlier tokens invalidated.
- **Refresh stampedes.** Concurrent workers coordinate through a short lock in
  Mongo, so a burst of requests triggers one refresh, not twenty.
- **Client secrets are stored encrypted** in the self-client flow — refreshing an
  access token an hour later needs the secret again, so it can't be discarded
  after connect. Serve the connect endpoint over HTTPS only.
- **Conversation memory** falls back to in-process `MemorySaver`. With
  `ZOHO_STORE=mongo` and `langgraph-checkpoint-mongodb` installed,
  `crm_agent.py` picks it up automatically and moves threads into Mongo.
- **Scopes** are granted once at consent time. Adding a tool that needs a new
  scope means sending every existing user back through the consent screen, so
  review `DEFAULT_CRM_SCOPES` in `config.py` before going live.
- **Rate limits** are per org. The client retries 429s with backoff and honours
  `Retry-After`, then raises `ZohoRateLimited` so the caller can back off too.
