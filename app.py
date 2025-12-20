from flask import Flask, request, redirect, url_for, render_template, render_template_string
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
import sqlite3
from datetime import datetime, timezone
import json
import hashlib


from twilio.twiml.voice_response import VoiceResponse, Gather

app = Flask(__name__)
app.secret_key = "dev-secret-key"  # replace later
DB_PATH = "database.db"

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# -----------------------------
# Utilities
# -----------------------------
def now_utc_iso():
    return datetime.now(timezone.utc).isoformat()

def hash_pw(pw: str) -> str:
    # MVP hashing. Upgrade to werkzeug security later.
    return hashlib.sha256((pw or "").encode("utf-8")).hexdigest()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Users (SaaS accounts)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Biz settings per user
    cur.execute("""
        CREATE TABLE IF NOT EXISTS biz_settings (
            user_id INTEGER PRIMARY KEY,
            business_name TEXT,
            twilio_number TEXT UNIQUE,       -- the Twilio number callers dial (E.164 preferred)
            greeting TEXT,
            hours_text TEXT,
            services_text TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Call logs per user
    cur.execute("""
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            call_sid TEXT,
            from_number TEXT,
            to_number TEXT,
            created_at TEXT,
            intent TEXT,
            stage TEXT,
            transcript TEXT,
            bot_reply TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Call state per call (for follow-ups)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS call_state (
            call_sid TEXT PRIMARY KEY,
            user_id INTEGER,
            intent TEXT,
            stage TEXT,
            data_json TEXT,
            updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Messages captured
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            call_sid TEXT,
            from_number TEXT,
            created_at TEXT,
            name TEXT,
            phone TEXT,
            message TEXT,
            intent TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


# -----------------------------
# Auth
# -----------------------------
class User(UserMixin):
    def __init__(self, user_id, email):
        self.id = user_id
        self.email = email

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["email"])
    return None


# -----------------------------
# SaaS: settings + routing
# -----------------------------
def get_settings_by_user(user_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM biz_settings WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row

def get_user_by_twilio_to_number(to_number: str):
    """
    Map inbound calls to the correct business using the called Twilio number (To).
    """
    if not to_number:
        return None, None

    conn = get_db()
    s = conn.execute(
        "SELECT * FROM biz_settings WHERE twilio_number = ?",
        (to_number.strip(),)
    ).fetchone()

    if not s:
        conn.close()
        return None, None

    u = conn.execute("SELECT * FROM users WHERE id = ?", (s["user_id"],)).fetchone()
    conn.close()
    return u, s

def ensure_settings(user_id: int):
    """
    Ensure there's a settings row for this user.
    """
    existing = get_settings_by_user(user_id)
    if existing:
        return

    conn = get_db()
    conn.execute("""
        INSERT INTO biz_settings (user_id, business_name, twilio_number, greeting, hours_text, services_text, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        "My Business",
        None,
        "Hello, thanks for calling. How can I help you today?",
        "Mon–Fri 9am–6pm",
        "General services",
        now_utc_iso()
    ))
    conn.commit()
    conn.close()


# -----------------------------
# Call logic (rule-based + one follow-up max)
# -----------------------------
def detect_intent(text: str) -> str:
    t = (text or "").strip().lower()

    if any(k in t for k in ["appointment", "book", "booking", "schedule", "reserve"]):
        return "appointment"
    if any(k in t for k in ["hours", "open", "opening", "closing", "close", "what time"]):
        return "hours"
    if any(k in t for k in ["price", "pricing", "cost", "how much", "rate", "fees"]):
        return "pricing"
    if any(k in t for k in ["human", "agent", "representative", "operator", "call me back", "callback", "speak to", "message"]):
        return "message"

    return "general"

def get_state(call_sid: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM call_state WHERE call_sid = ?", (call_sid,)).fetchone()
    conn.close()
    if not row:
        return {"user_id": None, "intent": None, "stage": "root", "data": {}}
    try:
        data = json.loads(row["data_json"] or "{}")
    except Exception:
        data = {}
    return {"user_id": row["user_id"], "intent": row["intent"], "stage": row["stage"] or "root", "data": data}

def set_state(call_sid: str, user_id: int, intent: str, stage: str, data: dict):
    """
    MVP-safe upsert that works regardless of prior schema drift:
    delete then insert.
    """
    conn = get_db()
    conn.execute("DELETE FROM call_state WHERE call_sid = ?", (call_sid,))
    conn.execute("""
        INSERT INTO call_state (call_sid, user_id, intent, stage, data_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (call_sid, user_id, intent, stage, json.dumps(data or {}), now_utc_iso()))
    conn.commit()
    conn.close()

def clear_state(call_sid: str):
    conn = get_db()
    conn.execute("DELETE FROM call_state WHERE call_sid = ?", (call_sid,))
    conn.commit()
    conn.close()

def log_call(user_id, call_sid, from_number, to_number, intent, stage, transcript, bot_reply):
    conn = get_db()
    conn.execute("""
        INSERT INTO call_logs (user_id, call_sid, from_number, to_number, created_at, intent, stage, transcript, bot_reply)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, call_sid, from_number, to_number, now_utc_iso(), intent, stage, transcript, bot_reply))
    conn.commit()
    conn.close()

def make_gather(prompt: str, action_url="/handle-input"):
    vr = VoiceResponse()
    g = Gather(
        input="speech",
        action=action_url,
        method="POST",
        timeout=4,
        speech_timeout="auto"
    )
    g.say(prompt, voice="alice")
    vr.append(g)
    vr.say("I did not hear anything. Goodbye.", voice="alice")
    return vr


# -----------------------------
# Web UI (SaaS)
# -----------------------------
@app.route("/", methods=["GET"])
def home():
    return render_template("landing.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""

        if not email or not pw:
            return "Email and password required", 400

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
                (email, hash_pw(pw), now_utc_iso())
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return "That email is already registered.", 400

        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        user = User(row["id"], row["email"])
        login_user(user)
        ensure_settings(user.id)
        return redirect(url_for("settings"))

    return render_template_string("""
        <h2>Register</h2>
        <form method="post">
          Email: <input name="email" type="email" /><br/>
          Password: <input name="password" type="password" /><br/>
          <button type="submit">Create Account</button>
        </form>
        <p><a href="/login">Login</a></p>
    """)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""

        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if not row or row["password_hash"] != hash_pw(pw):
            return "Invalid credentials", 401

        login_user(User(row["id"], row["email"]))
        ensure_settings(row["id"])
        return redirect(url_for("dashboard"))

    return render_template_string("""
        <h2>Login</h2>
        <form method="post">
          Email: <input name="email" type="email" /><br/>
          Password: <input name="password" type="password" /><br/>
          <button type="submit">Login</button>
        </form>
        <p><a href="/register">Register</a></p>
    """)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("home"))

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    ensure_settings(current_user.id)

    if request.method == "POST":
        business_name = (request.form.get("business_name") or "").strip()
        twilio_number = (request.form.get("twilio_number") or "").strip() or None
        greeting = (request.form.get("greeting") or "").strip()
        hours_text = (request.form.get("hours_text") or "").strip()
        services_text = (request.form.get("services_text") or "").strip()

        conn = get_db()
        try:
            conn.execute("""
                UPDATE biz_settings
                SET business_name=?, twilio_number=?, greeting=?, hours_text=?, services_text=?, updated_at=?
                WHERE user_id=?
            """, (business_name, twilio_number, greeting, hours_text, services_text, now_utc_iso(), current_user.id))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return "That Twilio number is already assigned to another account.", 400
        conn.close()

        return redirect(url_for("settings"))

    s = get_settings_by_user(current_user.id)

    return render_template_string("""
        <h2>Settings</h2>
        <p><a href="/dashboard">Dashboard</a> | <a href="/logout">Logout</a></p>

        <form method="post">
          Business Name:<br/>
          <input name="business_name" value="{{s['business_name'] or ''}}" style="width:420px"/><br/><br/>

          Your Twilio Number (the number callers dial):<br/>
          <input name="twilio_number" value="{{s['twilio_number'] or ''}}" style="width:420px" placeholder="+1XXXXXXXXXX"/><br/>
          <small>Use the exact E.164 format shown in Twilio (recommended).</small><br/><br/>

          Greeting:<br/>
          <input name="greeting" value="{{s['greeting'] or ''}}" style="width:420px"/><br/><br/>

          Hours (plain text):<br/>
          <textarea name="hours_text" rows="3" cols="70">{{s['hours_text'] or ''}}</textarea><br/><br/>

          Services (plain text):<br/>
          <textarea name="services_text" rows="4" cols="70">{{s['services_text'] or ''}}</textarea><br/><br/>

          <button type="submit">Save Settings</button>
        </form>
    """, s=s)

@app.route("/dashboard")
@login_required
def dashboard():
    ensure_settings(current_user.id)
    s = get_settings_by_user(current_user.id)

    conn = get_db()
    calls = conn.execute("""
        SELECT created_at, from_number, to_number, intent, stage, transcript, bot_reply
        FROM call_logs
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
    """, (current_user.id,)).fetchall()

    msgs = conn.execute("""
        SELECT created_at, name, phone, message, intent, from_number
        FROM messages
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
    """, (current_user.id,)).fetchall()
    conn.close()

    def row_escape(x):
        return (x or "").replace("<", "&lt;").replace(">", "&gt;")

    calls_rows = "".join([
        f"<tr><td>{row_escape(r['created_at'])}</td><td>{row_escape(r['from_number'])}</td><td>{row_escape(r['intent'])}</td><td>{row_escape(r['transcript'])}</td><td>{row_escape(r['bot_reply'])}</td></tr>"
        for r in calls
    ]) or "<tr><td colspan='5'>No calls logged yet.</td></tr>"

    msgs_rows = "".join([
        f"<tr><td>{row_escape(r['created_at'])}</td><td>{row_escape(r['name'])}</td><td>{row_escape(r['phone'])}</td><td>{row_escape(r['intent'])}</td><td>{row_escape(r['message'])}</td></tr>"
        for r in msgs
    ]) or "<tr><td colspan='5'>No messages captured yet.</td></tr>"

    return render_template_string("""
        <h2>Dashboard — {{s['business_name']}}</h2>
        <p>
          <a href="/settings">Settings</a> |
          <a href="/logout">Logout</a>
        </p>

        <h3>Recent Calls</h3>
        <table border="1" cellpadding="6">
          <tr><th>Time (UTC)</th><th>From</th><th>Intent</th><th>Transcript</th><th>Bot Reply</th></tr>
          {{calls_rows | safe}}
        </table>

        <h3>Messages</h3>
        <table border="1" cellpadding="6">
          <tr><th>Time (UTC)</th><th>Name</th><th>Phone</th><th>Intent</th><th>Message</th></tr>
          {{msgs_rows | safe}}
        </table>

        <hr/>
        <p><b>Twilio Number on File:</b> {{s['twilio_number'] or 'Not set (go to Settings)'}} </p>
    """, s=s, calls_rows=calls_rows, msgs_rows=msgs_rows)


# -----------------------------
# Twilio inbound
# -----------------------------
@app.route("/voice", methods=["GET", "POST"])
def voice():
    to_number = (request.form.get("To") or "").strip()
    u, s = get_user_by_twilio_to_number(to_number)

    # If not mapped yet, still respond safely
    business_name = s["business_name"] if s else "this business"
    greeting = s["greeting"] if s and s["greeting"] else "Hello. Thanks for calling. How can I help you today?"

    vr = VoiceResponse()
    vr.say("Notice: This call may be recorded for quality and training purposes.", voice="alice")
    vr.say(f"Welcome to {business_name}.", voice="alice")
    vr.say(greeting, voice="alice")

    g = Gather(input="speech", action="/handle-input", method="POST", timeout=4, speech_timeout="auto")
    g.say("You can say appointments, hours, pricing, or leave a message.", voice="alice")
    vr.append(g)
    vr.say("I did not hear anything. Goodbye.", voice="alice")
    return str(vr)

@app.route("/handle-input", methods=["POST"])
def handle_input():
    speech = (request.form.get("SpeechResult") or "").strip()
    call_sid = (request.form.get("CallSid") or "").strip()
    from_number = (request.form.get("From") or "").strip()
    to_number = (request.form.get("To") or "").strip()

    print("DEBUG /handle-input To:", to_number)
    print("DEBUG /handle-input SpeechResult:", speech)


    # Determine which user this call belongs to
    u, s = get_user_by_twilio_to_number(to_number)
    user_id = u["id"] if u else None

    # If unmapped, do a minimal message capture path
    if not user_id:
        vr = VoiceResponse()
        vr.say("Thanks. This number is not yet linked to an account. Please call back later.", voice="alice")
        return str(vr)

    state = get_state(call_sid)
    stage = state["stage"]
    intent = state["intent"]
    data = state["data"] or {}

    # Silence handling
    if not speech:
        if stage == "root":
            reply = "I did not catch that. Please repeat."
            log_call(user_id, call_sid, from_number, to_number, intent, stage, speech, reply)
            return str(make_gather(reply))
        reply = "I still did not catch that. Goodbye."
        log_call(user_id, call_sid, from_number, to_number, intent, stage, speech, reply)
        clear_state(call_sid)
        vr = VoiceResponse()
        vr.say(reply, voice="alice")
        return str(vr)

    # Root routing
    if stage == "root":
        intent = detect_intent(speech)
        data = {"first_utterance": speech}

        if intent == "hours":
            reply = "Which location are you asking about?"
            set_state(call_sid, user_id, intent, "hours_location", data)
            log_call(user_id, call_sid, from_number, to_number, intent, "root", speech, reply)
            return str(make_gather(reply))

        if intent == "pricing":
            reply = "Which service are you asking about?"
            set_state(call_sid, user_id, intent, "pricing_service", data)
            log_call(user_id, call_sid, from_number, to_number, intent, "root", speech, reply)
            return str(make_gather(reply))

        if intent == "appointment":
            reply = "What day and time would you like the appointment?"
            set_state(call_sid, user_id, intent, "appt_datetime", data)
            log_call(user_id, call_sid, from_number, to_number, intent, "root", speech, reply)
            return str(make_gather(reply))

        # message or general -> message capture
        reply = "Sure. Please tell me your name."
        set_state(call_sid, user_id, "message", "msg_name", data)
        log_call(user_id, call_sid, from_number, to_number, "message", "root", speech, reply)
        return str(make_gather(reply))

    # Hours follow-up (one question max -> message capture)
    if stage == "hours_location":
        data["location"] = speech
        reply = "Thanks. Please tell me your name so the team can confirm the hours and follow up."
        set_state(call_sid, user_id, "message", "msg_name", data)
        log_call(user_id, call_sid, from_number, to_number, "hours", stage, speech, reply)
        return str(make_gather(reply))

    # Pricing follow-up (one question max -> message capture)
    if stage == "pricing_service":
        data["service"] = speech
        reply = "Thanks. Please tell me your name so the team can follow up with accurate pricing."
        set_state(call_sid, user_id, "message", "msg_name", data)
        log_call(user_id, call_sid, from_number, to_number, "pricing", stage, speech, reply)
        return str(make_gather(reply))

    # Appointment flow (two-step capture)
    if stage == "appt_datetime":
        data["appt_datetime"] = speech
        reply = "Thanks. Please tell me your name."
        set_state(call_sid, user_id, "appointment", "appt_name", data)
        log_call(user_id, call_sid, from_number, to_number, "appointment", stage, speech, reply)
        return str(make_gather(reply))

    if stage == "appt_name":
        data["name"] = speech
        reply = "Please confirm the best callback phone number."
        set_state(call_sid, user_id, "appointment", "appt_phone", data)
        log_call(user_id, call_sid, from_number, to_number, "appointment", stage, speech, reply)
        return str(make_gather(reply))

    if stage == "appt_phone":
        data["phone"] = speech
        # Save as message record for MVP
        conn = get_db()
        conn.execute("""
            INSERT INTO messages (user_id, call_sid, from_number, created_at, name, phone, message, intent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            call_sid,
            from_number,
            now_utc_iso(),
            data.get("name", ""),
            data.get("phone", ""),
            f"Appointment request: {data.get('appt_datetime','')} | Caller said: {data.get('first_utterance','')}",
            "appointment"
        ))
        conn.commit()
        conn.close()

        reply = "Perfect. I have your request. The team will confirm shortly. Goodbye."
        log_call(user_id, call_sid, from_number, to_number, "appointment", stage, speech, reply)
        clear_state(call_sid)
        vr = VoiceResponse()
        vr.say(reply, voice="alice")
        return str(vr)

    # Message capture flow
    if stage == "msg_name":
        data["name"] = speech
        reply = "Thanks. What is the best callback phone number?"
        set_state(call_sid, user_id, "message", "msg_phone", data)
        log_call(user_id, call_sid, from_number, to_number, "message", stage, speech, reply)
        return str(make_gather(reply))

    if stage == "msg_phone":
        data["phone"] = speech
        reply = "Please say your message."
        set_state(call_sid, user_id, "message", "msg_body", data)
        log_call(user_id, call_sid, from_number, to_number, "message", stage, speech, reply)
        return str(make_gather(reply))

    if stage == "msg_body":
        data["message"] = speech
        conn = get_db()
        conn.execute("""
            INSERT INTO messages (user_id, call_sid, from_number, created_at, name, phone, message, intent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            call_sid,
            from_number,
            now_utc_iso(),
            data.get("name", ""),
            data.get("phone", ""),
            data.get("message", ""),
            data.get("intent", "message") if data.get("intent") else "message"
        ))
        conn.commit()
        conn.close()

        reply = "Thank you. Your message has been recorded. Goodbye."
        log_call(user_id, call_sid, from_number, to_number, "message", stage, speech, reply)
        clear_state(call_sid)
        vr = VoiceResponse()
        vr.say(reply, voice="alice")
        return str(vr)

    # Fallback reset
    reply = "Thanks. Let me restart. Please tell me how I can help."
    log_call(user_id, call_sid, from_number, to_number, intent, stage, speech, reply)
    clear_state(call_sid)
    return str(make_gather(reply))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)


LANDING_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Caltora — Never Miss Another Business Call</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {
      font-family: Arial, sans-serif;
      background: #ffffff;
      color: #111;
      max-width: 900px;
      margin: 0 auto;
      padding: 40px 20px;
      line-height: 1.6;
    }
    h1 { font-size: 42px; margin-bottom: 10px; }
    h2 { margin-top: 40px; }
    p { font-size: 18px; }
    ul { font-size: 18px; }
    .cta {
      display: inline-block;
      margin-top: 20px;
      padding: 14px 24px;
      background: #000;
      color: #fff;
      text-decoration: none;
      font-size: 18px;
      border-radius: 6px;
    }
    .box {
      background: #f7f7f7;
      padding: 20px;
      border-radius: 8px;
      margin-top: 20px;
    }
    .price {
      font-size: 28px;
      font-weight: bold;
    }
    footer {
      margin-top: 60px;
      font-size: 14px;
      color: #666;
    }
  </style>
</head>
<body>

<h1>Never miss another business call.</h1>

<p>
Caltora is an AI receptionist that answers calls when you're busy and captures messages automatically — so you don’t lose customers.
</p>

<a class="cta" href="mailto:your@email.com?subject=Caltora%20Setup%20Request">
Request setup
</a>

<h2>The problem</h2>

<p>
You miss calls when you’re:
</p>

<ul>
  <li>With another client</li>
  <li>Driving or unavailable</li>
  <li>After hours</li>
  <li>Understaffed</li>
</ul>

<p>
Missed calls mean <strong>lost business</strong>.
</p>

<h2>The solution</h2>

<p>
<strong>Caltora answers your calls for you.</strong>
</p>

<ul>
  <li>Answers calls professionally</li>
  <li>Asks what the caller needs</li>
  <li>Captures messages or appointment requests</li>
  <li>Logs everything in a simple dashboard</li>
</ul>

<h2>How it works</h2>

<ol>
  <li>A customer calls your business</li>
  <li>BizBot answers and takes the message</li>
  <li>You follow up when it suits you</li>
</ol>

<h2>What BizBot handles</h2>

<ul>
  <li>Missed calls</li>
  <li>Appointment requests</li>
  <li>Customer messages</li>
  <li>After-hours calls</li>
  <li>Call logs & summaries</li>
</ul>

<p>
If BizBot isn’t sure, it takes a message instead of guessing.
</p>

<h2>Who this is for</h2>

<p>
Caltora is built for service businesses:
</p>

<ul>
  <li>Clinics & medical practices</li>
  <li>Salons & barbers</li>
  <li>Repair shops</li>
  <li>Consultants & solo professionals</li>
</ul>

<h2>Pricing (early access)</h2>

<div class="box">
  <p class="price">$99 setup + $29/month</p>
  <ul>
    <li>AI receptionist setup</li>
    <li>24/7 call answering</li>
    <li>Message & appointment capture</li>
    <li>Dashboard access</li>
    <li>Ongoing support</li>
  </ul>
</div>

<h2>Get early access</h2>

<p>
We’re onboarding a limited number of early businesses.
</p>

<a class="cta" href="mailto:your@email.com?subject=Caltora%20Setup%20Request">
Request setup
</a>

<footer>
  © Caltora. All rights reserved.
</footer>

</body>
</html>
"""
