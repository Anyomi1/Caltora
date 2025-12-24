import os
import sqlite3
from datetime import datetime, timezone

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template,
    render_template_string,
    flash,
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
# App setup
# =========================================================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

DB_PATH = os.getenv("DB_PATH", "database.db")

openai_client = None
if OpenAI is not None and os.getenv("OPENAI_API_KEY"):
    openai_client = OpenAI()  # reads OPENAI_API_KEY from env


# =========================================================
# DB helpers + migrations
# =========================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn, table_name: str) -> set:
    if not table_exists(conn, table_name):
        return set()
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {r["name"] for r in rows}


def ensure_column(conn, table: str, col: str, col_type: str):
    cols = table_columns(conn, table)
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


def try_create_unique_index(conn, index_name: str, table: str, col: str):
    try:
        conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table}({col})")
    except Exception:
        pass


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = get_db()
    cur = conn.cursor()

    # USERS
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            password_hash TEXT,
            password TEXT,
            business_name TEXT,
            phone TEXT,
            twilio_number TEXT,
            greeting TEXT,
            faqs TEXT,
            created_at_utc TEXT
        )
        """
    )

    # Ensure columns exist even if DB schema is older
    ensure_column(conn, "users", "username", "TEXT")
    ensure_column(conn, "users", "password_hash", "TEXT")
    ensure_column(conn, "users", "password", "TEXT")  # legacy fallback
    ensure_column(conn, "users", "business_name", "TEXT")
    ensure_column(conn, "users", "phone", "TEXT")
    ensure_column(conn, "users", "twilio_number", "TEXT")  # used for To-number mapping
    ensure_column(conn, "users", "greeting", "TEXT")
    ensure_column(conn, "users", "faqs", "TEXT")
    ensure_column(conn, "users", "created_at_utc", "TEXT")
    try_create_unique_index(conn, "idx_users_username_unique", "users", "username")

    # CALL LOGS
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS call_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_sid TEXT,
            from_number TEXT,
            to_number TEXT,
            ts_utc TEXT,
            speech TEXT,
            reply TEXT
        )
        """
    )
    ensure_column(conn, "call_logs", "call_sid", "TEXT")
    ensure_column(conn, "call_logs", "from_number", "TEXT")
    ensure_column(conn, "call_logs", "to_number", "TEXT")
    ensure_column(conn, "call_logs", "ts_utc", "TEXT")
    ensure_column(conn, "call_logs", "speech", "TEXT")
    ensure_column(conn, "call_logs", "reply", "TEXT")

    conn.commit()
    conn.close()


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


def verify_password(row, password: str) -> bool:
    # New system (hashed)
    if row.get("password_hash"):
        try:
            return check_password_hash(row["password_hash"], password)
        except Exception:
            return False
    # Legacy fallback (plaintext)
    if row.get("password"):
        return row["password"] == password
    return False


# =========================================================
# AI receptionist
# =========================================================
def ai_receptionist_reply(business_name: str, greeting: str, faqs: str, caller_text: str) -> str:
    business_name = (business_name or "the business").strip()
    greeting = (greeting or f"Thanks for calling {business_name}. How can I help?").strip()
    faqs = (faqs or "").strip()
    caller_text = (caller_text or "").strip()

    if openai_client is None:
        return "Thanks. Can I get your name and what you’re calling about?"

    instructions = f"""
You are BizBot, the receptionist for {business_name}.
Your output will be spoken on a phone call.
Rules:
- Be professional and concise (max 2 sentences).
- Use the FAQ if it contains the answer.
- If unsure, ask ONE clarifying question OR take a message.
- If caller wants to book, ask for name + preferred day/time.
- Never invent hours, location, pricing, or services not in the FAQ.
"""

    user_input = f"""
Greeting used: {greeting}

Business FAQ:
{faqs}

Caller said: {caller_text}

Write the next receptionist line (max 2 sentences).
"""

    try:
        resp = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "developer", "content": instructions},
                {"role": "user", "content": user_input},
            ],
        )
        text = (resp.output_text or "").strip()
        return text or "Sorry, I didn’t catch that. What’s your name and what are you calling about?"
    except Exception:
        return "Sorry—there was a problem on our side. Please leave your name and what you’re calling about."


# =========================================================
# Multi-tenant mapping (To-number -> business)
# =========================================================
def normalize_phone(s: str) -> str:
    return (s or "").replace(" ", "").strip()


def get_business_config_for_to_number(to_number: str):
    """
    Find the correct business by the Twilio number the caller dialed (To).
    If not found, fall back to first user.
    """
    to_number = normalize_phone(to_number)

    conn = get_db()
    row = None

    if to_number:
        row = conn.execute(
            """
            SELECT business_name, greeting, faqs
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
            SELECT business_name, greeting, faqs
            FROM users
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()

    conn.close()

    if row:
        return (
            (row["business_name"] or "Caltora"),
            (row["greeting"] or "Thanks for calling. How can I help?"),
            (row["faqs"] or ""),
        )

    return ("Caltora", "Thanks for calling. How can I help?", "")


def log_call(call_sid: str, from_number: str, to_number: str, speech: str, reply: str):
    try:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO call_logs (call_sid, from_number, to_number, ts_utc, speech, reply)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (call_sid, from_number, to_number, utc_now_iso(), speech, reply),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


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
        <h1>Caltora</h1>
        <p>AI receptionist for small businesses.</p>
        <p><a href="/register">Register</a> | <a href="/login">Login</a></p>
        """
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
        business_name = (request.form.get("business_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        twilio_number = (request.form.get("twilio_number") or "").strip()
        greeting = (request.form.get("greeting") or "").strip()
        faqs = (request.form.get("faqs") or "").strip()

        if not username or not password:
            return "Username and password are required.", 400

        pw_hash = generate_password_hash(password)

        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, business_name, phone, twilio_number, greeting, faqs, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (username, pw_hash, business_name, phone, twilio_number, greeting, faqs, utc_now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return "That username is already taken. Try another.", 400
        except Exception as e:
            conn.close()
            return f"Registration error: {e}", 500
        conn.close()

        return redirect(url_for("login"))

    return render_template_string(
        """
    <h2>Register</h2>
    <form method="post">
      <label>Username</label><br><input name="username"><br><br>
      <label>Password</label><br><input name="password" type="password"><br><br>

      <label>Business Name (optional)</label><br><input name="business_name"><br><br>
      <label>Your Phone (optional)</label><br><input name="phone" placeholder="+966..."><br><br>

      <label>Twilio Number (the number customers call) (optional)</label><br>
      <input name="twilio_number" placeholder="+1..." /><br><br>

      <label>Greeting (optional)</label><br><input name="greeting" placeholder="Thanks for calling..."><br><br>

      <label>FAQs (optional)</label><br>
      <textarea name="faqs" rows="6" cols="60" placeholder="Hours, location, services, pricing..."></textarea><br><br>

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

        if row and verify_password(row, password):
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
        """
        SELECT business_name, phone, twilio_number, greeting, faqs
        FROM users WHERE id = ?
        """,
        (current_user.id,),
    ).fetchone()

    logs = conn.execute(
        """
        SELECT ts_utc, from_number, to_number, speech, reply
        FROM call_logs
        ORDER BY id DESC
        LIMIT 25
        """
    ).fetchall()
    conn.close()

    business_name = user_row["business_name"] if user_row else ""
    phone = user_row["phone"] if user_row else ""
    twilio_number = user_row["twilio_number"] if user_row else ""
    greeting = user_row["greeting"] if user_row else ""
    faqs = user_row["faqs"] if user_row else ""

    return render_template_string(
        """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8"/>
      <meta name="viewport" content="width=device-width, initial-scale=1"/>
      <title>BizBot Dashboard</title>
    </head>
    <body style="font-family:Arial, sans-serif; max-width: 960px; margin: 0 auto; padding: 30px;">
      <h2>Dashboard</h2>
      <p>Logged in as <b>{{ username }}</b> | <a href="/logout">Logout</a></p>

      {% with messages = get_flashed_messages() %}
        {% if messages %}
          <div style="padding:10px;border:1px solid #cfc;border-radius:6px;margin:10px 0;">
            {{ messages[0] }}
          </div>
        {% endif %}
      {% endwith %}

      <h3>Your BizBot Settings</h3>
      <form method="post" action="/update-settings">
        <label>Business Name</label><br>
        <input name="business_name" value="{{ business_name }}" style="width:100%; padding:10px;"><br><br>

        <label>Your Phone</label><br>
        <input name="phone" value="{{ phone }}" style="width:100%; padding:10px;" placeholder="+966..."><br><br>

        <label>Twilio Number (the number customers call)</label><br>
        <input name="twilio_number" value="{{ twilio_number }}" style="width:100%; padding:10px;" placeholder="+1..."><br><br>

        <label>Greeting</label><br>
        <input name="greeting" value="{{ greeting }}" style="width:100%; padding:10px;"><br><br>

        <label>FAQs</label><br>
        <textarea name="faqs" rows="7" style="width:100%; padding:10px;">{{ faqs }}</textarea><br><br>

        <button type="submit" style="padding:12px 18px;">Save</button>
      </form>

      <h3 style="margin-top:30px;">Recent Calls</h3>
      {% if logs and logs|length > 0 %}
        {% for r in logs %}
          <div style="padding:10px;border:1px solid #ddd;border-radius:6px;margin:10px 0;">
            <div><b>Time:</b> {{ r.ts_utc }}</div>
            <div><b>From:</b> {{ r.from_number }}</div>
            <div><b>To:</b> {{ r.to_number }}</div>
            <div><b>Caller:</b> {{ r.speech }}</div>
            <div><b>BizBot:</b> {{ r.reply }}</div>
          </div>
        {% endfor %}
      {% else %}
        <p>No calls logged yet.</p>
      {% endif %}
    </body>
    </html>
    """,
        username=current_user.username,
        business_name=business_name or "",
        phone=phone or "",
        twilio_number=twilio_number or "",
        greeting=greeting or "",
        faqs=faqs or "",
        logs=logs,
    )


@app.route("/update-settings", methods=["POST"])
@login_required
def update_settings():
    business_name = (request.form.get("business_name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    twilio_number = (request.form.get("twilio_number") or "").strip()
    greeting = (request.form.get("greeting") or "").strip()
    faqs = (request.form.get("faqs") or "").strip()

    conn = get_db()
    conn.execute(
        """
        UPDATE users
        SET business_name=?, phone=?, twilio_number=?, greeting=?, faqs=?
        WHERE id=?
        """,
        (business_name, phone, twilio_number, greeting, faqs, current_user.id),
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
# Twilio Voice Webhooks
# =========================================================
@app.route("/voice", methods=["GET", "POST"])
def voice():
    to_number = request.values.get("To", "")
    business_name, greeting, _faqs = get_business_config_for_to_number(to_number)

    vr = VoiceResponse()
    vr.say(greeting or f"Thanks for calling {business_name}. How can I help?")

    gather = Gather(
        input="speech",
        action="/handle-input",
        method="POST",
        speechTimeout="auto",
        timeout=6,
    )
    gather.say("Please tell me what you need.")
    vr.append(gather)

    vr.say("Sorry, I didn’t catch that. Please call back or try again.")
    return str(vr)


@app.route("/handle-input", methods=["POST"])
def handle_input():
    call_sid = request.values.get("CallSid", "")
    from_number = request.values.get("From", "")
    to_number = request.values.get("To", "")
    speech = (request.values.get("SpeechResult") or "").strip()

    business_name, greeting, faqs = get_business_config_for_to_number(to_number)

    vr = VoiceResponse()

    if not speech:
        vr.say("Sorry, I didn’t catch that. Please say that again.")
        gather = Gather(
            input="speech",
            action="/handle-input",
            method="POST",
            speechTimeout="auto",
            timeout=6,
        )
        gather.say("Go ahead.")
        vr.append(gather)
        vr.say("Thanks for calling. Goodbye.")
        return str(vr)

    reply = ai_receptionist_reply(business_name, greeting, faqs, speech)
    log_call(call_sid, from_number, to_number, speech, reply)

    vr.say(reply)

    gather = Gather(
        input="speech",
        action="/handle-input",
        method="POST",
        speechTimeout="auto",
        timeout=6,
    )
    gather.say("Anything else?")
    vr.append(gather)

    vr.say("Thanks for calling. Goodbye.")
    return str(vr)


# =========================================================
# Main
# =========================================================
if __name__ == "__main__":
    init_db()
    app.run(debug=True)
