import os
import json
import sqlite3
import traceback
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template,
    render_template_string,
    flash,
    abort,
)
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    login_required,
    logout_user,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash

from twilio.twiml.voice_response import VoiceResponse, Gather

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    from jinja2 import TemplateNotFound
except Exception:
    TemplateNotFound = Exception


# =========================================================
# Configuration
# =========================================================
APP_NAME = os.getenv("APP_NAME", "Caltora BizBot")
DB_PATH = os.getenv("DB_PATH", "database.db")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")  # MUST override in production
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# Optional: protect debug routes / webhook echoes
ADMIN_DEBUG_KEY = os.getenv("ADMIN_DEBUG_KEY", "")  # set in Render if you want debug endpoints


# =========================================================
# App setup
# =========================================================
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


# =========================================================
# OpenAI client (optional)
# =========================================================
openai_client = None
if OpenAI is not None and os.getenv("OPENAI_API_KEY"):
    try:
        openai_client = OpenAI()  # reads OPENAI_API_KEY from env
    except Exception:
        openai_client = None


# =========================================================
# Database helpers + migrations
# =========================================================
_DB_READY = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db():
    """
    SQLite connection. Works on Windows + Render.
    IMPORTANT: Render filesystem can be ephemeral unless you attach a disk.
    For beta, this is OK; for real SaaS, move to Postgres.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Improve concurrency a bit
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn


def table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn, table: str) -> set:
    if not table_exists(conn, table):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def ensure_column(conn, table: str, col: str, col_type: str):
    cols = table_columns(conn, table)
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def try_create_index(conn, sql: str):
    try:
        conn.execute(sql)
    except Exception:
        pass


def init_db():
    """
    Safe to run multiple times. Creates tables and adds missing columns.
    This function is called automatically in production via before_request.
    """
    conn = get_db()
    cur = conn.cursor()

    # USERS (tenant = user row; tenant is selected by twilio_number "To")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            password_hash TEXT,
            business_name TEXT,
            phone TEXT,
            twilio_number TEXT,
            greeting TEXT,
            faqs TEXT,
            notify_email TEXT,
            created_at_utc TEXT
        )
        """
    )
    ensure_column(conn, "users", "username", "TEXT")
    ensure_column(conn, "users", "password_hash", "TEXT")
    ensure_column(conn, "users", "business_name", "TEXT")
    ensure_column(conn, "users", "phone", "TEXT")
    ensure_column(conn, "users", "twilio_number", "TEXT")  # key for To-number mapping
    ensure_column(conn, "users", "greeting", "TEXT")
    ensure_column(conn, "users", "faqs", "TEXT")
    ensure_column(conn, "users", "notify_email", "TEXT")
    ensure_column(conn, "users", "created_at_utc", "TEXT")

    # Call sessions (stores stage + structured data per CallSid)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS call_sessions (
            call_sid TEXT PRIMARY KEY,
            to_number TEXT,
            from_number TEXT,
            intent TEXT,
            stage TEXT,
            data_json TEXT,
            created_at_utc TEXT,
            updated_at_utc TEXT
        )
        """
    )
    ensure_column(conn, "call_sessions", "to_number", "TEXT")
    ensure_column(conn, "call_sessions", "from_number", "TEXT")
    ensure_column(conn, "call_sessions", "intent", "TEXT")
    ensure_column(conn, "call_sessions", "stage", "TEXT")
    ensure_column(conn, "call_sessions", "data_json", "TEXT")
    ensure_column(conn, "call_sessions", "created_at_utc", "TEXT")
    ensure_column(conn, "call_sessions", "updated_at_utc", "TEXT")

    # Call logs (for dashboard)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT,
            to_number TEXT,
            from_number TEXT,
            ts_utc TEXT,
            transcript TEXT,
            bot_reply TEXT,
            business_name TEXT
        )
        """
    )
    ensure_column(conn, "call_logs", "call_sid", "TEXT")
    ensure_column(conn, "call_logs", "to_number", "TEXT")
    ensure_column(conn, "call_logs", "from_number", "TEXT")
    ensure_column(conn, "call_logs", "ts_utc", "TEXT")
    ensure_column(conn, "call_logs", "transcript", "TEXT")
    ensure_column(conn, "call_logs", "bot_reply", "TEXT")
    ensure_column(conn, "call_logs", "business_name", "TEXT")

    # Indexes
    try_create_index(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
    try_create_index(conn, "CREATE INDEX IF NOT EXISTS idx_users_twilio_number ON users(twilio_number)")
    try_create_index(conn, "CREATE INDEX IF NOT EXISTS idx_logs_call_sid ON call_logs(call_sid)")
    try_create_index(conn, "CREATE INDEX IF NOT EXISTS idx_logs_ts ON call_logs(ts_utc)")

    conn.commit()
    conn.close()


def ensure_db_ready():
    """
    Critical for Render/Gunicorn:
    - In production, __main__ doesn't run, so we ensure DB exists before handling any request.
    - This prevents 500 errors on /voice, /login, /register when tables are missing.
    """
    global _DB_READY
    if _DB_READY:
        return
    try:
        conn = get_db()
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        conn.close()
        init_db()
        _DB_READY = True
    except Exception:
        # Last resort
        try:
            init_db()
        finally:
            _DB_READY = True


@app.before_request
def _before_any_request():
    ensure_db_ready()


# =========================================================
# Utilities
# =========================================================
def normalize_phone(s: str) -> str:
    return (s or "").replace(" ", "").strip()


def require_debug_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not ADMIN_DEBUG_KEY:
            abort(404)
        key = request.args.get("key") or request.headers.get("X-Admin-Debug-Key", "")
        if key != ADMIN_DEBUG_KEY:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def safe_json_load(s: str, default=None):
    if default is None:
        default = {}
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


# =========================================================
# Tenant resolution: To-number -> business config
# =========================================================
def get_business_config_for_to_number(to_number: str):
    """
    Picks the correct business based on the Twilio number the caller dialed.
    Falls back to first user (beta-friendly).
    """
    to_number = normalize_phone(to_number)

    conn = get_db()
    row = None
    if to_number:
        row = conn.execute(
            """
            SELECT business_name, greeting, faqs, notify_email, twilio_number
            FROM users
            WHERE REPLACE(twilio_number, ' ', '') = REPLACE(?, ' ', '')
            ORDER BY id ASC
            LIMIT 1
            """,
            (to_number,),
        ).fetchone()

    if not row:
        row = conn.execute(
            """
            SELECT business_name, greeting, faqs, notify_email, twilio_number
            FROM users
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()

    conn.close()

    if row:
        business_name = row["business_name"] or APP_NAME
        greeting = row["greeting"] or f"Thanks for calling {business_name}. How can I help?"
        faqs = row["faqs"] or ""
        notify_email = row["notify_email"] or ""
        configured_twilio = row["twilio_number"] or ""
        return business_name, greeting, faqs, notify_email, configured_twilio

    return APP_NAME, f"Thanks for calling {APP_NAME}. How can I help?", "", "", ""


# =========================================================
# Call session state
# =========================================================
def get_or_create_call_session(call_sid: str, to_number: str, from_number: str):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM call_sessions WHERE call_sid = ?",
        (call_sid,),
    ).fetchone()

    if row:
        conn.close()
        return row

    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO call_sessions (call_sid, to_number, from_number, intent, stage, data_json, created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (call_sid, to_number, from_number, "", "root", json.dumps({}), now, now),
    )
    conn.commit()
    row2 = conn.execute("SELECT * FROM call_sessions WHERE call_sid = ?", (call_sid,)).fetchone()
    conn.close()
    return row2


def update_call_session(call_sid: str, intent: str = None, stage: str = None, data: dict = None):
    conn = get_db()
    row = conn.execute("SELECT * FROM call_sessions WHERE call_sid = ?", (call_sid,)).fetchone()
    if not row:
        conn.close()
        return

    current_data = safe_json_load(row["data_json"], {})
    if data:
        current_data.update(data)

    new_intent = intent if intent is not None else (row["intent"] or "")
    new_stage = stage if stage is not None else (row["stage"] or "root")

    conn.execute(
        """
        UPDATE call_sessions
        SET intent = ?, stage = ?, data_json = ?, updated_at_utc = ?
        WHERE call_sid = ?
        """,
        (new_intent, new_stage, json.dumps(current_data), utc_now_iso(), call_sid),
    )
    conn.commit()
    conn.close()


def log_call(call_sid: str, to_number: str, from_number: str, transcript: str, bot_reply: str, business_name: str):
    try:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO call_logs (call_sid, to_number, from_number, ts_utc, transcript, bot_reply, business_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (call_sid, to_number, from_number, utc_now_iso(), transcript, bot_reply, business_name),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# =========================================================
# AI receptionist
# =========================================================
def ai_receptionist_reply(business_name: str, greeting: str, faqs: str, caller_text: str, stage: str, data: dict) -> str:
    """
    Phone-safe, short, controlled. Uses FAQ if available. Falls back if AI is unavailable.
    """
    business_name = (business_name or APP_NAME).strip()
    greeting = (greeting or f"Thanks for calling {business_name}. How can I help?").strip()
    faqs = (faqs or "").strip()
    caller_text = (caller_text or "").strip()

    # Hard stop phrases
    lower = caller_text.lower()
    if any(x in lower for x in ["goodbye", "bye", "hang up", "never mind", "cancel"]):
        return "No problem. Thanks for calling—goodbye."

    if openai_client is None:
        # Safe deterministic fallback for beta
        if stage == "root":
            return "Thanks. Can I get your name and what you’re calling about?"
        return "Thanks. I can take a message—what’s the best callback number and the reason for your call?"

    developer_rules = f"""
You are BizBot, the phone receptionist for {business_name}.
Your response will be spoken aloud on a phone call.
You MUST follow these rules:
- Max 2 sentences.
- Be professional, concise, and helpful.
- If FAQ contains the answer, use it. Do not invent facts.
- If uncertain, ask one clarifying question OR take a message.
- If caller requests booking, ask for name + preferred day/time + callback number.
- If caller asks hours/location/services/pricing and FAQ doesn't say, say you can take a message.
- Never mention OpenAI, AI, prompts, policies, or internal details.
"""

    context = {
        "greeting": greeting,
        "faq": faqs,
        "stage": stage,
        "known_data": data,
        "caller_said": caller_text,
    }

    user_msg = f"""
Context (JSON):
{json.dumps(context, ensure_ascii=False)}

Write ONLY the next receptionist line (max 2 sentences).
"""

    try:
        resp = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "developer", "content": developer_rules},
                {"role": "user", "content": user_msg},
            ],
        )
        out = (resp.output_text or "").strip()
        if not out:
            return "Sorry, I didn’t catch that. What’s your name and what are you calling about?"
        return out
    except Exception:
        return "Sorry—there was a problem on our side. Please leave your name and what you’re calling about."


# =========================================================
# Auth
# =========================================================
class User(UserMixin):
    def __init__(self, user_id, username):
        self.id = user_id
        self.username = username


@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["username"] or "")
    return None


# =========================================================
# Error handling (prevents silent failures)
# =========================================================
@app.errorhandler(500)
def internal_error(e):
    # Log full traceback in Render logs
    print("=== INTERNAL SERVER ERROR ===")
    traceback.print_exc()
    # Friendly output
    return (
        "Internal Server Error. Check Render logs for the stack trace. "
        "If this happened on /voice, it is usually a DB init/env mismatch.",
        500,
    )


# =========================================================
# Web UI
# =========================================================
@app.route("/")
def home():
    try:
        return render_template("landing.html")
    except TemplateNotFound:
        return render_template_string(
            """
            <h1>{{app_name}}</h1>
            <p>AI receptionist for small businesses.</p>
            <p><a href="/register">Register</a> | <a href="/login">Login</a></p>
            """,
            app_name=APP_NAME,
        )


@app.route("/health")
def health():
    return "OK", 200


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/disclaimer")
def disclaimer():
    return render_template("disclaimer.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            return "Username and password are required.", 400

        business_name = (request.form.get("business_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        twilio_number = (request.form.get("twilio_number") or "").strip()
        greeting = (request.form.get("greeting") or "").strip()
        faqs = (request.form.get("faqs") or "").strip()
        notify_email = (request.form.get("notify_email") or "").strip()

        pw_hash = generate_password_hash(password)

        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, business_name, phone, twilio_number, greeting, faqs, notify_email, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (username, pw_hash, business_name, phone, twilio_number, greeting, faqs, notify_email, utc_now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return "That username is already taken. Try another.", 400
        except Exception as ex:
            conn.close()
            return f"Registration error: {ex}", 500
        conn.close()

        return redirect(url_for("login"))

    return render_template_string(
        """
        <h2>Register</h2>
        <form method="post">
          <label>Username</label><br><input name="username"><br><br>
          <label>Password</label><br><input name="password" type="password"><br><br>

          <label>Business Name</label><br><input name="business_name"><br><br>
          <label>Your Phone</label><br><input name="phone" placeholder="+966..."><br><br>

          <label>Twilio Number (the number customers call)</label><br>
          <input name="twilio_number" placeholder="+1..." /><br><br>

          <label>Greeting</label><br><input name="greeting" placeholder="Thanks for calling..."><br><br>

          <label>FAQs</label><br>
          <textarea name="faqs" rows="7" cols="60" placeholder="Hours, location, services, pricing..."></textarea><br><br>

          <label>Notify Email (optional)</label><br>
          <input name="notify_email" placeholder="you@company.com"><br><br>

          <button type="submit">Create Account</button>
        </form>
        <p><a href="/login">Already have an account? Login</a></p>
        """
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()

        if row and check_password_hash(row["password_hash"], password):
            login_user(User(row["id"], row["username"] or ""))
            return redirect(url_for("dashboard"))

        return "Invalid credentials", 401

    return render_template_string(
        """
        <h2>Login</h2>
        <form method="post">
          <label>Username</label><br><input name="username"><br><br>
          <label>Password</label><br><input name="password" type="password"><br><br>
          <button type="submit">Login</button>
        </form>
        <p><a href="/register">Create an account</a></p>
        """
    )


@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    user_row = conn.execute(
        "SELECT business_name, phone, twilio_number, greeting, faqs, notify_email FROM users WHERE id = ?",
        (current_user.id,),
    ).fetchone()

    logs = conn.execute(
        """
        SELECT ts_utc, from_number, to_number, transcript, bot_reply, business_name
        FROM call_logs
        ORDER BY id DESC
        LIMIT 25
        """
    ).fetchall()
    conn.close()

    business_name = (user_row["business_name"] or "") if user_row else ""
    phone = (user_row["phone"] or "") if user_row else ""
    twilio_number = (user_row["twilio_number"] or "") if user_row else ""
    greeting = (user_row["greeting"] or "") if user_row else ""
    faqs = (user_row["faqs"] or "") if user_row else ""
    notify_email = (user_row["notify_email"] or "") if user_row else ""

    # Setup checklist (sellable)
    checklist = []
    checklist.append(("Set your Twilio Number", bool(twilio_number)))
    checklist.append(("Set a Greeting", bool(greeting)))
    checklist.append(("Add FAQs (hours/services/etc.)", bool(faqs.strip())))
    checklist.append(("OpenAI key configured (AI replies)", bool(os.getenv("OPENAI_API_KEY"))))
    checklist.append(("Terms/Privacy/Disclaimer live", True))

    return render_template_string(
        """
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8"/>
          <meta name="viewport" content="width=device-width, initial-scale=1"/>
          <title>BizBot Dashboard</title>
        </head>
        <body style="font-family:Arial, sans-serif; max-width: 1000px; margin: 0 auto; padding: 30px;">
          <h2>Dashboard</h2>
          <p>Logged in as <b>{{ username }}</b> | <a href="/logout">Logout</a></p>

          {% with messages = get_flashed_messages() %}
            {% if messages %}
              <div style="padding:10px;border:1px solid #cfc;border-radius:6px;margin:10px 0;">
                {{ messages[0] }}
              </div>
            {% endif %}
          {% endwith %}

          <h3>Setup Checklist</h3>
          <ul>
            {% for item, ok in checklist %}
              <li>
                {% if ok %}✅{% else %}⬜{% endif %}
                {{ item }}
              </li>
            {% endfor %}
          </ul>

          <h3>Your BizBot Settings</h3>
          <form method="post" action="/update-settings">
            <label>Business Name</label><br>
            <input name="business_name" value="{{ business_name }}" style="width:100%; padding:10px;"><br><br>

            <label>Your Phone</label><br>
            <input name="phone" value="{{ phone }}" style="width:100%; padding:10px;" placeholder="+966..."><br><br>

            <label>Twilio Number (the number customers call)</label><br>
            <input name="twilio_number" value="{{ twilio_number }}" style="width:100%; padding:10px;" placeholder="+1..."><br>
            <small>Must match Twilio "To" exactly (include the + and country code).</small><br><br>

            <label>Greeting</label><br>
            <input name="greeting" value="{{ greeting }}" style="width:100%; padding:10px;"><br><br>

            <label>FAQs</label><br>
            <textarea name="faqs" rows="8" style="width:100%; padding:10px;">{{ faqs }}</textarea><br><br>

            <label>Notify Email (optional)</label><br>
            <input name="notify_email" value="{{ notify_email }}" style="width:100%; padding:10px;" placeholder="you@company.com"><br><br>

            <button type="submit" style="padding:12px 18px;">Save</button>
          </form>

          <h3 style="margin-top:30px;">Recent Calls</h3>
          {% if logs and logs|length > 0 %}
            {% for r in logs %}
              <div style="padding:10px;border:1px solid #ddd;border-radius:6px;margin:10px 0;">
                <div><b>Time:</b> {{ r.ts_utc }}</div>
                <div><b>Business:</b> {{ r.business_name }}</div>
                <div><b>From:</b> {{ r.from_number }}</div>
                <div><b>To:</b> {{ r.to_number }}</div>
                <div><b>Caller:</b> {{ r.transcript }}</div>
                <div><b>BizBot:</b> {{ r.bot_reply }}</div>
              </div>
            {% endfor %}
          {% else %}
            <p>No calls logged yet.</p>
          {% endif %}

          <hr style="margin:30px 0;">
          <p><b>Twilio Webhook URLs</b> (set in Twilio console):</p>
          <ul>
            <li><code>https://caltora.onrender.com/voice</code> (POST)</li>
            <li><code>https://caltora.onrender.com/status</code> (POST) (optional)</li>
          </ul>

        </body>
        </html>
        """,
        username=current_user.username,
        business_name=business_name,
        phone=phone,
        twilio_number=twilio_number,
        greeting=greeting,
        faqs=faqs,
        notify_email=notify_email,
        logs=logs,
        checklist=checklist,
    )


@app.route("/update-settings", methods=["POST"])
@login_required
def update_settings():
    business_name = (request.form.get("business_name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    twilio_number = (request.form.get("twilio_number") or "").strip()
    greeting = (request.form.get("greeting") or "").strip()
    faqs = (request.form.get("faqs") or "").strip()
    notify_email = (request.form.get("notify_email") or "").strip()

    conn = get_db()
    conn.execute(
        """
        UPDATE users
        SET business_name=?, phone=?, twilio_number=?, greeting=?, faqs=?, notify_email=?
        WHERE id=?
        """,
        (business_name, phone, twilio_number, greeting, faqs, notify_email, current_user.id),
    )
    conn.commit()
    conn.close()

    flash("Settings saved.")
    return redirect(url_for("dashboard"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# =========================================================
# Twilio voice webhooks
# =========================================================
@app.route("/voice", methods=["GET", "POST"])
def voice():
    """
    Entry point for incoming calls.
    Twilio will POST: CallSid, From, To, etc.
    """
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    to_number = request.values.get("To", "")

    # Ensure session exists
    get_or_create_call_session(call_sid, to_number, from_number)

    business_name, greeting, _faqs, _notify_email, configured_twilio = get_business_config_for_to_number(to_number)

    # If To-number isn't configured yet, still run (beta), but wording becomes generic.
    vr = VoiceResponse()
    vr.say(greeting)

    gather = Gather(
        input="speech",
        action="/handle-input",
        method="POST",
        speechTimeout="auto",
        timeout=7,
    )
    gather.say("Please tell me what you need.")
    vr.append(gather)

    vr.say("Sorry, I didn’t catch that. Please call back or try again.")
    return str(vr)


@app.route("/handle-input", methods=["POST"])
def handle_input():
    """
    Receives speech result from Gather.
    """
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    to_number = request.values.get("To", "")
    speech = (request.values.get("SpeechResult") or "").strip()

    # Ensure session exists and load state
    sess = get_or_create_call_session(call_sid, to_number, from_number)
    stage = sess["stage"] or "root"
    data = safe_json_load(sess["data_json"], {})

    business_name, greeting, faqs, notify_email, configured_twilio = get_business_config_for_to_number(to_number)

    vr = VoiceResponse()

    if not speech:
        vr.say("Sorry, I didn’t catch that.")
        gather = Gather(input="speech", action="/handle-input", method="POST", speechTimeout="auto", timeout=7)
        gather.say("Please say that again.")
        vr.append(gather)
        vr.say("Thanks for calling. Goodbye.")
        return str(vr)

    # Generate reply
    reply = ai_receptionist_reply(business_name, greeting, faqs, speech, stage, data)

    # Update a very light session memory (helps AI avoid repeating)
    # We keep this minimal to avoid uncontrolled state growth.
    turns = data.get("turns", [])
    turns.append({"caller": speech, "bot": reply})
    turns = turns[-6:]  # keep last 6 turns
    update_call_session(call_sid, stage="root", data={"turns": turns})

    # Log the call turn
    log_call(call_sid, to_number, from_number, speech, reply, business_name)

    vr.say(reply)

    # Continue unless goodbye
    if "goodbye" in reply.lower():
        vr.hangup()
        return str(vr)

    gather = Gather(input="speech", action="/handle-input", method="POST", speechTimeout="auto", timeout=7)
    gather.say("Anything else?")
    vr.append(gather)

    vr.say("Thanks for calling. Goodbye.")
    return str(vr)


@app.route("/status", methods=["POST"])
def status():
    """
    Optional Twilio status callback endpoint.
    Useful for debugging call lifecycle. Safe to accept even if unused.
    """
    # You can inspect Render logs to see these callbacks if enabled
    call_sid = request.values.get("CallSid", "")
    call_status = request.values.get("CallStatus", "")
    print(f"[Twilio Status] CallSid={call_sid} CallStatus={call_status}")
    return ("", 204)


# =========================================================
# Debug endpoints (optional)
# =========================================================
@app.route("/debug/echo", methods=["GET", "POST"])
@require_debug_key
def debug_echo():
    """
    Lets you see exactly what Twilio is posting.
    Protected by ADMIN_DEBUG_KEY.
    """
    payload = {k: request.values.get(k) for k in request.values.keys()}
    return {"method": request.method, "payload": payload}


@app.route("/debug/db", methods=["GET"])
@require_debug_key
def debug_db():
    """
    Quick DB sanity: counts + table list.
    """
    conn = get_db()
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    logs = conn.execute("SELECT COUNT(*) AS c FROM call_logs").fetchone()["c"]
    conn.close()
    return {"tables": [t["name"] for t in tables], "users": users, "call_logs": logs}


# =========================================================
# Main (local only)
# =========================================================
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
