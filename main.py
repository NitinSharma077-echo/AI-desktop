import atexit
import os
import uuid

import streamlit as st

from chat import chat as chat_module
from RAG.rag import add_pdf, clear_all


@st.cache_resource
def reset_vector_store_once():
    """
    Wipe the vector DB once per app process, at startup.

    Streamlit has no public per-session teardown hook, and a force-killed
    process skips atexit entirely -- so clearing on startup is what actually
    guarantees no documents survive from a previous session.
    """
    clear_all()
    atexit.register(clear_all)
    return True


reset_vector_store_once()

st.title("AI Desktop")

st.sidebar.title("Settings")
st.sidebar.subheader("Configuration")

coding_mode = st.sidebar.checkbox("Coding mode (route to Gemini)")

missing_keys = [
    name
    for name, value in {
        "TAVILY_API_KEY": os.getenv("TAVILY_API_KEY"),
        "Open_weather_api_key": os.getenv("Open_weather_api_key"),
        "GOOGLE_API_KEY": os.getenv("GOOGLE_API_KEY"),
    }.items()
    if not value
]
if missing_keys:
    st.info(f"Missing from .env, so those tools will error until set there: {', '.join(missing_keys)}")

if st.sidebar.button("Clear conversation"):
    st.session_state.messages = []
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.pop("last_ingested_file", None)
    clear_all()

# --- Zoho CRM -------------------------------------------------------------
# The connection is keyed by its own id rather than by thread_id, so clearing
# the conversation doesn't disconnect Zoho. Streamlit gives us no teardown hook
# to call session_auth.end() on, so abandoned connections are left to the TTL
# in Mongo (ZOHO_SESSION_TTL_SECONDS) -- Disconnect is the clean exit.
if "crm_session_id" not in st.session_state:
    st.session_state.crm_session_id = str(uuid.uuid4())

st.sidebar.subheader("Zoho CRM")


def crm_connection():
    """Current connection status, or an error if Zoho isn't configured or reachable."""
    try:
        from zoho.auth import session_auth

        return session_auth.status(st.session_state.crm_session_id), None
    except Exception as exc:
        from zoho.config import describe_db_error

        return None, describe_db_error(exc)


crm_status, crm_error = crm_connection()

if crm_error:
    st.sidebar.warning(crm_error)
elif crm_status["connected"]:
    st.sidebar.success(f"Connected ({crm_status['region']})")
    st.sidebar.caption("Prefix a message with `/crm` to run CRM commands.")
    if st.sidebar.button("Disconnect Zoho"):
        from zoho.auth import session_auth

        session_auth.end(st.session_state.crm_session_id)
        st.rerun()
else:
    from zoho.config import ACCOUNTS_DOMAINS, get_settings

    with st.sidebar.form("zoho_connect"):
        st.caption(
            "From api-console.zoho.com → Self Client. The grant code is single-use "
            "and expires in a few minutes, so paste it straight away."
        )
        client_id = st.text_input("Client ID")
        client_secret = st.text_input("Client secret", type="password")
        grant_code = st.text_input("Grant code", type="password")
        regions = sorted(ACCOUNTS_DOMAINS)
        try:
            default_region = regions.index(get_settings().default_region)
        except Exception:
            default_region = regions.index("us")
        region = st.selectbox("Data centre", regions, index=default_region)

        if st.form_submit_button("Connect"):
            from zoho.auth import session_auth

            try:
                session_auth.connect(
                    session_id=st.session_state.crm_session_id,
                    client_id=client_id,
                    client_secret=client_secret,
                    grant_code=grant_code,
                    region=region,
                )
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

st.sidebar.subheader("Documents")
uploaded_file = st.sidebar.file_uploader("Upload a PDF", type="pdf")
if uploaded_file is not None:
    file_id = f"{uploaded_file.name}-{uploaded_file.size}"
    if st.session_state.get("last_ingested_file") != file_id:
        with st.spinner("Processing PDF..."):
            num_chunks = add_pdf(uploaded_file.getvalue(), filename=uploaded_file.name)
        st.session_state.last_ingested_file = file_id
        st.sidebar.success(f"Ingested {num_chunks} chunk(s) from {uploaded_file.name}")

if "messages" not in st.session_state:
    st.session_state.messages = []
if "thread_id" not in st.session_state:
    st.session_state.thread_id = str(uuid.uuid4())

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

user_input = st.chat_input("Ask me anything, or /crm for Zoho CRM...")

if user_input:
    # `/crm` is exempt: prefixing it would route a CRM command into general
    # chat instead, where none of the CRM tools exist.
    if coding_mode and not user_input.startswith(("/coding", "/crm")):
        user_input = f"/coding {user_input}"

    st.session_state.messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    def safe_stream():
        try:
            yield from chat_module.stream_chat(
                user_input,
                st.session_state.thread_id,
                crm_session_id=st.session_state.crm_session_id,
            )
        except Exception as e:
            yield f"Something went wrong: {e}"

    with st.chat_message("assistant"):
        response = st.write_stream(safe_stream())

    st.session_state.messages.append({"role": "assistant", "content": response})
