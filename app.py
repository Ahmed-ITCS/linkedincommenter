import os
import sqlite3
import subprocess
import threading
import collections
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, Response, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("DASHBOARD_SECRET_KEY", "change-me-please")

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "admin")
DB_FILE            = "commented_posts.db"
LOG_FILE           = "linkedin_bot.log"

# ─────────────────────────────────────────────
# Bot process state
# ─────────────────────────────────────────────
bot_process: subprocess.Popen | None = None
bot_lock = threading.Lock()

# Ring buffer — last 300 log lines kept in memory for SSE streaming
log_buffer: collections.deque = collections.deque(maxlen=300)
log_buffer_lock = threading.Lock()


def _tail_log_to_buffer():
    """Background thread: tails linkedin_bot.log and pushes lines to buffer."""
    with open(LOG_FILE, "a"):  # ensure file exists
        pass
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # seek to end
        while True:
            line = f.readline()
            if line:
                with log_buffer_lock:
                    log_buffer.append(line.rstrip())
            else:
                import time
                time.sleep(0.3)


threading.Thread(target=_tail_log_to_buffer, daemon=True).start()

# ─────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────
def logged_in():
    return session.get("auth") is True

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not logged_in():
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

# ─────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def get_stats():
    try:
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) FROM commented").fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM comment_log WHERE DATE(created_at) = DATE('now')"
        ).fetchone()[0]
        conn.close()
        return {"total": total, "today": today}
    except Exception:
        return {"total": 0, "today": 0}

def get_recent_comments(limit=50):
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT urn, post_text, comment, created_at FROM comment_log ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []

# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["auth"] = True
            return redirect("/")
        error = "Wrong password"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.route("/")
@require_auth
def index():
    stats   = get_stats()
    running = bot_process is not None and bot_process.poll() is None
    return render_template("index.html", stats=stats, running=running)

@app.route("/api/stats")
@require_auth
def api_stats():
    running = bot_process is not None and bot_process.poll() is None
    stats   = get_stats()
    stats["running"] = running
    return jsonify(stats)

@app.route("/api/comments")
@require_auth
def api_comments():
    return jsonify(get_recent_comments(50))

@app.route("/api/bot/start", methods=["POST"])
@require_auth
def bot_start():
    global bot_process
    with bot_lock:
        if bot_process and bot_process.poll() is None:
            return jsonify({"ok": False, "msg": "Bot already running"})
        bot_process = subprocess.Popen(
            ["python", "bot.py"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return jsonify({"ok": True, "msg": f"Bot started (PID {bot_process.pid})"})

@app.route("/api/bot/stop", methods=["POST"])
@require_auth
def bot_stop():
    global bot_process
    with bot_lock:
        if bot_process is None or bot_process.poll() is not None:
            return jsonify({"ok": False, "msg": "Bot is not running"})
        bot_process.terminate()
        try:
            bot_process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            bot_process.kill()
        bot_process = None
    return jsonify({"ok": True, "msg": "Bot stopped"})

@app.route("/stream/logs")
@require_auth
def stream_logs():
    """SSE endpoint — sends buffered lines then streams new ones."""
    def generate():
        # First, dump current buffer
        with log_buffer_lock:
            snapshot = list(log_buffer)
        for line in snapshot:
            yield f"data: {line}\n\n"

        # Then stream new lines as they arrive
        last_seen = len(snapshot)
        import time
        while True:
            with log_buffer_lock:
                current = list(log_buffer)
            new_lines = current[last_seen:]
            for line in new_lines:
                yield f"data: {line}\n\n"
            last_seen = len(current)
            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False, threaded=True)
