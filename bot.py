import os
import sqlite3
import logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "study_tracker.db")
TZ = ZoneInfo("Asia/Kolkata")
SUBJECTS = ["Maths", "English", "Reasoning", "GK/GS"]
MAX_SESSION_MINUTES = 360
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


def now():
    return datetime.now(TZ)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT NOT NULL,
            target_minutes INTEGER NOT NULL DEFAULT 360,
            reminder_time TEXT,
            joined_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS active_sessions (
            user_id INTEGER PRIMARY KEY,
            subject TEXT NOT NULL,
            started_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user_end ON sessions(user_id, ended_at);
        """)
        c.commit()


def register_user(user):
    name = " ".join(x for x in [user.first_name, user.last_name] if x).strip() or "Aspirant"
    username = user.username or ""
    with db() as c:
        c.execute("""
            INSERT INTO users(user_id, username, display_name, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                display_name=excluded.display_name
        """, (user.id, username, name, now().isoformat()))
        c.commit()


def user_name(row):
    return ("@" + row["username"]) if row["username"] else row["display_name"]


def parse_dt(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def period_start(period):
    n = now()
    if period == "week":
        return (n - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def total_minutes(user_id, start=None, end=None, subject=None):
    sql = "SELECT COALESCE(SUM(minutes), 0) FROM sessions WHERE user_id=?"
    args = [user_id]
    if start:
        sql += " AND ended_at >= ?"
        args.append(start.isoformat())
    if end:
        sql += " AND ended_at < ?"
        args.append(end.isoformat())
    if subject:
        sql += " AND subject=?"
        args.append(subject)
    with db() as c:
        return int(c.execute(sql, args).fetchone()[0])


def streak_for(user_id):
    with db() as c:
        rows = c.execute("""
            SELECT DISTINCT substr(ended_at, 1, 10) AS d
            FROM sessions WHERE user_id=? AND minutes > 0
            ORDER BY d DESC
        """, (user_id,)).fetchall()
    days = {r["d"] for r in rows}
    today = now().date()
    # A streak remains current if the user studied today or yesterday.
    cursor = today if today.isoformat() in days else today - timedelta(days=1)
    count = 0
    while cursor.isoformat() in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def fmt_minutes(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m" if h else f"{m}m"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    text = (
        "📚 *SSC CGL Study Tracker*\n\n"
        "Apni padhai track karo, target set karo aur aspirants ke leaderboard par apni progress dekho.\n\n"
        "*Commands*\n"
        "/study — subject choose karke timer start\n"
        "/stop — timer stop karke session save\n"
        "/today — aaj ki report\n"
        "/week — pichhle 7 din ki report\n"
        "/month — is mahine ki report\n"
        "/target 6 — daily target hours set (example: 6)\n"
        "/rank — weekly leaderboard\n"
        "/rankmonth — monthly leaderboard\n"
        "/myrank — apni rank aur streak\n"
        "/history — latest 5 sessions\n"
        "/cancel — active timer cancel (record nahi hoga)\n\n"
        "🔐 Tumhare detailed study records sirf tumhare commands par dikhte hain. Leaderboard par naam aur ranking public hoti hai."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def choose_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    with db() as c:
        active = c.execute("SELECT * FROM active_sessions WHERE user_id=?", (update.effective_user.id,)).fetchone()
    if active:
        await update.message.reply_text(
            f"⏱️ Tumhara {active['subject']} timer already chal raha hai. Pehle /stop karo."
        )
        return
    keyboard = [[InlineKeyboardButton(s, callback_data=f"subject:{s}")] for s in SUBJECTS]
    await update.message.reply_text("Kis subject ki padhai shuru kar rahe ho?", reply_markup=InlineKeyboardMarkup(keyboard))


async def subject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not q.from_user:
        return
    register_user(q.from_user)
    subject = q.data.split(":", 1)[1]
    if subject not in SUBJECTS:
        await q.edit_message_text("Unknown subject. /study se dobara try karo.")
        return
    started = now()
    with db() as c:
        active = c.execute("SELECT * FROM active_sessions WHERE user_id=?", (q.from_user.id,)).fetchone()
        if active:
            await q.edit_message_text(f"Timer already chal raha hai: {active['subject']}. Pehle /stop karo.")
            return
        c.execute("INSERT INTO active_sessions(user_id, subject, started_at) VALUES (?, ?, ?)",
                  (q.from_user.id, subject, started.isoformat()))
        c.commit()
    await q.edit_message_text(
        f"✅ *{subject} timer started!*\nStart: {started.strftime('%I:%M %p')}\n\nPadhai khatam ho to /stop bhejo.",
        parse_mode="Markdown",
    )


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    uid = update.effective_user.id
    with db() as c:
        active = c.execute("SELECT * FROM active_sessions WHERE user_id=?", (uid,)).fetchone()
        if not active:
            await update.message.reply_text("Koi active timer nahi hai. /study se shuru karo.")
            return
        started = parse_dt(active["started_at"])
        ended = now()
        raw_minutes = int((ended - started).total_seconds() // 60)
        if raw_minutes < 1:
            await update.message.reply_text("Session 1 minute se kam tha, isliye save nahi kiya. Padhai continue karni ho to /study use karo.")
            c.execute("DELETE FROM active_sessions WHERE user_id=?", (uid,))
            c.commit()
            return
        minutes = min(raw_minutes, MAX_SESSION_MINUTES)
        c.execute("""
            INSERT INTO sessions(user_id, subject, started_at, ended_at, minutes)
            VALUES (?, ?, ?, ?, ?)
        """, (uid, active["subject"], started.isoformat(), ended.isoformat(), minutes))
        c.execute("DELETE FROM active_sessions WHERE user_id=?", (uid,))
        c.commit()
    note = "\n⚠️ Session 6-hour cap par save hua. Long break ya bhoola hua timer ho to time manually verify karo." if raw_minutes > MAX_SESSION_MINUTES else ""
    await update.message.reply_text(
        f"✅ *Session saved!*\nSubject: {active['subject']}\nTime: {fmt_minutes(minutes)}\nToday total: {fmt_minutes(total_minutes(uid, now().replace(hour=0, minute=0, second=0, microsecond=0)))}{note}",
        parse_mode="Markdown",
    )


async def cancel_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as c:
        cur = c.execute("DELETE FROM active_sessions WHERE user_id=?", (uid,))
        c.commit()
    await update.message.reply_text("Active timer cancel kar diya; study time record nahi hua." if cur.rowcount else "Koi active timer nahi hai.")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE, period):
    register_user(update.effective_user)
    uid = update.effective_user.id
    n = now()
    if period == "today":
        start = n.replace(hour=0, minute=0, second=0, microsecond=0)
        title = "Aaj ki report"
        end = start + timedelta(days=1)
    elif period == "week":
        start = period_start("week")
        title = "Pichhle 7 din ki report"
        end = n + timedelta(seconds=1)
    else:
        start = period_start("month")
        title = f"{n.strftime('%B')} ki report"
        end = n + timedelta(seconds=1)
    total = total_minutes(uid, start, end)
    with db() as c:
        rows = c.execute("""
            SELECT subject, COALESCE(SUM(minutes), 0) AS mins
            FROM sessions WHERE user_id=? AND ended_at >= ? AND ended_at < ?
            GROUP BY subject ORDER BY mins DESC
        """, (uid, start.isoformat(), end.isoformat())).fetchall()
        user = c.execute("SELECT target_minutes FROM users WHERE user_id=?", (uid,)).fetchone()
    target = user["target_minutes"] if user else 360
    pct = min(100, int(total * 100 / target)) if target else 0
    lines = [f"📊 *{title}*", "", f"⏱️ Total study: *{fmt_minutes(total)}*"]
    if period == "today":
        lines.append(f"🎯 Target: {fmt_minutes(target)} ({pct}% complete)")
    lines.append(f"🔥 Current streak: {streak_for(uid)} day(s)")
    lines.append("")
    lines.append("*Subject breakdown:*")
    by_subject = {r["subject"]: int(r["mins"]) for r in rows}
    for s in SUBJECTS:
        lines.append(f"• {s}: {fmt_minutes(by_subject.get(s, 0))}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def today_cmd(update, context):
    await report(update, context, "today")


async def week_cmd(update, context):
    await report(update, context, "week")


async def month_cmd(update, context):
    await report(update, context, "month")


async def target_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if not context.args:
        await update.message.reply_text("Use: /target 6  (daily target 6 hours)")
        return
    try:
        hours = float(context.args[0])
        if not 0.5 <= hours <= 18:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Target 0.5 se 18 hours ke beech rakho. Example: /target 6")
        return
    minutes = int(hours * 60)
    with db() as c:
        c.execute("UPDATE users SET target_minutes=? WHERE user_id=?", (minutes, update.effective_user.id))
        c.commit()
    await update.message.reply_text(f"🎯 Daily target set: {fmt_minutes(minutes)}")


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE, period):
    register_user(update.effective_user)
    n = now()
    if period == "week":
        start = period_start("week")
        title = "🏆 Weekly leaderboard (last 7 days)"
    else:
        start = period_start("month")
        title = f"🏆 Monthly leaderboard ({n.strftime('%B')})"
    with db() as c:
        users = c.execute("SELECT user_id, username, display_name FROM users").fetchall()
        rows = c.execute("""
            SELECT user_id, SUM(minutes) AS mins
            FROM sessions WHERE ended_at >= ? AND ended_at <= ?
            GROUP BY user_id
        """, (start.isoformat(), n.isoformat())).fetchall()
    minutes_by_user = {r["user_id"]: int(r["mins"]) for r in rows}
    entries = []
    for u in users:
        mins = minutes_by_user.get(u["user_id"], 0)
        streak = streak_for(u["user_id"])
        # Hours are primary; current streak breaks ties.
        entries.append((mins, streak, user_name(u), u["user_id"]))
    entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if not entries:
        await update.message.reply_text("Abhi koi registered user nahi hai.")
        return
    lines = [f"*{title}*", "_Hours first; current streak breaks ties._", ""]
    for i, (mins, streak, name, uid) in enumerate(entries[:10], 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} {name} — *{fmt_minutes(mins)}* · 🔥 {streak}d")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def rank_cmd(update, context):
    await leaderboard(update, context, "week")


async def rankmonth_cmd(update, context):
    await leaderboard(update, context, "month")


async def myrank_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    uid = update.effective_user.id
    n = now()
    start = period_start("week")
    with db() as c:
        users = c.execute("SELECT user_id, username, display_name FROM users").fetchall()
        rows = c.execute("SELECT user_id, SUM(minutes) AS mins FROM sessions WHERE ended_at >= ? AND ended_at <= ? GROUP BY user_id",
                         (start.isoformat(), n.isoformat())).fetchall()
    mins_map = {r["user_id"]: int(r["mins"]) for r in rows}
    entries = [(mins_map.get(u["user_id"], 0), streak_for(u["user_id"]), u["user_id"]) for u in users]
    entries.sort(key=lambda x: (x[0], x[1]), reverse=True)
    rank = next((i for i, item in enumerate(entries, 1) if item[2] == uid), None)
    own_mins = mins_map.get(uid, 0)
    await update.message.reply_text(
        f"📍 *Tumhari weekly rank:* #{rank or '—'} of {len(entries)}\n"
        f"⏱️ Last 7 days: {fmt_minutes(own_mins)}\n"
        f"🔥 Current streak: {streak_for(uid)} day(s)\n\n"
        "Ranking mein study hours primary hain; equal hours par streak tie-breaker hai.",
        parse_mode="Markdown",
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    with db() as c:
        rows = c.execute("""
            SELECT subject, started_at, ended_at, minutes FROM sessions
            WHERE user_id=? ORDER BY ended_at DESC LIMIT 5
        """, (update.effective_user.id,)).fetchall()
    if not rows:
        await update.message.reply_text("Abhi study history nahi hai. /study se pehla session shuru karo.")
        return
    lines = ["🗂️ *Tumhare latest 5 sessions*"]
    for r in rows:
        ended = parse_dt(r["ended_at"])
        lines.append(f"• {ended.strftime('%d %b, %I:%M %p')} — {r['subject']}: {fmt_minutes(r['minutes'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if not context.args:
        await update.message.reply_text("Use /reminder HH:MM, e.g. /reminder 19:00. Disable with /reminder off.")
        return
    value = context.args[0].lower()
    if value == "off":
        with db() as c:
            c.execute("UPDATE users SET reminder_time=NULL WHERE user_id=?", (update.effective_user.id,))
            c.commit()
        await update.message.reply_text("Daily reminder off kar diya.")
        return
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        await update.message.reply_text("Time 24-hour format mein do, e.g. /reminder 19:00")
        return
    with db() as c:
        c.execute("UPDATE users SET reminder_time=? WHERE user_id=?", (value, update.effective_user.id))
        c.commit()
    schedule_reminder(context.application, update.effective_user.id, value)
    await update.message.reply_text(f"🔔 Daily reminder set: {value} (India time). Off karne ke liye /reminder off")


def schedule_reminder(app, user_id, hhmm):
    job_name = f"reminder_{user_id}"
    for job in app.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
    if not hhmm:
        return
    hour, minute = map(int, hhmm.split(":"))
    app.job_queue.run_daily(
        reminder_job, time=datetime.now(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0).timetz(),
        data={"user_id": user_id}, name=job_name, chat_id=user_id,
    )


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.data["user_id"]
    await context.bot.send_message(
        chat_id=uid,
        text="📚 Study time! Aaj ka target complete karne ke liye /study se session shuru karo.\nReport: /today",
    )


async def post_init(app: Application):
    # Restore saved reminders after a process restart.
    with db() as c:
        rows = c.execute("SELECT user_id, reminder_time FROM users WHERE reminder_time IS NOT NULL").fetchall()
    for row in rows:
        schedule_reminder(app, row["user_id"], row["reminder_time"])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled bot error", exc_info=context.error)


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing. Do not hardcode your token.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("study", choose_subject))
    app.add_handler(CallbackQueryHandler(subject_callback, pattern=r"^subject:"))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("cancel", cancel_timer))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("target", target_cmd))
    app.add_handler(CommandHandler("rank", rank_cmd))
    app.add_handler(CommandHandler("rankmonth", rankmonth_cmd))
    app.add_handler(CommandHandler("myrank", myrank_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("reminder", set_reminder))
    app.add_error_handler(error_handler)
    log.info("Study tracker bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
