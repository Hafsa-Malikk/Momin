"""
Momin AI backend server for Google Colab.

Run this AFTER uploading momin-app.html to the same /content directory,
and after pip-installing dependencies (gradio, groq, edge-tts).

Usage in Colab:
    %run momin-server.py
"""
import io
import json
import re
import traceback
from pathlib import Path

import edge_tts
from google.colab import userdata
from groq import Groq
from gradio import Server
from fastapi import Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from typing import List, Dict

# ---------------------------------------------------------------------------
# API key + Groq client
# ---------------------------------------------------------------------------
GROQ_API_KEY = userdata.get("Momin")
if not GROQ_API_KEY:
    raise ValueError(
        "Secret 'Momin' not found. Click the key icon in the left sidebar, "
        "add a secret named exactly 'Momin' with your Groq API key, and enable "
        "'Notebook access' for it."
    )
client = Groq(api_key=GROQ_API_KEY, timeout=45, max_retries=1)

# ---------------------------------------------------------------------------
# Load the frontend HTML (must be uploaded first)
# ---------------------------------------------------------------------------
HTML_PATH = Path("/content/momin-app.html")
if not HTML_PATH.is_file():
    raise FileNotFoundError(
        "momin-app.html not found in /content — upload it before running this script."
    )
HTML_CONTENT = HTML_PATH.read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Speech-to-text now happens client-side in the browser (Web Speech API), so
# the transcript arrives in the request body already — no Whisper call needed.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Conversational engine: dynamic follow-ups + auto classification
# ---------------------------------------------------------------------------
CATEGORIES = ["Fire", "Medical", "Road accident", "Disaster", "Child abuse", "Harassment", "Other"]

FINALIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "finalize_assessment",
        "description": (
            "Call this once you have enough information to classify the emergency, "
            "judge how serious and how real it is, and know whether the speaker is "
            "the person affected or reporting on someone else's behalf. Do not call "
            "it before then."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": CATEGORIES},
                "confidence": {"type": "integer", "description": "0-100"},
                "role": {
                    "type": "string",
                    "enum": ["self", "witness"],
                    "description": "self = speaker is the person affected; witness = reporting for someone else",
                },
                "summary": {"type": "string", "description": "One or two sentence summary, in English"},
                "language": {"type": "string", "description": "Language the speaker is using, e.g. 'Urdu' or 'English'"},
                "guidance": {
                    "type": "string",
                    "description": (
                        "2-4 short, concrete next steps tailored specifically to what this person "
                        "described (not a generic template) — reflect the actual details they gave "
                        "you (location, who's involved, what's already been tried). Write it in the "
                        "same language they were speaking. If there is any ongoing danger to life, "
                        "the first step must tell them to call 112 immediately (Pakistan's universal "
                        "emergency number — works on any network, even without a SIM or balance) "
                        "before anything else, then give the practical steps. If it is not a genuine "
                        "life-threatening emergency, do not tell them to call 112 — give calmer, "
                        "proportionate advice instead."
                    ),
                },
            },
            "required": ["category", "confidence", "role", "summary", "language", "guidance"],
        },
    },
}

SYSTEM_PROMPT = """You are Momin, a calm emergency-response voice assistant for Pakistan. Someone has just started talking to you about a possible emergency.

LANGUAGE RULE - FOLLOW STRICTLY: Always reply in the exact same language the person just used in their most recent message. If they spoke in Urdu, your entire reply must be in Urdu. If they spoke in English, reply in English. Never switch languages on your own, even if earlier turns were in a different language than the current one.

Your job: understand what's happening through natural conversation, in whichever language they speak (English or Urdu). Ask short, targeted follow-up questions ONE AT A TIME — never a list.

Before you finalize anything, you need enough to judge:
- Severity: is this happening right now, how bad is it, is anyone in immediate danger, is it getting worse or stable.
- Authenticity: does the story hold together, are the details specific and consistent with a real situation.
- Role: whether they are the person affected or reporting on someone else's behalf. Infer this naturally from how they talk about it — do not ask a menu-style question like "are you the victim or a bystander" — only ask directly if it's genuinely unclear.

Ask at least two follow-up questions before finalizing, and more if the picture is still unclear or something doesn't add up — do not rush to a conclusion. Keep responses SHORT (1-2 sentences) — this is a spoken conversation, not a chat. Stay calm and reassuring.

Once you genuinely have enough — what happened, how serious and real it is, the category, and their role — call finalize_assessment. When you call it, write real guidance grounded in what THEY specifically told you, not a generic checklist for the category. Think about what actually matters given their exact situation before writing it."""

def converse(conversation_history):
    """
    conversation_history: list of {"role": "user"|"assistant", "content": "..."} dicts,
    NOT including the system prompt.
    Returns either:
      {"type": "question", "text": "..."}
      {"type": "complete", "category":..., "confidence":..., "role":..., "summary":..., "language":...}
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history

    # Hard guardrail: the model must not be allowed to finalize before it has asked
    # at least two follow-up questions and heard the answers — enough to judge
    # severity and authenticity, not just take the first thing at face value.
    # Smaller/faster models sometimes call an available tool even when told
    # tool_choice="none", so we don't rely on that alone — for early turns we
    # don't pass the tool to the API at all, making it physically impossible
    # for the model to call it.
    user_turns = sum(1 for m in conversation_history if m.get("role") == "user")

    if user_turns <= 2:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=messages,
            temperature=0.4,
        )
        choice = response.choices[0].message
        return {"type": "question", "text": choice.content}

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=messages,
        tools=[FINALIZE_TOOL],
        tool_choice="auto",
        temperature=0.4,
    )

    choice = response.choices[0].message

    if choice.tool_calls:
        args = json.loads(choice.tool_calls[0].function.arguments)
        return {"type": "complete", **args}

    return {"type": "question", "text": choice.content}

def detect_lang(text):
    return "ur" if re.search(r"[\u0600-\u06FF]", text) else "en"

VOICE_UR = "ur-PK-UzmaNeural"
VOICE_EN = "en-US-AriaNeural"

async def synthesize_speech(text, lang):
    voice = VOICE_UR if lang == "ur" else VOICE_EN
    communicate = edge_tts.Communicate(text, voice, rate="-10%")
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    buf.seek(0)
    data = buf.read()
    if len(data) < 1000:
        raise ValueError("Empty speech output")
    return data

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
app = Server()

@app.get("/", response_class=HTMLResponse)
def serve_app():
    return HTML_CONTENT

class ConverseRequest(BaseModel):
    history: List[Dict[str, str]]
    lang: str = "en"

@app.post("/api/converse")
async def api_converse(payload: ConverseRequest):
    try:
        print(f"[api_converse] turns={len(payload.history)} lang={payload.lang}", flush=True)
        result = converse(payload.history)
        return result
    except Exception as e:
        print("[api_converse] ERROR:", flush=True)
        traceback.print_exc()
        return {
            "type": "question",
            "text": "Sorry, could you say that again?",
            "error": str(e),
        }

@app.get("/api/tts")
async def api_tts(text: str = Query(...)):
    lang = detect_lang(text)
    try:
        audio_bytes = await synthesize_speech(text, lang)
    except Exception:
        print("[api_tts] ERROR:", flush=True)
        traceback.print_exc()
        return Response(content=b"", media_type="audio/mpeg", status_code=503)
    return Response(content=audio_bytes, media_type="audio/mpeg")

app.launch(share=True)
