"""Momin on Streamlit Community Cloud. No separate web server or tunnel."""
import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import time

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Momin — مومن", page_icon="🎙️", layout="wide")
st.markdown("<style>.block-container{padding-top:1rem;max-width:1200px}iframe{border:0}</style>", unsafe_allow_html=True)
try:
    key = st.secrets.get("GROQ_API_KEY", "") or os.environ.get("GROQ_API_KEY", "")
except FileNotFoundError:
    key = os.environ.get("GROQ_API_KEY", "")
if not key:
    st.error('Add GROQ_API_KEY in the app Settings → Secrets, then reboot the app.')
    st.stop()
os.environ["GROQ_API_KEY"] = key
import momin_engine as engine

@st.cache_resource
def frontend_directory():
    directory = Path(tempfile.mkdtemp(prefix="momin-component-"))
    # Serve only the interface, never Python files or secrets.
    directory.joinpath("index.html").write_bytes(Path(__file__).with_name("momin-app.html").read_bytes())
    return str(directory)

momin = components.declare_component("momin_voice", path=frontend_directory())
st.session_state.setdefault("momin_response", None)
st.session_state.setdefault("momin_seen", [])
st.session_state.setdefault("momin_requests", [])
request = momin(response=st.session_state.momin_response, key="momin-interface", default=None)

HANDLERS = {
    "/api/converse-text": engine.api_converse_text,
    "/api/coach-text": engine.api_coach_text,
    "/api/tts": engine.api_tts,
    "/api/report-translations": engine.report_translations,
    "/api/prepare-report": engine.prepare_report,
}

if isinstance(request, dict) and isinstance(request.get("id"), str) and request["id"] not in st.session_state.momin_seen:
    request_id = request["id"]
    st.session_state.momin_seen = (st.session_state.momin_seen + [request_id])[-100:]
    response = {"id": request_id, "status": 400, "body": {"error": "Invalid request."}}
    try:
        path, payload = request.get("path"), request.get("payload")
        if path not in HANDLERS or not isinstance(payload, dict) or len(json.dumps(payload)) > 100000:
            raise ValueError("Invalid request")
        now = time.monotonic()
        recent = [t for t in st.session_state.momin_requests if now-t < 60]
        if len(recent) >= 30:
            response.update(status=429, body={"error": "Please wait a minute before trying again."})
        else:
            st.session_state.momin_requests = recent + [now]
            result = asyncio.run(HANDLERS[path](payload))
            if hasattr(result, "body"):
                content_type = result.headers.get("content-type", "application/json")
                response.update(status=result.status_code, contentType=content_type)
                if content_type.startswith("audio/"):
                    response.update(encoding="base64", body=base64.b64encode(result.body).decode("ascii"))
                else:
                    response["body"] = json.loads(result.body)
            else:
                response.update(status=200, body=result)
    except Exception:
        response.update(status=503, body={"error": "The service is unavailable. Please try again."})
    st.session_state.momin_response = response
    st.rerun()
