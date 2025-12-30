import os
import json
import sqlite3
import traceback
from datetime import datetime, timezone

from flask import (
    Flask, request, redirect, url_for,
    render_template, render_template_string
)
from flask_login import (
    LoginManager, UserMixin,
    login_user, login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# =========================================================
# CONFIG (Render env vars)
# =========================================================
APP_NAME = os.getenv("APP_NAME", "Optenor BizBot")
DB_PATH = os.getenv("DB_PATH", "database.db")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()  # MUST be https://...
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()

# Gate to prevent accidental number purchases
ALLOW_PROVISIONING = os.getenv("ALLOW_PROVISIONING", "0") == "1"

# Beta constraints / guardrails
DEFAULT_COUNTRY = os.getenv("DEFAULT_COUNTRY", "US").upper()
ALLOWED_COUNTRIES = set((os.getenv("ALLOWED_COUNTRIES", "US").upper().replace(" ", "")).split(","))
MAX_NUMBERS_PER_ACCOUNT = int(os.getenv("MAX_NUMBERS_PER_ACCOUNT", "1"))

# Call safety caps (protect your Twilio bill)
MAX_CALL_STEPS = int(os.getenv("MAX_CALL_STEPS", "8"))  # hard stop if loop occurs


# =========================================================
# APP
# =========================================================
app = Flask(__name__)
app.secret_key = SECRET_KEY

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

openai_client = None
if OpenAI is not None and os.getenv("OPENAI_API_KEY"):
    try:
        openai_client = OpenAI()
    except Exception:
        openai_client = None


# =========================================================
# TIME/UTILS
# =========================================================
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def safe_json(s: str, default):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default

def norm_phone(s: str) -> str:
    return (s or "").replace(" ", "").strip()

def base_url() -> str:
    """
    Clean base URL (no trailing slash). For Twilio we strongly prefer https.
    """
    u = (PUBLIC_BASE_URL or "").strip().rstrip("/")
    return u

def absolute_url(path: str) -> str:
    """
    Build absolute URL for Twilio callbacks.
    If PUBLIC_BASE_URL is missing, we fall back to relative paths, but that is less reliable.
    """
    p = (path or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    bu = base_url()
    if not bu:
        return p
    return bu + p


# =========================================================
# DB HELPERS + STRONG MIGRATIONS
# =========================================================
_DB_READY = False

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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

def ensure_index(conn, index_sql: str):
    try:
        conn.execute(index_sql)
    except Exception:
        # If already exists or sqlite limitation, ignore
        pass

def recreate_users_table_if_critical_missing(conn):
    """
    If users table exists but is missing critical columns from old/broken schemas,
    we rename it and create a fresh users table.
    """
    if not table_exists(conn, "users"):
        return
    cols = table_columns(conn, "users")
    critical = {"username", "password_hash"}
    if not critical.issubset(cols):
        old_name = f"users_broken_{int(datetime.now().timestamp())}"
        conn.execute(f"ALTER TABLE users RENAME TO {old_name}")

def recreate_call_logs_if_critical_missing(conn):
    if not table_exists(conn, "call_logs"):
        return
    cols = table_columns(conn, "call_logs")
    critical = {"to_number", "from_number"}
    if not critical.issubset(cols):
        old_name = f"call_logs_broken_{int(datetime.now().timestamp())}"
        conn.execute(f"ALTER TABLE call_logs RENAME TO {old_name}")

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Repair/normalize tables if past schema is incompatible
    recreate_users_table_if_critical_missing(conn)
    recreate_call_logs_if_critical_missing(conn)

    # USERS
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            password_hash TEXT,

            business_name TEXT,
            business_type TEXT,
            timezone TEXT,
            notify_email TEXT,

            greeting TEXT,
            faqs TEXT,

            mode TEXT,                 -- "message" (recommended beta) or "ai"
            capture_json TEXT,         -- json dict of what to capture
            is_onboarded INTEGER,      -- 0/1

            bizbot_number TEXT,        -- Twilio number we assign (unique per customer)
            twilio_number_sid TEXT,    -- IncomingPhoneNumber SID
            provision_status TEXT,     -- "none" | "active" | "failed"
            provision_error TEXT,
            provisioned_at_utc TEXT,

            last_voice_utc TEXT,
            created_at_utc TEXT
        )
        """
    )

    # Ensure all columns exist (safe upgrades)
    ensure_column(conn, "users", "username", "TEXT")
    ensure_column(conn, "users", "password_hash", "TEXT")

    ensure_column(conn, "users", "business_name", "TEXT")
    ensure_column(conn, "users", "business_type", "TEXT")
    ensure_column(conn, "users", "timezone", "TEXT")
    ensure_column(conn, "users", "notify_email", "TEXT")

    ensure_column(conn, "users", "greeting", "TEXT")
    ensure_column(conn, "users", "faqs", "TEXT")
    ensure_column(conn, "users", "mode", "TEXT")
    ensure_column(conn, "users", "capture_json", "TEXT")
    ensure_column(conn, "users", "is_onboarded", "INTEGER")

    ensure_column(conn, "users", "bizbot_number", "TEXT")
    ensure_column(conn, "users", "twilio_number_sid", "TEXT")
    ensure_column(conn, "users", "provision_status", "TEXT")
    ensure_column(conn, "users", "provision_error", "TEXT")
    ensure_column(conn, "users", "provisioned_at_utc", "TEXT")

    ensure_column(conn, "users", "last_voice_utc", "TEXT")
    ensure_column(conn, "users", "created_at_utc", "TEXT")

    # Unique index for username (safer than ALTER TABLE UNIQUE)
    ensure_index(conn, "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_unique ON users(username);")
    # Helpful indexes
    ensure_index(conn, "CREATE INDEX IF NOT EXISTS idx_users_bizbot_number ON users(bizbot_number);")

    # CALL LOGS
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            call_sid TEXT,
            to_number TEXT,
            from_number TEXT,
            ts_utc TEXT,
            stage TEXT,
            transcript TEXT,
            bot_reply TEXT
        )
        """
    )
    ensure_column(conn, "call_logs", "user_id", "INTEGER")
    ensure_column(conn, "call_logs", "call_sid", "TEXT")
    ensure_column(conn, "call_logs", "to_number", "TEXT")
    ensure_column(conn, "call_logs", "from_number", "TEXT")
    ensure_column(conn, "call_logs", "ts_utc", "TEXT")
    ensure_column(conn, "call_logs", "stage", "TEXT")
    ensure_column(conn, "call_logs", "transcript", "TEXT")
    ensure_column(conn, "call_logs", "bot_reply", "TEXT")

    ensure_index(conn, "CREATE INDEX IF NOT EXISTS idx_call_logs_user_id ON call_logs(user_id);")
    ensure_index(conn, "CREATE INDEX IF NOT EXISTS idx_call_logs_call_sid ON call_logs(call_sid);")

    # CALL SESSIONS (multi-turn capture)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS call_sessions (
            call_sid TEXT PRIMARY KEY,
            user_id INTEGER,
            stage TEXT,
            step_count INTEGER,
            data_json TEXT,
            created_at_utc TEXT,
            updated_at_utc TEXT
        )
        """
    )
    ensure_column(conn, "call_sessions", "call_sid", "TEXT")
    ensure_column(conn, "call_sessions", "user_id", "INTEGER")
    ensure_column(conn, "call_sessions", "stage", "TEXT")
    ensure_column(conn, "call_sessions", "step_count", "INTEGER")
    ensure_column(conn, "call_sessions", "data_json", "TEXT")
    ensure_column(conn, "call_sessions", "created_at_utc", "TEXT")
    ensure_column(conn, "call_sessions", "updated_at_utc", "TEXT")
    ensure_index(conn, "CREATE INDEX IF NOT EXISTS idx_call_sessions_user_id ON call_sessions(user_id);")

    conn.commit()
    conn.close()

def ensure_db_ready():
    global _DB_READY
    if _DB_READY:
        return
    init_db()
    _DB_READY = True

@app.before_request
def _before_any_request():
    ensure_db_ready()


# =========================================================
# TWILIO + PROVISIONING
# =========================================================
def twilio_client():
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in environment variables.")
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

def template_greeting(business_name: str, business_type: str):
    bn = business_name or "our business"
    bt = (business_type or "").lower()
    if "clinic" in bt or "medical" in bt:
        return f"Thanks for calling {bn}. How can I help?"
    if "salon" in bt or "barber" in bt:
        return f"Thanks for calling {bn}. How can I help you today?"
    if "repair" in bt:
        return f"Thanks for calling {bn}. What can I help you with?"
    return f"Thanks for calling {bn}. How can I help?"

def template_faqs(business_type: str):
    bt = (business_type or "").lower()
    if "clinic" in bt:
        return "Hours:\nLocation:\nServices:\nBooking:\nPricing policy:\n"
    if "salon" in bt:
        return "Hours:\nLocation:\nServices:\nBooking:\nPricing:\n"
    if "repair" in bt:
        return "Hours:\nLocation:\nServices:\nDrop-off/Pickup:\nPricing:\n"
    return "Hours:\nLocation:\nServices:\nBooking:\nPricing:\n"

def can_provision_for_user(user_row):
    if not ALLOW_PROVISIONING:
        return (False, "Provisioning disabled: set ALLOW_PROVISIONING=1 in Render Environment.")
    if user_row["bizbot_number"]:
        return (False, "This account already has an active BizBot number.")
    return (True, "")


# =========================================================
# SESSIONS + LOGGING
# =========================================================
def get_or_create_session(call_sid: str, user_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM call_sessions WHERE call_sid=?", (call_sid,)).fetchone()
    if row:
        conn.close()
        return row
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO call_sessions (call_sid, user_id, stage, step_count, data_json, created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (call_sid, user_id, "root", 0, json.dumps({}), now, now),
    )
    conn.commit()
    row2 = conn.execute("SELECT * FROM call_sessions WHERE call_sid=?", (call_sid,)).fetchone()
    conn.close()
    return row2

def update_session(call_sid: str, stage: str = None, data: dict = None, bump_step: bool = True):
    conn = get_db()
    row = conn.execute("SELECT * FROM call_sessions WHERE call_sid=?", (call_sid,)).fetchone()
    if not row:
        conn.close()
        return
    existing = safe_json(row["data_json"], {})
    if isinstance(data, dict):
        existing.update(data)

    new_stage = stage if stage is not None else row["stage"]
    step_count = int(row["step_count"] or 0) + (1 if bump_step else 0)

    conn.execute(
        """
        UPDATE call_sessions
        SET stage=?, step_count=?, data_json=?, updated_at_utc=?
        WHERE call_sid=?
        """,
        (new_stage, step_count, json.dumps(existing), utc_now_iso(), call_sid),
    )
    conn.commit()
    conn.close()

def delete_session(call_sid: str):
    conn = get_db()
    conn.execute("DELETE FROM call_sessions WHERE call_sid=?", (call_sid,))
    conn.commit()
    conn.close()

def log_call(user_id: int, call_sid: str, to_number: str, from_number: str, stage: str, transcript: str, bot_reply: str):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO call_logs (user_id, call_sid, to_number, from_number, ts_utc, stage, transcript, bot_reply)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, call_sid, to_number, from_number, utc_now_iso(), stage, transcript, bot_reply),
    )
    conn.commit()
    conn.close()


# =========================================================
# OPTIONAL AI
# =========================================================
def ai_reply(business_name: str, faqs: str, caller_text: str):
    if openai_client is None:
        return "Thanks. I can take a message—what’s your name and callback number?"
    developer_rules = f"""
You are BizBot, a phone receptionist for {business_name}.
Rules:
- Max 2 sentences.
- If unsure: ask one question or take a message.
- Use the FAQ if relevant. Do not invent facts.
- Never mention AI/OpenAI.
"""
    user_msg = f"FAQ:\n{faqs}\n\nCaller said:\n{caller_text}\n\nWrite ONLY the next receptionist line."
    try:
        r = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=[
                {"role": "developer", "content": developer_rules},
                {"role": "user", "content": user_msg},
            ],
        )
        out = (r.output_text or "").strip()
        return out or "Sorry—I didn’t catch that. What’s your name and callback number?"
    except Exception:
        return "Sorry—there was a problem. Please leave your name and callback number."


# =========================================================
# AUTH
# =========================================================
class User(UserMixin):
    def __init__(self, user_id, username):
        self.id = user_id
        self.username = username

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["username"])
    return None


# =========================================================
# ERRORS
# =========================================================
@app.errorhandler(500)
def _err_500(e):
    print("=== 500 ===")
    traceback.print_exc()
    return "Internal Server Error (check Render logs)", 500


# =========================================================
# ROUTES (LANDING/HEALTH)
# =========================================================
@app.route("/health")
def health():
    return {"ok": True, "app": APP_NAME, "time_utc": utc_now_iso()}

@app.route("/")
def home():
    try:
        return render_template("landing.html")
    except Exception:
        return render_template_string(
            "<h1>{{name}}</h1><p><a href='/register'>Get started</a></p>",
            name=APP_NAME,
        )

@app.route("/terms")
def terms():
    return render_template_string("""
    <h2>Terms</h2>
    <p>Optenor BizBot is provided on an early-access basis.</p>
    <p>You are responsible for complying with applicable laws, including any call recording, consent, and disclosure requirements in your jurisdiction.</p>
    <p>Service features may change during beta.</p>
    """)

@app.route("/privacy")
def privacy():
    return render_template_string("""
    <h2>Privacy</h2>
    <p>We store account details and call metadata to provide the service (e.g., phone numbers, message content, timestamps, and configuration).</p>
    <p>We do not sell your data.</p>
    <p>Contact support to request deletion.</p>
    """)


# =========================================================
# REGISTER/LOGIN/LOGOUT
# =========================================================
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            return "Username and password required", 400
        if len(password) < 8:
            return "Password must be at least 8 characters.", 400

        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO users (
                    username, password_hash,
                    mode, capture_json, is_onboarded,
                    provision_status, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    generate_password_hash(password),
                    "message",
                    json.dumps({
                        "collect_reason": True,
                        "collect_name": True,
                        "collect_callback": True,
                        "collect_preferred_time": True
                    }),
                    0,
                    "none",
                    utc_now_iso()
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return "Username already exists", 400
        conn.close()
        return redirect(url_for("login"))

    return render_template_string(
        """
        <h2>Create account</h2>
        <form method="post">
          Username<br><input name="username"><br><br>
          Password (min 8 chars)<br><input name="password" type="password"><br><br>
          <button type="submit">Continue</button>
        </form>
        <p><a href="/login">Login</a></p>
        """
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if row and check_password_hash(row["password_hash"], password):
            login_user(User(row["id"], row["username"]))
            if (row["is_onboarded"] or 0) == 0:
                return redirect(url_for("onboarding"))
            return redirect(url_for("dashboard"))
        return "Invalid credentials", 401

    return render_template_string(
        """
        <h2>Login</h2>
        <form method="post">
          Username<br><input name="username"><br><br>
          Password<br><input name="password" type="password"><br><br>
          <button type="submit">Login</button>
        </form>
        <p><a href="/register">Create account</a></p>
        """
    )

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# =========================================================
# ONBOARDING
# =========================================================
@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    if request.method == "POST":
        business_name = (request.form.get("business_name") or "").strip()
        business_type = (request.form.get("business_type") or "").strip()
        timezone_val = (request.form.get("timezone") or "").strip()
        notify_email = (request.form.get("notify_email") or "").strip()

        if not business_name:
            return "Business name is required", 400
        if not notify_email:
            return "Notification email is required (so you receive captured calls).", 400

        greeting = template_greeting(business_name, business_type)
        faqs = template_faqs(business_type)

        conn = get_db()
        conn.execute(
            """
            UPDATE users
            SET business_name=?, business_type=?, timezone=?, notify_email=?,
                greeting=?, faqs=?, is_onboarded=?
            WHERE id=?
            """,
            (business_name, business_type, timezone_val, notify_email, greeting, faqs, 1, current_user.id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    return render_template_string(
        """
        <h2>Quick setup</h2>
        <form method="post">
          Business name<br><input name="business_name" style="width:360px;"><br><br>
          Business type<br>
          <select name="business_type">
            <option>Clinic</option>
            <option>Salon</option>
            <option>Repair Shop</option>
            <option>Consultant</option>
            <option>Other</option>
          </select><br><br>
          Timezone<br>
          <select name="timezone">
            <option value="Asia/Riyadh">Asia/Riyadh</option>
            <option value="UTC">UTC</option>
            <option value="America/New_York">America/New_York</option>
            <option value="Europe/London">Europe/London</option>
          </select><br><br>
          Notification email (required)<br><input name="notify_email" style="width:360px;"><br><br>
          <button type="submit">Finish</button>
        </form>
        """
    )


# =========================================================
# DASHBOARD + INBOX + SETTINGS
# =========================================================
@app.route("/dashboard")
@login_required
def dashboard():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (current_user.id,)).fetchone()
    logs = conn.execute(
        """
        SELECT ts_utc, from_number, to_number, transcript, bot_reply
        FROM call_logs WHERE user_id=?
        ORDER BY id DESC LIMIT 15
        """,
        (current_user.id,),
    ).fetchall()
    conn.close()

    if not user:
        return redirect(url_for("logout"))

    connected = bool(user["last_voice_utc"])
    provisioning_enabled = "YES" if ALLOW_PROVISIONING else "NO"
    bu = base_url() or "(not set)"

    return render_template_string(
        """
        <h2>Dashboard</h2>
        <p>Logged in as <b>{{u.username}}</b> | <a href="/logout">Logout</a></p>
        <p><a href="/inbox">Open Inbox</a> | <a href="/terms">Terms</a> | <a href="/privacy">Privacy</a></p>

        <h3>Status</h3>
        <p><b>PUBLIC_BASE_URL:</b> <code>{{bu}}</code></p>

        {% if u.bizbot_number %}
          <p><b>Your BizBot Number:</b> {{u.bizbot_number}}</p>
          {% if connected %}
            <p>✅ Receiving calls. Last webhook: {{u.last_voice_utc}}</p>
          {% else %}
            <p>⏳ Number active. Waiting for first call…</p>
          {% endif %}
        {% else %}
          <p>❌ No BizBot number yet.</p>
        {% endif %}

        <hr>

        <h3>One-click number activation (Twilio inside Optenor)</h3>
        <p><b>Provisioning enabled:</b> {{provisioning_enabled}}</p>

        {% if not u.bizbot_number %}
          <form method="post" action="/provision-number">
            Country (beta recommended: US)<br>
            <input name="country" value="{{default_country}}" style="width:120px;"><br><br>
            Area code (optional, US only)<br>
            <input name="area_code" placeholder="e.g., 212" style="width:120px;"><br><br>
            <button type="submit">Activate BizBot Number</button>
          </form>

          {% if u.provision_status == "failed" %}
            <p style="color:#b00;"><b>Provision failed:</b> {{u.provision_error}}</p>
          {% endif %}
        {% endif %}

        <hr>

        <h3>Call Mode</h3>
        <form method="post" action="/set-mode">
          <select name="mode">
            <option value="message" {% if u.mode=="message" %}selected{% endif %}>Message Capture (recommended beta)</option>
            <option value="ai" {% if u.mode=="ai" %}selected{% endif %}>AI Mode (optional)</option>
          </select>
          <button type="submit">Save</button>
        </form>

        <h3>Recent calls</h3>
        {% if logs %}
          {% for r in logs %}
            <div style="border:1px solid #ddd; padding:10px; margin:10px 0;">
              <div><b>Time:</b> {{r.ts_utc}}</div>
              <div><b>From:</b> {{r.from_number}} <b>To:</b> {{r.to_number}}</div>
              <div><b>Caller:</b> {{r.transcript}}</div>
              <div><b>BizBot:</b> {{r.bot_reply}}</div>
            </div>
          {% endfor %}
        {% else %}
          <p>No calls logged yet.</p>
        {% endif %}
        """,
        u=user,
        logs=logs,
        bu=bu,
        provisioning_enabled=provisioning_enabled,
        default_country=DEFAULT_COUNTRY
    )

@app.route("/inbox")
@login_required
def inbox():
    conn = get_db()
    logs = conn.execute(
        """
        SELECT ts_utc, from_number, to_number, stage, transcript, bot_reply
        FROM call_logs
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 200
        """,
        (current_user.id,),
    ).fetchall()
    conn.close()

    return render_template_string(
        """
        <h2>Inbox</h2>
        <p><a href="/dashboard">Back to dashboard</a></p>
        {% if logs %}
          {% for r in logs %}
            <div style="border:1px solid #ddd; padding:10px; margin:10px 0;">
              <div><b>Time:</b> {{r.ts_utc}}</div>
              <div><b>Stage:</b> {{r.stage}}</div>
              <div><b>From:</b> {{r.from_number}} <b>To:</b> {{r.to_number}}</div>
              <div><b>Caller:</b> {{r.transcript}}</div>
              <div><b>BizBot:</b> {{r.bot_reply}}</div>
            </div>
          {% endfor %}
        {% else %}
          <p>No leads yet.</p>
        {% endif %}
        """,
        logs=logs,
    )

@app.route("/set-mode", methods=["POST"])
@login_required
def set_mode():
    mode = (request.form.get("mode") or "message").strip().lower()
    if mode not in ("message", "ai"):
        mode = "message"
    conn = get_db()
    conn.execute("UPDATE users SET mode=? WHERE id=?", (mode, current_user.id))
    conn.commit()
    conn.close()
    return redirect(url_for("dashboard"))


# =========================================================
# PROVISION NUMBER (TWILIO INSIDE PRODUCT)
# =========================================================
@app.route("/provision-number", methods=["POST"])
@login_required
def provision_number():
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id=?", (current_user.id,)).fetchone()

    ok, reason = can_provision_for_user(user)
    if not ok:
        conn.execute(
            "UPDATE users SET provision_status=?, provision_error=? WHERE id=?",
            ("failed", reason, current_user.id)
        )
        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    country = (request.form.get("country") or DEFAULT_COUNTRY).strip().upper()
    area_code = (request.form.get("area_code") or "").strip()

    if country not in ALLOWED_COUNTRIES:
        conn.execute(
            "UPDATE users SET provision_status=?, provision_error=? WHERE id=?",
            ("failed", f"Country not allowed in beta. Allowed: {', '.join(sorted(ALLOWED_COUNTRIES))}", current_user.id)
        )
        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    try:
        client = twilio_client()

        # Search available numbers
        if country == "US" and area_code:
            available = client.available_phone_numbers("US").local.list(area_code=area_code, limit=1)
        else:
            available = client.available_phone_numbers(country).local.list(limit=1)

        if not available:
            raise RuntimeError("No numbers available for that selection.")

        phone_number = available[0].phone_number

        # Buy number
        incoming = client.incoming_phone_numbers.create(
            phone_number=phone_number,
            friendly_name=f"{APP_NAME} user {current_user.id}",
        )

        # Configure webhooks (ABSOLUTE URLs)
        voice_url = absolute_url("/voice")
        status_url = absolute_url("/status")

        client.incoming_phone_numbers(incoming.sid).update(
            voice_url=voice_url,
            voice_method="POST",
            status_callback=status_url,
            status_callback_method="POST",
        )

        conn.execute(
            """
            UPDATE users
            SET bizbot_number=?, twilio_number_sid=?, provision_status=?,
                provision_error=?, provisioned_at_utc=?
            WHERE id=?
            """,
            (phone_number, incoming.sid, "active", "", utc_now_iso(), current_user.id)
        )
        conn.commit()
        conn.close()
        return redirect(url_for("dashboard"))

    except Exception as e:
        err = str(e)
        try:
            conn.execute(
                "UPDATE users SET provision_status=?, provision_error=? WHERE id=?",
                ("failed", err, current_user.id)
            )
            conn.commit()
        finally:
            conn.close()
        print("Provision error:", err)
        traceback.print_exc()
        return redirect(url_for("dashboard"))


# =========================================================
# TENANT RESOLUTION (incoming call -> correct business)
# =========================================================
def find_user_by_to_number(to_number: str):
    to_number = norm_phone(to_number)
    conn = get_db()
    row = conn.execute(
        """
        SELECT * FROM users
        WHERE REPLACE(bizbot_number,' ','') = REPLACE(?, ' ', '')
        LIMIT 1
        """,
        (to_number,),
    ).fetchone()
    conn.close()
    return row


# =========================================================
# TWILIO WEBHOOKS
# =========================================================
@app.route("/voice", methods=["GET", "POST"])
def voice():
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    to_number = request.values.get("To", "")

    vr = VoiceResponse()

    user = find_user_by_to_number(to_number)
    if not user:
        vr.say("This number is not linked to a business yet. Please call back later.")
        vr.hangup()
        return str(vr)

    # mark connected
    conn = get_db()
    conn.execute("UPDATE users SET last_voice_utc=? WHERE id=?", (utc_now_iso(), user["id"]))
    conn.commit()
    conn.close()

    sess = get_or_create_session(call_sid, user["id"])
    if int(sess["step_count"] or 0) > MAX_CALL_STEPS:
        vr.say("Sorry, something went wrong. Please call back later.")
        vr.hangup()
        return str(vr)

    greeting = user["greeting"] or template_greeting(user["business_name"], user["business_type"])

    gather = Gather(
        input="speech",
        action=absolute_url("/handle-input"),
        method="POST",
        speechTimeout="auto",
        timeout=7
    )
    gather.say(greeting)
    gather.say("Please tell me what you need.")
    vr.append(gather)

    vr.say("Sorry, I didn’t catch that. Goodbye.")
    vr.hangup()
    return str(vr)


@app.route("/handle-input", methods=["POST"])
def handle_input():
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    to_number = request.values.get("To", "")
    speech = (request.values.get("SpeechResult") or "").strip()

    vr = VoiceResponse()
    user = find_user_by_to_number(to_number)

    if not user:
        vr.say("This number is not linked to a business yet.")
        vr.hangup()
        return str(vr)

    sess = get_or_create_session(call_sid, user["id"])
    stage = (sess["stage"] or "root").strip()
    step_count = int(sess["step_count"] or 0)

    if step_count > MAX_CALL_STEPS:
        vr.say("Sorry, something went wrong. Please call back later.")
        vr.hangup()
        delete_session(call_sid)
        return str(vr)

    data = safe_json(sess["data_json"], {})
    capture = safe_json(user["capture_json"], {
        "collect_reason": True,
        "collect_name": True,
        "collect_callback": True,
        "collect_preferred_time": True
    })

    mode = (user["mode"] or "message").strip().lower()

    # If Twilio didn’t return speech, retry once
    if not speech:
        bot = "Sorry, I didn’t catch that. Please repeat."
        log_call(user["id"], call_sid, to_number, from_number, stage, "", bot)
        gather = Gather(
            input="speech",
            action=absolute_url("/handle-input"),
            method="POST",
            speechTimeout="auto",
            timeout=7
        )
        gather.say(bot)
        vr.append(gather)
        vr.say("Goodbye.")
        return str(vr)

    # =======================
    # MESSAGE CAPTURE MODE
    # =======================
    if mode == "message":
        if stage == "root":
            data["reason"] = speech
            update_session(call_sid, stage="ask_name", data=data)
            bot = "Thanks. What’s your name?"
            log_call(user["id"], call_sid, to_number, from_number, stage, speech, bot)

            gather = Gather(input="speech", action=absolute_url("/handle-input"), method="POST", speechTimeout="auto", timeout=7)
            gather.say(bot)
            vr.append(gather)
            vr.say("Goodbye.")
            return str(vr)

        elif stage == "ask_name":
            data["name"] = speech
            update_session(call_sid, stage="ask_callback", data=data)
            bot = "Thanks. What’s the best callback number?"
            log_call(user["id"], call_sid, to_number, from_number, stage, speech, bot)

            gather = Gather(input="speech", action=absolute_url("/handle-input"), method="POST", speechTimeout="auto", timeout=7)
            gather.say(bot)
            vr.append(gather)
            vr.say("Goodbye.")
            return str(vr)

        elif stage == "ask_callback":
            data["callback"] = speech
            if capture.get("collect_preferred_time", True):
                update_session(call_sid, stage="ask_time", data=data)
                bot = "Got it. What’s a good time to call you back?"
                log_call(user["id"], call_sid, to_number, from_number, stage, speech, bot)

                gather = Gather(input="speech", action=absolute_url("/handle-input"), method="POST", speechTimeout="auto", timeout=7)
                gather.say(bot)
                vr.append(gather)
                vr.say("Goodbye.")
                return str(vr)
            else:
                update_session(call_sid, stage="done", data=data)
                bot = "Perfect. We’ll get back to you soon. Goodbye."
                log_call(user["id"], call_sid, to_number, from_number, stage, speech, bot)
                vr.say(bot)
                vr.hangup()
                delete_session(call_sid)
                return str(vr)

        elif stage == "ask_time":
            data["preferred_time"] = speech
            update_session(call_sid, stage="done", data=data)
            bot = "Perfect. We’ll get back to you soon. Goodbye."
            log_call(user["id"], call_sid, to_number, from_number, stage, speech, bot)
            vr.say(bot)
            vr.hangup()
            delete_session(call_sid)
            return str(vr)

        else:
            bot = "Thanks for calling. Goodbye."
            log_call(user["id"], call_sid, to_number, from_number, stage, speech, bot)
            vr.say(bot)
            vr.hangup()
            delete_session(call_sid)
            return str(vr)

    # =======================
    # AI MODE (OPTIONAL)
    # =======================
    bot = ai_reply(user["business_name"] or APP_NAME, user["faqs"] or "", speech)
    update_session(call_sid, stage="ai", data={"last_user": speech})
    log_call(user["id"], call_sid, to_number, from_number, stage, speech, bot)

    gather = Gather(input="speech", action=absolute_url("/handle-input"), method="POST", speechTimeout="auto", timeout=7)
    gather.say(bot)
    gather.say("Anything else?")
    vr.append(gather)

    vr.say("Goodbye.")
    vr.hangup()
    return str(vr)


@app.route("/status", methods=["POST"])
def status():
    call_sid = request.values.get("CallSid", "")
    call_status = request.values.get("CallStatus", "")
    to_number = request.values.get("To", "")
    from_number = request.values.get("From", "")
    print(f"[Twilio Status] CallSid={call_sid} Status={call_status} To={to_number} From={from_number}")
    return ("", 204)


# =========================================================
# LOCAL RUN
# =========================================================
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
