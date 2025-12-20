from flask import Flask, request
import sqlite3
from datetime import datetime
import json

from twilio.twiml.voice_response import VoiceResponse, Gather

app = Flask(__name__)
DB_PATH = "database.db"


# -----------------------------
# DB
# -----------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Core call logs
    cur.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT,
            from_number TEXT,
            to_number TEXT,
            created_at TEXT,
            intent TEXT,
            stage TEXT,
            transcript TEXT,
            bot_reply TEXT
        )
    """)

    # Lightweight state machine storage per CallSid
    cur.execute("""
        CREATE TABLE IF NOT EXISTS call_state (
            call_sid TEXT PRIMARY KEY,
            intent TEXT,
            stage TEXT,
            data_json TEXT,
            updated_at TEXT
        )
    """)

    # Messages BizBot takes (for "human"/"call me back"/general messages)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT,
            from_number TEXT,
            created_at TEXT,
            name TEXT,
            phone TEXT,
            message TEXT,
            intent TEXT
        )
    """)

    conn.commit()
    conn.close()


def get_state(call_sid: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM call_state WHERE call_sid = ?", (call_sid,)).fetchone()
    conn.close()
    if not row:
        return {"intent": None, "stage": "root", "data": {}}
    try:
        data = json.loads(row["data_json"] or "{}")
    except Exception:
        data = {}
    return {"intent": row["intent"], "stage": row["stage"] or "root", "data": data}


def set_state(call_sid: str, intent: str, stage: str, data: dict):
    conn = get_db()
    conn.execute("""
        INSERT INTO call_state (call_sid, intent, stage, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(call_sid) DO UPDATE SET
            intent=excluded.intent,
            stage=excluded.stage,
            data_json=excluded.data_json,
            updated_at=excluded.updated_at
    """, (call_sid, intent, stage, json.dumps(data or {}), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def clear_state(call_sid: str):
    conn = get_db()
    conn.execute("DELETE FROM call_state WHERE call_sid = ?", (call_sid,))
    conn.commit()
    conn.close()


def log_call(call_sid, from_number, to_number, intent, stage, transcript, bot_reply):
    conn = get_db()
    conn.execute("""
        INSERT INTO call_logs (call_sid, from_number, to_number, created_at, intent, stage, transcript, bot_reply)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (call_sid, from_number, to_number, datetime.utcnow().isoformat(), intent, stage, transcript, bot_reply))
    conn.commit()
    conn.close()


# -----------------------------
# Intent detection (rule-based)
# -----------------------------
def detect_intent(text: str) -> str:
    t = (text or "").strip().lower()

    if any(k in t for k in ["appointment", "book", "booking", "schedule", "reserve"]):
        return "appointment"

    if any(k in t for k in ["hours", "open", "opening", "closing", "close", "when are you open", "what time"]):
        return "hours"

    if any(k in t for k in ["price", "pricing", "cost", "how much", "rate", "fees"]):
        return "pricing"

    if any(k in t for k in ["human", "agent", "representative", "operator", "call me back", "callback", "speak to"]):
        return "message"

    # If caller starts with a message-like statement
    if any(k in t for k in ["leave a message", "message", "voicemail"]):
        return "message"

    return "general"


def make_gather(action_url="/handle-input", prompt="Please tell me how I can help."):
    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action=action_url,
        method="POST",
        timeout=4,
        speech_timeout="auto"
    )
    gather.say(prompt, voice="alice")
    vr.append(gather)
    vr.say("I did not catch that. Goodbye.", voice="alice")
    return vr


# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def index():
    return "Caltora BizBot is running."


@app.route("/voice", methods=["GET", "POST"])
def voice():
    """
    Entry point for inbound calls.
    """
    vr = VoiceResponse()

    # Compliance-safe disclosure
    vr.say("Notice: This call may be recorded for quality and training purposes.", voice="alice")
    vr.say("Hello. Thanks for calling. How can I help you today?", voice="alice")

    gather = Gather(
        input="speech",
        action="/handle-input",
        method="POST",
        timeout=4,
        speech_timeout="auto"
    )
    gather.say("You can say appointments, hours, pricing, or leave a message.", voice="alice")
    vr.append(gather)

    vr.say("I did not hear anything. Goodbye.", voice="alice")
    return str(vr)


@app.route("/handle-input", methods=["POST"])
def handle_input():
    """
    Handles speech input and routes the call using a small state machine.
    """
    speech = request.form.get("SpeechResult", "") or ""
    call_sid = request.form.get("CallSid", "") or ""
    from_number = request.form.get("From", "") or ""
    to_number = request.form.get("To", "") or ""

    state = get_state(call_sid)
    stage = state["stage"]
    intent = state["intent"]
    data = state["data"] or {}

    vr = VoiceResponse()

    # If we got no speech, try one gentle retry then exit
    if not speech.strip():
        if stage == "root":
            reply = "I did not catch that. Please repeat what you need."
            log_call(call_sid, from_number, to_number, intent, stage, speech, reply)
            g = Gather(input="speech", action="/handle-input", method="POST", timeout=4, speech_timeout="auto")
            g.say(reply, voice="alice")
            vr.append(g)
            vr.say("Goodbye.", voice="alice")
            return str(vr)
        reply = "I still did not catch that. Goodbye."
        log_call(call_sid, from_number, to_number, intent, stage, speech, reply)
        clear_state(call_sid)
        vr.say(reply, voice="alice")
        return str(vr)

    # Stage machine
    if stage == "root":
        intent = detect_intent(speech)
        data = {"first_utterance": speech}

        if intent == "hours":
            reply = "What city or location are you asking about?"
            set_state(call_sid, intent, "hours_location", data)
            log_call(call_sid, from_number, to_number, intent, "root", speech, reply)
            return str(make_gather("/handle-input", reply))

        if intent == "pricing":
            reply = "Which service are you asking about, and what is the size of your request?"
            set_state(call_sid, intent, "pricing_details", data)
            log_call(call_sid, from_number, to_number, intent, "root", speech, reply)
            return str(make_gather("/handle-input", reply))

        if intent == "appointment":
            reply = "Sure. What day and time would you like the appointment?"
            set_state(call_sid, intent, "appt_datetime", data)
            log_call(call_sid, from_number, to_number, intent, "root", speech, reply)
            return str(make_gather("/handle-input", reply))

        if intent == "message":
            reply = "Of course. Please tell me your name."
            set_state(call_sid, intent, "msg_name", data)
            log_call(call_sid, from_number, to_number, intent, "root", speech, reply)
            return str(make_gather("/handle-input", reply))

        # General fallback: take a message instead of guessing
        reply = "Thanks. I can help best by taking a message. Please tell me your name."
        set_state(call_sid, "message", "msg_name", data)
        log_call(call_sid, from_number, to_number, "general", "root", speech, reply)
        return str(make_gather("/handle-input", reply))

    # HOURS flow
    if stage == "hours_location":
        data["location"] = speech.strip()
        reply = f"Thanks. I noted {data['location']}. Our hours vary by location. I will have the team confirm and follow up. Please tell me your name."
        set_state(call_sid, intent, "msg_name", data)
        log_call(call_sid, from_number, to_number, intent, stage, speech, reply)
        return str(make_gather("/handle-input", reply))

    # PRICING flow
    if stage == "pricing_details":
        data["pricing_details"] = speech.strip()
        reply = "Thanks. I noted that. Please tell me your name so the team can respond with accurate pricing."
        set_state(call_sid, intent, "msg_name", data)
        log_call(call_sid, from_number, to_number, intent, stage, speech, reply)
        return str(make_gather("/handle-input", reply))

    # APPOINTMENT flow
    if stage == "appt_datetime":
        data["appt_datetime"] = speech.strip()
        reply = "Got it. Please tell me your name."
        set_state(call_sid, intent, "appt_name", data)
        log_call(call_sid, from_number, to_number, intent, stage, speech, reply)
        return str(make_gather("/handle-input", reply))

    if stage == "appt_name":
        data["name"] = speech.strip()
        reply = "Thanks. Finally, please confirm the best phone number for a callback."
        set_state(call_sid, intent, "appt_phone", data)
        log_call(call_sid, from_number, to_number, intent, stage, speech, reply)
        return str(make_gather("/handle-input", reply))

    if stage == "appt_phone":
        data["phone"] = speech.strip()
        # Save as a message record for now (appointments become structured later)
        conn = get_db()
        conn.execute("""
            INSERT INTO messages (call_sid, from_number, created_at, name, phone, message, intent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            call_sid,
            from_number,
            datetime.utcnow().isoformat(),
            data.get("name", ""),
            data.get("phone", ""),
            f"Appointment request: {data.get('appt_datetime','')} | First: {data.get('first_utterance','')}",
            "appointment"
        ))
        conn.commit()
        conn.close()

        reply = "Perfect. I have your appointment request and contact details. The team will confirm shortly. Goodbye."
        log_call(call_sid, from_number, to_number, intent, stage, speech, reply)
        clear_state(call_sid)
        vr.say(reply, voice="alice")
        return str(vr)

    # MESSAGE flow (used by hours/pricing/general too)
    if stage == "msg_name":
        data["name"] = speech.strip()
        reply = "Thanks. Please confirm the best phone number for a callback."
        set_state(call_sid, "message", "msg_phone", data)
        log_call(call_sid, from_number, to_number, "message", stage, speech, reply)
        return str(make_gather("/handle-input", reply))

    if stage == "msg_phone":
        data["phone"] = speech.strip()
        reply = "Great. Now, please say your message."
        set_state(call_sid, "message", "msg_body", data)
        log_call(call_sid, from_number, to_number, "message", stage, speech, reply)
        return str(make_gather("/handle-input", reply))

    if stage == "msg_body":
        data["message"] = speech.strip()

        conn = get_db()
        conn.execute("""
            INSERT INTO messages (call_sid, from_number, created_at, name, phone, message, intent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            call_sid,
            from_number,
            datetime.utcnow().isoformat(),
            data.get("name", ""),
            data.get("phone", ""),
            data.get("message", ""),
            data.get("intent", "message") if data.get("intent") else "message"
        ))
        conn.commit()
        conn.close()

        reply = "Thank you. Your message has been recorded and someone will follow up shortly. Goodbye."
        log_call(call_sid, from_number, to_number, "message", stage, speech, reply)
        clear_state(call_sid)
        vr.say(reply, voice="alice")
        return str(vr)

    # Safety fallback: reset state
    reply = "Thanks. Let me restart. Please tell me how I can help."
    log_call(call_sid, from_number, to_number, intent, stage, speech, reply)
    clear_state(call_sid)
    return str(make_gather("/handle-input", reply))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
