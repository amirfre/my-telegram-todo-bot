import os
import re
import sqlite3
import html
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, ContextTypes, filters

TZ = ZoneInfo("Asia/Jerusalem")
DB_PATH = "tasks.db"

SECTION_ORDER = ["דחוף", "היום", "מחר", "כללי"]
SECTION_PRIORITY = {name: i for i, name in enumerate(SECTION_ORDER)}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  section TEXT NOT NULL DEFAULT 'כללי',
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  done INTEGER NOT NULL DEFAULT 0,
  done_at TEXT
);
"""



def start_health_server():
    port = int(os.environ.get("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            return

    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

# ---------------- DB ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    return conn

def now():
    return datetime.now(TZ)

def ensure_schema_migration():
    """מיגרציה אוטומטית ל-DB ישן (מוסיף section אם חסר)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'")
    exists = cur.fetchone() is not None
    if not exists:
        conn.execute(CREATE_TABLE_SQL)
        conn.commit()
        conn.close()
        return

    cur.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in cur.fetchall()}
    if "section" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN section TEXT NOT NULL DEFAULT 'כללי'")
        conn.commit()

    conn.close()

# ---------------- Parsing ----------------
def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def parse_section_from_text(raw: str) -> tuple[str, str]:
    """
    קובע סקשן לפי המילה האחרונה:
    "... דחוף" / "... היום" / "... מחר" / "... כללי"
    אם אין => כללי
    """
    text = raw.strip()
    if not text:
        return "כללי", ""

    words = text.split()
    last = words[-1].strip()

    if last in SECTION_PRIORITY:
        section = last
        task_text = " ".join(words[:-1]).strip()
        if not task_text:
            return section, ""
        return section, task_text

    return "כללי", text

def is_list_command(text: str) -> bool:
    return text in {"רשימה", "הצג רשימה", "תראה רשימה", "תציג רשימה"}

def parse_done_command(text: str) -> int | None:
    m = re.match(r"^(סיים|סיימתי)\s+(\d+)\s*$", text)
    if not m:
        return None
    return int(m.group(2))

# ---------------- Tasks ops ----------------
def add_task(chat_id: int, section: str, text: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks(chat_id, section, text, created_at, done) VALUES(?, ?, ?, ?, 0)",
        (chat_id, section, text.strip(), now().isoformat()),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id

def get_open_tasks(chat_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, section, text, created_at
        FROM tasks
        WHERE chat_id=? AND done=0
        """,
        (chat_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def ordered_open_tasks_with_numbers(chat_id: int):
    """
    מספור תצוגה 1..N לפי סדר:
    דחוף->היום->מחר->כללי, ואז created_at עולה.
    """
    rows = get_open_tasks(chat_id)

    def key(r):
        _id, section, _text, created_at = r
        pr = SECTION_PRIORITY.get(section, 999)
        return (pr, created_at, _id)

    rows_sorted = sorted(rows, key=key)

    numbered = []
    for idx, r in enumerate(rows_sorted, start=1):
        numbered.append((idx, *r))  # (display_no, id, section, text, created_at)
    return numbered

def mark_done_by_display_number(chat_id: int, display_no: int) -> bool:
    numbered = ordered_open_tasks_with_numbers(chat_id)
    target = next((x for x in numbered if x[0] == display_no), None)
    if not target:
        return False

    _, task_id, _, _, _ = target

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE tasks SET done=1, done_at=? WHERE chat_id=? AND id=? AND done=0",
        (now().isoformat(), chat_id, task_id),
    )
    conn.commit()
    changed = cur.rowcount > 0
    conn.close()
    return changed

def is_older_than_week(created_at_iso: str) -> bool:
    dt = datetime.fromisoformat(created_at_iso).astimezone(TZ)
    return now() - dt >= timedelta(days=7)

# ---- FIX: robust datetime compare in sqlite ----
def get_tasks_done_in_range(chat_id: int, start: datetime, end: datetime):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, section, text, done_at
        FROM tasks
        WHERE chat_id=? AND done=1 AND done_at IS NOT NULL
          AND datetime(done_at) >= datetime(?)
          AND datetime(done_at) < datetime(?)
        ORDER BY datetime(done_at) ASC
        """,
        (chat_id, start.isoformat(), end.isoformat()),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

# ---------------- Formatting ----------------
def header(title: str) -> str:
    safe = html.escape(title)
    return f"━━━━━━━━━━━━━━━━━━━━\n<b>{safe}</b>\n━━━━━━━━━━━━━━━━━━━━"

def indent_spaces(level: int = 6) -> str:
    # רווח Unicode רחב (EM SPACE). יותר יציב מ-" " רגיל.
    return "\u2003" * max(0, level)

def section_title(name: str) -> str:
    lines = {
        "דחוף": "━━━━━━━━━ דחוף ━━━━━━━━━",
        "היום": "━━━━━━━━━ היום ━━━━━━━━━",
        "מחר":  "━━━━━━━━━ מחר ━━━━━━━━━",
        "כללי": "━━━━━━━━━ כללי ━━━━━━━━━",
    }

    return f"\n<b>{lines.get(name, name)}</b>"


def format_open_tasks_message(chat_id: int) -> str:
    numbered = ordered_open_tasks_with_numbers(chat_id)
    if not numbered:
        return "✅ <b>אין משימות פתוחות כרגע.</b>"

    groups = {s: [] for s in SECTION_ORDER}
    for display_no, _id, section, text, created_at in numbered:
        red = "🔴 " if is_older_than_week(created_at) else ""
        groups.setdefault(section, []).append(
            f"{red}<b>{display_no}</b> — {html.escape(text)}"
        )

    lines = []
    lines.append(header("🧾 רשימת משימות פתוחות"))

    for sec in SECTION_ORDER:
        lines.append(section_title(sec))
        items = groups.get(sec, [])
        if items:
            lines.extend([f"• {it}" for it in items])
        else:
            lines.append("• (אין)")

    return "\n".join(lines)

def format_done_summary_for_range(chat_id: int, start: datetime, end: datetime) -> str:
    rows = get_tasks_done_in_range(chat_id, start, end)
    day_label = start.strftime("%d/%m/%Y")

    count = len(rows)

    lines = []
    lines.append(header(f"📅 סיכום משימות — {day_label}"))

    # שורת ספירה למעלה
    if count > 0:
        lines.append(f"\n🔥 <b>סיימת היום {count} משימות</b>\n")
    else:
        lines.append(f"\n😅 <b>סיימת היום 0 משימות יא אפס</b>\n")

    if count == 0:
        return "\n".join(lines)

    # רשימת משימות שבוצעו (בלי סקשן)
    for _id, section, text, done_at in rows:
        lines.append(f"• {html.escape(text)}")

    return "\n".join(lines)


# ---------------- Scheduled jobs ----------------
async def send_open_tasks(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await context.bot.send_message(
        chat_id=chat_id,
        text=format_open_tasks_message(chat_id),
        parse_mode=ParseMode.HTML
    )

# ---- FIX: midnight uses ref=now()-1s to target the day that just ended ----
async def send_midnight_done_summary(context: ContextTypes.DEFAULT_TYPE):
    """
    ב-00:00 מסכמים את היום שהסתיים עכשיו (אתמול).
    טריק: לוקחים now()-1s כדי להיות בטוחים שהתאריך הוא של "אתמול".
    """
    chat_id = context.job.chat_id

    ref = now() - timedelta(seconds=1)
    day_start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    await context.bot.send_message(
        chat_id=chat_id,
        text=format_done_summary_for_range(chat_id, day_start, day_end),
        parse_mode=ParseMode.HTML
    )

async def start_jobs_for_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # למנוע כפילויות
    for j in context.job_queue.get_jobs_by_name(f"rem-0900-{chat_id}"):
        j.schedule_removal()
    for j in context.job_queue.get_jobs_by_name(f"rem-1815-{chat_id}"):
        j.schedule_removal()
    for j in context.job_queue.get_jobs_by_name(f"sum-0000-{chat_id}"):
        j.schedule_removal()

    context.job_queue.run_daily(
        send_open_tasks,
        time=time(9, 0, tzinfo=TZ),
        chat_id=chat_id,
        name=f"rem-0900-{chat_id}",
    )
    context.job_queue.run_daily(
        send_open_tasks,
        time=time(18, 15, tzinfo=TZ),
        chat_id=chat_id,
        name=f"rem-1815-{chat_id}",
    )
    context.job_queue.run_daily(
        send_midnight_done_summary,
        time=time(0, 0, tzinfo=TZ),
        chat_id=chat_id,
        name=f"sum-0000-{chat_id}",
    )

# ---------------- Handlers ----------------
async def on_first_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    בלי /start:
    בהודעה הראשונה – נרשמים לתזכורות ושולחים הסבר פעם אחת.
    """
    chat_id = update.effective_chat.id
    existing = context.job_queue.get_jobs_by_name(f"rem-0900-{chat_id}")
    if not existing:
        await start_jobs_for_chat(update, context)
        await update.message.reply_text(
            header("🤖 הבוט מוכן") +
            "\n\nכל הודעה = משימה חדשה.\n"
            "כדי לשים בסקשן, תוסיף בסוף: <b>דחוף</b> / <b>היום</b> / <b>מחר</b>\n\n"
            "<b>רשימה</b> — מציג משימות פתוחות\n"
            "<b>סיים 7</b> — מסיים לפי מספר תצוגה\n",
            parse_mode=ParseMode.HTML
        )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = normalize_text(update.message.text)

    if is_list_command(text):
        await update.message.reply_text(
            format_open_tasks_message(chat_id),
            parse_mode=ParseMode.HTML
        )
        return

    done_no = parse_done_command(text)
    if done_no is not None:
        ok = mark_done_by_display_number(chat_id, done_no)
        await update.message.reply_text("סומן כבוצע ✅" if ok else "לא מצאתי מספר כזה ברשימה.")
        return

    section, task_text = parse_section_from_text(text)
    if not task_text:
        await update.message.reply_text(
            "רשום משימה, לדוגמה:\n• <b>להאכיל את ליצי דחוף</b>\n• <b>לשתות מים מחר</b>\n• <b>ללכת לים</b>",
            parse_mode=ParseMode.HTML
        )
        return

    add_task(chat_id, section, task_text)
    await update.message.reply_text(f"נוסף ✅ <b>{html.escape(section)}</b>", parse_mode=ParseMode.HTML)

# ---------------- Main ----------------
def main():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("חסר TELEGRAM_TOKEN בסביבה")

    ensure_schema_migration()

    app = Application.builder().token(token).build()

    # הודעה ראשונה: רישום תזכורות בלי /start
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_first_message), group=0)
    # טיפול בכל טקסט
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=1)

    print("🤖 הבוט רץ ומוכן (Polling)...")
    threading.Thread(target=start_health_server, daemon=True).start()
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()

