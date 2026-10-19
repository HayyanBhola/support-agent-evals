"""
api/server.py

The agent as a small web service (Phase 11), so programs other than the
Streamlit page can use it: n8n now, WhatsApp (through n8n) in Phase 12.

    python -m api.server          -> http://127.0.0.1:8000
    http://127.0.0.1:8000/docs    -> every endpoint, with a "Try it out" button

It adds NO new answering logic. /chat calls agent.support_agent.handle_message(),
the same function the chat page uses, so routing, the database answers, the
answer checker, memory, escalations and message logging all work exactly as
before and are covered by the same tests.

Endpoints
    GET  /health                             is the API up? can it reach the database?
    POST /chat                               a customer message in, the agent's reply out
    GET  /alerts/escalations                 cases the team hasn't been told about yet
    POST /alerts/escalations/{id}/sent       n8n: "I sent that alert"
    GET  /alerts/overdue                     cases waiting too long for a human
    POST /alerts/overdue/{id}/sent           n8n: "I sent that reminder"
    GET  /reports/daily                      the daily summary text

Security
  - Listens on 127.0.0.1 only: other computers can't reach it, only programs
    on this computer (n8n runs here too). Keep it that way; in Phase 12 it's
    n8n that faces the internet, never this API.
  - Optional API key: set GLOWCARE_API_KEY in .env and every request (except
    /health) must send the header  X-API-Key: <that value>.
  - /chat with "eval_mode": true (for an evaluation tool) saves nothing: no
    chat, no message log, no escalation, so no team alerts. It is refused
    (403) unless GLOWCARE_API_KEY is set, so it can't be used anonymously.
"""

import hmac
import logging
import os
import re
import time
import uuid
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator

from api import messages
from database import dashboard_queries as dq
from database import notifications as nt

load_dotenv()

API_HOST = os.getenv("API_HOST", "127.0.0.1").strip()
API_PORT = int(os.getenv("API_PORT", "8000"))
API_KEY = os.getenv("GLOWCARE_API_KEY", "").strip()

log = logging.getLogger("glowcare.api")

app = FastAPI(
    title="GlowCare Agent API",
    version="1.0 (Phase 11)",
    description="The GlowCare support agent as a web service, for n8n and later WhatsApp. "
                "FICTIONAL DEMO COMPANY.",
)

# Sent when something breaks inside the agent (database down, setup error).
# The customer is never left without a reply, and never gets a made-up one.
ERROR_REPLY = ("Sorry, I'm having a technical problem right now. Please try again in a few "
               "minutes, or contact GlowCare support directly.")


def require_key(x_api_key: Optional[str] = Header(default=None)):
    """Checks the X-API-Key header when GLOWCARE_API_KEY is set in .env.
    compare_digest takes the same time for every wrong key, so the key
    can't be guessed letter by letter from response times."""
    if API_KEY and not hmac.compare_digest((x_api_key or "").encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key header.")


# --- Health -------------------------------------------------------------------

@app.get("/health", tags=["status"])
def health():
    """For a quick check (and n8n's): no key needed, shows no customer data."""
    try:
        nt.customer_channels()
        database = "ok"
    except Exception as e:  # the API is up, the database isn't
        database = f"error: {type(e).__name__}"
    from agent.llm_client import LLM_PROVIDER, MODEL_NAME
    return {"status": "ok", "database": database, "llm": f"{LLM_PROVIDER}/{MODEL_NAME}",
            "api_key_required": bool(API_KEY)}


# --- Chat ---------------------------------------------------------------------

CHANNEL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,19}$")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000,
                         description="What the customer wrote.")
    session_id: Optional[str] = Field(
        default=None, max_length=100,
        description="Who is writing, on that channel (e.g. a phone number in Phase 12). "
                    "Messages with the same channel + session_id continue one chat "
                    "(memory, one escalation per chat). Leave empty for a one-off question.")
    channel: str = Field(default="api", description="Where the message came from: api, n8n, whatsapp, ...")
    conversation_id: Optional[int] = Field(
        default=None, description="Continue this exact chat instead (overrides session_id).")
    eval_mode: bool = Field(
        default=False,
        description="For evaluation tools: answer exactly as usual but save nothing (no chat, "
                    "message log or escalation, so no alerts). Needs GLOWCARE_API_KEY to be set; "
                    "can't be combined with session_id or conversation_id.")

    @field_validator("message")
    @classmethod
    def not_blank(cls, v):
        if not v.strip():
            raise ValueError("message is empty")
        return v.strip()

    @field_validator("channel")
    @classmethod
    def valid_channel(cls, v):
        v = (v or "api").strip().lower()
        if not CHANNEL_PATTERN.match(v):
            raise ValueError("channel: 2-20 lower-case letters, digits or _ (e.g. 'n8n', 'whatsapp')")
        if v in nt.INTERNAL_CHANNELS:
            raise ValueError(f"channel '{v}' is reserved for demo/test data")
        return v

    @field_validator("session_id")
    @classmethod
    def clean_session(cls, v):
        v = (v or "").strip()
        return v or None


@app.post("/chat", tags=["chat"], dependencies=[Depends(require_key)])
def chat(req: ChatRequest):
    """One customer message -> the agent's reply. Always answers with a
    'reply' the channel can send, even when something fails inside
    (then ok=false and 'error' says what broke).

    Besides the reply, every response carries audit metadata for evaluation
    tools: request_id, latency_ms, reason, intents, details, retrieved_chunks,
    model, prompt_tokens, completion_tokens."""
    from agent.support_agent import handle_message  # loads the AI + documents once, on first use
    from agent import llm_client

    if req.eval_mode:
        # require_key has already checked the key, but with no key configured
        # it lets everyone in. Eval mode skips all logging, so it needs a key.
        if not API_KEY:
            raise HTTPException(status_code=403,
                                detail="eval_mode requires GLOWCARE_API_KEY to be set in .env.")
        if req.session_id or req.conversation_id is not None:
            raise HTTPException(status_code=422,
                                detail="eval_mode can't be combined with session_id or conversation_id: "
                                       "nothing is saved, so there is no chat to continue.")
        conversation_id, new_chat = None, False
    elif req.conversation_id is not None:
        if not nt.conversation_exists(req.conversation_id):
            raise HTTPException(status_code=404, detail=f"No conversation #{req.conversation_id}.")
        conversation_id, new_chat = req.conversation_id, False
    elif req.session_id:
        conversation_id, new_chat = nt.find_or_create_conversation(req.channel, req.session_id)
    else:
        conversation_id, new_chat = _new_conversation(req.channel), True

    meta = {"request_id": str(uuid.uuid4()), "eval_mode": req.eval_mode,
            "model": f"{llm_client.LLM_PROVIDER}/{llm_client.MODEL_NAME}"}
    llm_client.reset_request_usage()
    started = time.perf_counter()
    try:
        # conversation_id=None (eval mode) is the test-sheet path: nothing is
        # read from or written to the chat tables.
        result = handle_message(req.message, conversation_id=conversation_id, verbose=False)
    except Exception as e:
        log.exception("handle_message failed")
        return {"ok": False, "reply": ERROR_REPLY, "conversation_id": conversation_id,
                "new_chat": new_chat, "route": None, "escalated": False, "escalation": None,
                "error": f"{type(e).__name__}: {e}",
                **meta, "latency_ms": _ms_since(started), "reason": None, "intents": None,
                "details": None, "retrieved_chunks": None, **llm_client.request_usage()}
    latency_ms = _ms_since(started)

    if new_chat and not req.session_id and req.conversation_id is None:
        # A one-off question (no session_id): nothing will continue this
        # chat, so close it. An escalated chat stays open for the team.
        from database import db
        db.close_conversation(conversation_id)

    escalation = None
    if result.get("escalation_id"):
        escalation = {"id": result["escalation_id"],
                      "reference": messages.ref(result["escalation_id"]),
                      "priority": result.get("escalation_priority"),
                      "new": result.get("escalation_new")}
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    return {
        "ok": True,
        "reply": result["answer"],
        "conversation_id": conversation_id,
        "new_chat": new_chat,
        "route": result["route"],
        "source": result.get("source"),
        "answer_type": details.get("answer_type"),
        "escalated": bool(result["escalate"]),
        "escalation": escalation,
        "memory": result.get("memory"),
        **meta,
        "latency_ms": latency_ms,
        "reason": result.get("reason"),
        "intents": list(result.get("intents") or []),
        # Decimal prices, dates etc. -> plain JSON values
        "details": jsonable_encoder(result.get("details")),
        "retrieved_chunks": _retrieved_chunks(result),
        **llm_client.request_usage(),
    }


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _retrieved_chunks(result: dict):
    """The knowledge-base chunks the answer was based on, or None when the
    route didn't search the documents. score = Chroma distance: LOWER is
    more relevant (not a 0-1 similarity)."""
    if result.get("route") != "knowledge" or not isinstance(result.get("details"), dict):
        return None
    return [{"id": c.get("id"), "text": c.get("text"), "score": c.get("distance"),
             "source": c.get("source")}
            for c in result["details"].get("chunks_used") or []]


def _new_conversation(channel: str) -> int:
    from database import db
    return db.create_conversation(channel=channel)


# --- Alerts for the team (n8n -> email) --------------------------------------------

class SentBody(BaseModel):
    priority: str = Field(..., description="The priority that was in the alert you sent.")


@app.get("/alerts/escalations", tags=["alerts"], dependencies=[Depends(require_key)])
def escalation_alerts(limit: int = Query(20, ge=1, le=100)):
    """New cases (and cases that became more urgent) the team hasn't been
    told about. Each has a ready-to-send 'subject' + 'html' (email) and
    'text' (chat apps)."""
    rows = nt.pending_escalation_alerts(limit)
    alerts = [{
        "escalation_id": r["escalation_id"],
        "reference": messages.ref(r["escalation_id"]),
        "priority": r["priority"],
        "reason": r["reason"],
        "conversation_id": r["conversation_id"],
        "channel": r["channel"],
        "upgraded": bool(r["alerted_priority"]),
        "subject": messages.escalation_subject(r),
        "text": (text := messages.escalation_alert(r)),
        "html": messages.email_html(text),
    } for r in rows]
    return {"count": len(alerts), "alerts": alerts}


@app.post("/alerts/escalations/{escalation_id}/sent", tags=["alerts"],
          dependencies=[Depends(require_key)])
def escalation_alert_sent(escalation_id: int, body: SentBody):
    try:
        found = nt.mark_escalation_alerted(escalation_id, body.priority)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail=f"No escalation {messages.ref(escalation_id)}.")
    return {"ok": True, "escalation_id": escalation_id}


@app.get("/alerts/overdue", tags=["alerts"], dependencies=[Depends(require_key)])
def overdue(limit: int = Query(20, ge=1, le=100)):
    """Open cases waiting longer than OVERDUE_HOURS_HIGH / _NORMAL (.env)."""
    rows = nt.overdue_escalations(limit)
    reminders = [{
        "escalation_id": r["escalation_id"],
        "reference": messages.ref(r["escalation_id"]),
        "priority": r["priority"],
        "hours_waiting": float(r["hours_waiting"]),
        "subject": messages.overdue_subject(r),
        "text": (text := messages.overdue_reminder(r)),
        "html": messages.email_html(text),
    } for r in rows]
    return {"count": len(reminders), "reminders": reminders}


@app.post("/alerts/overdue/{escalation_id}/sent", tags=["alerts"],
          dependencies=[Depends(require_key)])
def overdue_sent(escalation_id: int):
    if not nt.mark_reminded(escalation_id):
        raise HTTPException(status_code=404, detail=f"No escalation {messages.ref(escalation_id)}.")
    return {"ok": True, "escalation_id": escalation_id}


# --- Reports ------------------------------------------------------------------

@app.get("/reports/daily", tags=["reports"], dependencies=[Depends(require_key)])
def daily_report(days: int = Query(1, ge=1, le=31)):
    """Summary of the last `days` days for real customer channels (demo and
    test chats are left out). Same numbers as the dashboard."""
    channels = nt.customer_channels()
    if channels:
        stats = dq.message_stats(days, channels)
        esc = dq.escalation_stats(days, channels)
        unanswered = dq.unanswered_questions(days, channels, limit=5)
    else:
        stats, esc, unanswered = {}, {}, []
    text = messages.daily_summary(stats, esc, unanswered, days * 24, channels)
    return {"subject": messages.daily_subject(), "text": text, "html": messages.email_html(text),
            "channels": channels,
            "stats": {k: (float(v) if v is not None and not isinstance(v, int) else v)
                      for k, v in {**stats, **{f"escalations_{k}": v for k, v in esc.items()}}.items()},
            "unanswered": [{"question": u["question"], "times_asked": u["times_asked"]}
                           for u in unanswered]}


def main():
    import uvicorn
    # Load the agent now (documents, embedding model, product list) instead of
    # on the first message, so a setup problem shows up here at start-up and
    # the first customer doesn't wait for the loading.
    print("Loading the agent (documents, database, AI settings)...")
    from agent.support_agent import get_products
    get_products()
    print(f"GlowCare Agent API on http://{API_HOST}:{API_PORT}  (docs: http://{API_HOST}:{API_PORT}/docs)")
    print("API key required: " + ("yes (X-API-Key header)" if API_KEY else
                                  "no (set GLOWCARE_API_KEY in .env to require one)"))
    uvicorn.run(app, host=API_HOST, port=API_PORT, log_level="info")


if __name__ == "__main__":
    main()
