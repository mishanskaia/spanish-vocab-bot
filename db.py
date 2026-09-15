import os
import sqlite3
import json
import math
import secrets
from datetime import date, timedelta, datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "spanish_vocab_bot.db")

INTERVALS = [1, 3, 7, 14, 30, 90]
MOSCOW_TZ = timezone(timedelta(hours=3))


def _stage_to_status(stage: int, times_reviewed: int = 0) -> str:
    if stage == 0:
        return "collected" if times_reviewed == 0 else "learning"
    if stage <= 2:
        return "learning"
    if stage <= 4:
        return "familiar"
    if stage <= 5:
        return "active"
    return "mastered"


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_add_column(conn, column_def: str):
    try:
        conn.execute(f"ALTER TABLE words ADD COLUMN {column_def}")
    except sqlite3.OperationalError:
        pass


def get_moscow_now() -> datetime:
    return datetime.now(MOSCOW_TZ)


def get_current_window(utc_offset_hours: int = 3) -> str:
    """'morning' (6-14) / 'evening' (14-23) / 'night' in the given UTC offset
    — defaults to Moscow (3) for callers that don't have a specific user's
    offset on hand. Callers that add a word for a specific user should pass
    that user's own db.get_user_utc_offset() instead, so "first review
    today/tomorrow" matches their own morning/evening, not Moscow's."""
    hour = (datetime.now(timezone.utc).hour + utc_offset_hours) % 24
    if 6 <= hour < 14:
        return 'morning'
    elif 14 <= hour < 23:
        return 'evening'
    return 'night'


def _local_today(utc_offset_hours: int = 3) -> str:
    """Calendar date in the given UTC offset — for anything that should reset
    at *that user's* midnight (added_date). Same default-to-Moscow convention
    as get_current_window."""
    return (datetime.now(timezone.utc) + timedelta(hours=utc_offset_hours)).date().isoformat()


def _local_week_start(utc_offset_hours: int = 3) -> str:
    """Monday's date (ISO) of the calendar week containing the user's current
    local date — for the weekly new-word limit, which resets every Monday in
    *that user's* own local time, not the server's UTC week. Same
    default-to-Moscow convention as get_current_window/_local_today."""
    today = (datetime.now(timezone.utc) + timedelta(hours=utc_offset_hours)).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


def first_review_for_window(window: str) -> tuple:
    """Returns (next_review_date_iso, stored_added_window)"""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    if window == 'morning':
        return today.isoformat(), 'morning'
    else:
        return tomorrow.isoformat(), 'evening'


# ---------------------------------------------------------------------------
# Per-user UTC offset — asked once at /start (see bot.py) so the three daily
# vocab reminders land at 10/14/18 in *that user's* local time instead of
# always Moscow. Whole-hour precision only (no DST, no half-hour zones) —
# plenty for "don't wake someone at 3am", not meant to be exact. A user who
# hasn't answered yet has no row here; callers default that to 3 (Moscow),
# matching pre-timezone behavior so nothing changes for them until they set it.
# ---------------------------------------------------------------------------

def get_user_utc_offset(user_id: int):
    conn = get_connection()
    row = conn.execute(
        "SELECT utc_offset_hours FROM user_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["utc_offset_hours"] if row else None


def set_user_utc_offset(user_id: int, offset_hours: int):
    conn = get_connection()
    conn.execute(
        """INSERT INTO user_settings (user_id, utc_offset_hours) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET utc_offset_hours = excluded.utc_offset_hours""",
        (user_id, offset_hours),
    )
    conn.commit()
    conn.close()


def init_db():
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            phrase TEXT NOT NULL,
            meaning TEXT,
            part_of_speech TEXT,
            cefr_level TEXT,
            examples TEXT,
            conjugation TEXT,
            added_date TEXT,
            interval_stage INTEGER DEFAULT 0,
            next_review_date TEXT,
            correct_streak INTEGER DEFAULT 0,
            status TEXT DEFAULT 'collected',
            times_reviewed INTEGER DEFAULT 0,
            success_rate REAL DEFAULT 0.0,
            last_reviewed TEXT,
            pool TEXT DEFAULT 'scheduled',
            added_window TEXT DEFAULT 'morning'
        )
        """
    )
    _safe_add_column(conn, "pool TEXT DEFAULT 'scheduled'")
    _safe_add_column(conn, "added_window TEXT DEFAULT 'morning'")
    _safe_add_column(conn, "mnemonic TEXT")
    _safe_add_column(conn, "mnemonic_retries INTEGER DEFAULT 0")
    _safe_add_column(conn, "collocations TEXT")
    _safe_add_column(conn, "gerund TEXT")
    conn.execute(
        "UPDATE words SET status = 'learning' WHERE status = 'collected' AND times_reviewed > 0"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            stage_before INTEGER NOT NULL,
            reviewed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_log_word_id ON review_log(word_id)"
    )
    _safe_add_column(conn, "speech_activation_last_shown TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            utc_offset_hours INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS study_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            added_date TEXT NOT NULL,
            x2_date TEXT NOT NULL,
            x7_date TEXT NOT NULL,
            x10_date TEXT NOT NULL,
            x14_date TEXT NOT NULL,
            x30_date TEXT NOT NULL,
            x2_sent INTEGER DEFAULT 0,
            x7_sent INTEGER DEFAULT 0,
            x10_sent INTEGER DEFAULT 0,
            x14_sent INTEGER DEFAULT 0,
            x30_sent INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS invites (
            code TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_by INTEGER,
            used_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def find_word_by_phrase(user_id: int, phrase: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM words WHERE user_id = ? AND phrase = ? COLLATE NOCASE",
        (user_id, phrase),
    ).fetchone()
    conn.close()
    return row


def _coerce_text(value):
    """Claude doesn't always follow the requested type for free-form fields
    (e.g. conjugation as a dict instead of a string) — normalize to a plain
    string (or None) so it can be bound as a SQLite parameter."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def add_word(user_id, phrase, meaning, part_of_speech, cefr_level, examples,
             conjugation=None, collocations=None, gerund=None):
    """Returns (word_id, is_new). If the phrase already exists for this
    user (case-insensitive), returns the existing row instead of inserting
    a duplicate."""
    existing = find_word_by_phrase(user_id, phrase)
    if existing:
        return existing["id"], False

    conjugation = _coerce_text(conjugation)
    gerund = _coerce_text(gerund)

    offset = get_user_utc_offset(user_id)
    if offset is None:
        offset = 3  # Moscow default, matches get_current_window()'s own default

    conn = get_connection()
    today = _local_today(offset)
    window = get_current_window(offset)
    first_review, added_window = first_review_for_window(window)
    cur = conn.execute(
        """INSERT INTO words
           (user_id, phrase, meaning, part_of_speech, cefr_level, examples,
            conjugation, collocations, gerund, added_date, interval_stage, next_review_date,
            correct_streak, times_reviewed, success_rate, status, pool, added_window)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0, 0.0, 'collected', 'scheduled', ?)""",
        (user_id, phrase, meaning, part_of_speech, cefr_level,
         json.dumps(examples), conjugation, json.dumps(collocations or []), gerund,
         today, first_review, added_window),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id, True


def delete_word(user_id: int, phrase: str) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM words WHERE user_id = ? AND phrase = ? COLLATE NOCASE",
        (user_id, phrase),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def delete_word_by_id(word_id: int, user_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "DELETE FROM words WHERE id = ? AND user_id = ?",
        (word_id, user_id),
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_user_words(user_id: int):
    """Most-recently-added first — callers slicing this for a "last N" list
    (e.g. the /delete button menu) rely on that order."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, phrase FROM words WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_review_history_words(user_id: int):
    """Every word that has been reviewed at least once, for diagnosing
    scheduling issues (status/pool/next_review_date at a glance)."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT phrase, status, pool, next_review_date, times_reviewed, interval_stage
           FROM words WHERE user_id = ? AND times_reviewed > 0
           ORDER BY next_review_date ASC""",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def reset_collected_review_dates(user_id: int, batch_size: int = 15):
    """Spreads this user's collected words across next_review_date in
    batches of batch_size (oldest first). Returns [(date_iso, count), ...]."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT id FROM words WHERE user_id = ? AND status = 'collected' AND pool = 'scheduled'
           ORDER BY added_date, id""",
        (user_id,),
    ).fetchall()
    today = date.today()
    batches = []
    for i, r in enumerate(rows):
        batch_index = i // batch_size
        next_review = (today + timedelta(days=batch_index)).isoformat()
        conn.execute("UPDATE words SET next_review_date = ? WHERE id = ?", (next_review, r["id"]))
        if batch_index == len(batches):
            batches.append([next_review, 0])
        batches[batch_index][1] += 1
    conn.commit()
    conn.close()
    return [(d, c) for d, c in batches]


def detect_and_mark_overdue(user_id: int):
    """Words due before today get marked overdue and staged back one step."""
    conn = get_connection()
    today = date.today().isoformat()
    rows = conn.execute(
        """SELECT id, interval_stage FROM words
           WHERE user_id = ? AND pool = 'scheduled'
           AND next_review_date < ?
           AND status NOT IN ('mastered', 'skipped')""",
        (user_id, today),
    ).fetchall()
    for r in rows:
        new_stage = max(0, r["interval_stage"] - 1)
        conn.execute(
            "UPDATE words SET pool = 'overdue', interval_stage = ? WHERE id = ?",
            (new_stage, r["id"]),
        )
    conn.commit()
    conn.close()


def get_due_words_split(user_id: int):
    """Returns (overdue_list, scheduled_list)"""
    conn = get_connection()
    today = date.today().isoformat()
    overdue = conn.execute(
        """SELECT * FROM words WHERE user_id = ? AND pool = 'overdue'
           AND status NOT IN ('mastered', 'skipped')
           ORDER BY next_review_date ASC, RANDOM()""",
        (user_id,),
    ).fetchall()
    scheduled = conn.execute(
        """SELECT * FROM words WHERE user_id = ? AND pool = 'scheduled'
           AND status NOT IN ('mastered', 'skipped')
           AND next_review_date <= ?
           ORDER BY next_review_date ASC, RANDOM()""",
        (user_id, today),
    ).fetchall()
    conn.close()
    return list(overdue), list(scheduled)


def get_due_words(user_id):
    overdue, scheduled = get_due_words_split(user_id)
    return overdue + scheduled


def count_words_added_this_week(user_id: int) -> int:
    """Counts rows inserted since Monday of this user's current local week —
    tracks token spend (a Claude call already happened by the time a lookup
    turns out to be a duplicate), not just successfully-kept vocabulary.

    Resets every Monday in the user's own local time (their stored UTC
    offset, or Moscow default), not the server's UTC week — see
    _local_week_start(). added_date is an ISO date string, so a plain
    lexicographic >= comparison against the Monday date works."""
    offset = get_user_utc_offset(user_id)
    if offset is None:
        offset = 3
    conn = get_connection()
    week_start = _local_week_start(offset)
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM words WHERE user_id = ? AND added_date >= ?",
        (user_id, week_start),
    ).fetchone()
    conn.close()
    return row["c"]


def get_all_due_users():
    conn = get_connection()
    today = date.today().isoformat()
    rows = conn.execute(
        """SELECT DISTINCT user_id FROM words
           WHERE status NOT IN ('mastered', 'skipped')
           AND (pool = 'overdue' OR (pool = 'scheduled' AND next_review_date <= ?))""",
        (today,),
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def count_due_not_reviewed_today(user_id: int) -> int:
    conn = get_connection()
    today = date.today().isoformat()
    row = conn.execute(
        """SELECT COUNT(*) AS c FROM words
           WHERE user_id = ? AND status NOT IN ('mastered', 'skipped')
           AND (pool = 'overdue' OR (pool = 'scheduled' AND next_review_date <= ?))
           AND (last_reviewed IS NULL OR last_reviewed != ?)""",
        (user_id, today, today),
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def get_words_per_user() -> list[dict]:
    """Total word count per user_id (excludes legacy 'skipped' rows), most words first."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT user_id, COUNT(*) AS total FROM words WHERE status != 'skipped' "
        "GROUP BY user_id ORDER BY total DESC"
    ).fetchall()
    conn.close()
    return [{"user_id": r["user_id"], "total": r["total"]} for r in rows]


def save_mnemonic(word_id: int, mnemonic: str):
    conn = get_connection()
    conn.execute("UPDATE words SET mnemonic = ? WHERE id = ?", (mnemonic, word_id))
    conn.commit()
    conn.close()


def get_mnemonic_retries(word_id: int) -> int:
    conn = get_connection()
    row = conn.execute("SELECT mnemonic_retries FROM words WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    return row["mnemonic_retries"] if row and row["mnemonic_retries"] is not None else 0


def increment_mnemonic_retries(word_id: int) -> int:
    conn = get_connection()
    conn.execute(
        "UPDATE words SET mnemonic_retries = COALESCE(mnemonic_retries, 0) + 1 WHERE id = ?",
        (word_id,),
    )
    conn.commit()
    row = conn.execute("SELECT mnemonic_retries FROM words WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    return row["mnemonic_retries"]


def get_word_by_id(word_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    return row


def mark_review_result(word_id: int, grade: str):
    """
    grade: 'remember' | 'almost' | 'hard'
    remember: advance stage, normal interval
    almost:  stay stage, interval × 0.8 (min 1 day) — recalled with hesitation,
             not clean enough to advance, but stronger than a miss
    hard:    stay stage, interval × 0.6 (min 1 day)
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    if row is None:
        conn.close()
        return

    stage = row["interval_stage"]
    streak = row["correct_streak"]
    times = (row["times_reviewed"] or 0) + 1
    old_rate = row["success_rate"] or 0.0
    credit = {"remember": 1.0, "almost": 0.5}.get(grade, 0.0)
    new_rate = ((old_rate * (times - 1)) + credit) / times
    today = date.today().isoformat()

    base_interval = INTERVALS[min(stage, len(INTERVALS) - 1)]

    if grade == 'remember':
        days = base_interval
        new_stage = stage + 1
        streak += 1
    elif grade == 'almost':
        days = max(1, round(base_interval * 0.8))
        new_stage = stage
        streak = 0
    else:  # hard
        days = max(1, math.floor(base_interval * 0.6))
        new_stage = stage
        streak = 0

    next_review = (date.today() + timedelta(days=days)).isoformat()

    conn.execute(
        """INSERT INTO review_log (word_id, user_id, grade, stage_before, reviewed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (word_id, row["user_id"], grade, stage, get_moscow_now().isoformat()),
    )

    if new_stage >= len(INTERVALS):
        conn.execute(
            """UPDATE words SET status = 'mastered', interval_stage = ?,
               correct_streak = ?, times_reviewed = ?, success_rate = ?,
               last_reviewed = ?, pool = 'scheduled', next_review_date = ?
               WHERE id = ?""",
            (new_stage, streak, times, new_rate, today, next_review, word_id),
        )
        conn.commit()
        conn.close()
        return

    new_status = _stage_to_status(new_stage, times)
    conn.execute(
        """UPDATE words SET interval_stage = ?, next_review_date = ?,
           correct_streak = ?, status = ?, times_reviewed = ?,
           success_rate = ?, last_reviewed = ?, pool = 'scheduled'
           WHERE id = ?""",
        (new_stage, next_review, streak, new_status, times, new_rate, today, word_id),
    )
    conn.commit()
    conn.close()


def get_all_words_for_review(user_id):
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM words
           WHERE user_id = ? AND status NOT IN ('mastered', 'skipped')
           ORDER BY RANDOM()""",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_words_for_export(user_id: int):
    """All of a user's words (skipped included), for the read-only API."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM words WHERE user_id = ? ORDER BY added_date DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def get_stats(user_id) -> dict:
    conn = get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM words WHERE user_id = ? GROUP BY status",
        (user_id,),
    ).fetchall()
    cefr_rows = conn.execute(
        "SELECT cefr_level, COUNT(*) AS c FROM words WHERE user_id = ? AND status != 'skipped' GROUP BY cefr_level",
        (user_id,),
    ).fetchall()
    conn.close()

    result = {"collected": 0, "learning": 0, "familiar": 0, "active": 0, "mastered": 0}
    for r in rows:
        if r["status"] in result:
            result[r["status"]] = r["c"]
    result["total"] = sum(result.values())

    cefr = {}
    for r in cefr_rows:
        if r["cefr_level"]:
            cefr[r["cefr_level"]] = r["c"]
    result["cefr"] = cefr
    return result


# ---------------------------------------------------------------------------
# Study Coach — admin-only feature, gated by OWNER_TELEGRAM_ID in bot.py.
# Grammar topics cycle through 5 fixed checkpoints from the input date X:
# X+2 and X+7 (drills), X+14 and X+30 (spaced repeats of the same drill idea),
# plus X+10 for a separate "use it in speech" nudge. All five dates are fixed
# at insert time and never recalculated — a checkpoint that can't be shown
# today (anchor day, or another topic's checkpoint already took the slot)
# just stays due (date <= today) until a day where it can be shown.
# ---------------------------------------------------------------------------

GRAMMAR_STAGE_ORDER = ["x2", "x7", "x14", "x30"]


def add_study_topic(user_id: int, topic: str) -> int:
    today = date.today()
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO study_topics
           (user_id, topic, added_date, x2_date, x7_date, x10_date, x14_date, x30_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id, topic, today.isoformat(),
            (today + timedelta(days=2)).isoformat(),
            (today + timedelta(days=7)).isoformat(),
            (today + timedelta(days=10)).isoformat(),
            (today + timedelta(days=14)).isoformat(),
            (today + timedelta(days=30)).isoformat(),
        ),
    )
    conn.commit()
    topic_id = cur.lastrowid
    conn.close()
    return topic_id


def get_study_topic(topic_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM study_topics WHERE id = ?", (topic_id,)).fetchone()
    conn.close()
    return row


def get_active_topics(user_id: int):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM study_topics WHERE user_id = ? AND status = 'active' ORDER BY added_date, id",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def cancel_topic(user_id: int, topic_id: int) -> bool:
    conn = get_connection()
    cur = conn.execute(
        "UPDATE study_topics SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'",
        (topic_id, user_id),
    )
    conn.commit()
    cancelled = cur.rowcount > 0
    conn.close()
    return cancelled


def get_due_grammar_item(user_id: int):
    """The oldest active topic (by added_date) that has an un-sent grammar
    checkpoint whose date has arrived. Stages are checked in chronological
    order per topic, so a badly delayed topic still surfaces its earliest
    pending stage first rather than jumping ahead."""
    today_iso = date.today().isoformat()
    for row in get_active_topics(user_id):
        for stage in GRAMMAR_STAGE_ORDER:
            if row[f"{stage}_sent"] == 0 and row[f"{stage}_date"] <= today_iso:
                return {"topic_id": row["id"], "topic": row["topic"], "stage": stage}
    return None


def mark_grammar_stage_sent(topic_id: int, stage: str):
    conn = get_connection()
    conn.execute(f"UPDATE study_topics SET {stage}_sent = 1 WHERE id = ?", (topic_id,))
    if stage == "x30":
        conn.execute("UPDATE study_topics SET status = 'done' WHERE id = ?", (topic_id,))
    conn.commit()
    conn.close()


def get_due_speaking_item(user_id: int):
    today_iso = date.today().isoformat()
    for row in get_active_topics(user_id):
        if row["x10_sent"] == 0 and row["x10_date"] <= today_iso:
            return {"topic_id": row["id"], "topic": row["topic"]}
    return None


def mark_speaking_sent(topic_id: int):
    conn = get_connection()
    conn.execute("UPDATE study_topics SET x10_sent = 1 WHERE id = ?", (topic_id,))
    conn.commit()
    conn.close()


def get_speech_activation_words(user_id: int, limit: int = 3):
    """Oldest-shown-first (NULL = never shown sorts first in SQLite), so the
    whole pool of familiar+ words rotates through before anything repeats —
    no separate 'used' flag or manual reset needed."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT * FROM words WHERE user_id = ? AND status IN ('familiar', 'active', 'mastered')
           ORDER BY speech_activation_last_shown ASC, RANDOM() LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def mark_speech_activation_shown(word_ids):
    if not word_ids:
        return
    conn = get_connection()
    today = date.today().isoformat()
    conn.executemany(
        "UPDATE words SET speech_activation_last_shown = ? WHERE id = ?",
        [(today, wid) for wid in word_ids],
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Backup — owner-only /backup and a weekly auto-backup job send this file to
# the owner as a Telegram document (see CLAUDE.md "Бэкапы"). Uses SQLite's
# own backup API rather than copying the raw file, so it produces a
# consistent snapshot even if a write is in flight (a plain file copy could
# grab a half-written page).
# ---------------------------------------------------------------------------

def create_backup(dest_path: str):
    src = get_connection()
    dest = sqlite3.connect(dest_path)
    with dest:
        src.backup(dest)
    dest.close()
    src.close()


# ---------------------------------------------------------------------------
# Invite-code gate — access control while the bot is shared with a small
# circle instead of the owner only. See CLAUDE.md "Доступ по инвайт-кодам"
# for the full rationale (why one-time codes, why a TTL, why no separate
# status-tracking command).
# ---------------------------------------------------------------------------

def create_invite(ttl_days: int) -> str:
    """Generates a one-time invite code, unused until someone redeems it via
    /start <code>. Unredeemed codes go stale after ttl_days — pure
    housekeeping, not a security boundary (codes are long enough that
    guessing one before it expires isn't a realistic risk)."""
    conn = get_connection()
    code = secrets.token_urlsafe(9)
    expires_at = (date.today() + timedelta(days=ttl_days)).isoformat()
    conn.execute(
        "INSERT INTO invites (code, created_at, expires_at) VALUES (?, ?, ?)",
        (code, date.today().isoformat(), expires_at),
    )
    conn.commit()
    conn.close()
    return code


def redeem_invite(code: str, user_id: int) -> bool:
    """Ties the code to user_id if it's unused and not expired. Whoever gets
    there first wins — a code shared with two people only ever authorizes
    the first to open the link."""
    conn = get_connection()
    today = date.today().isoformat()
    cur = conn.execute(
        """UPDATE invites SET used_by = ?, used_at = ?
           WHERE code = ? AND used_by IS NULL AND expires_at >= ?""",
        (user_id, get_moscow_now().isoformat(), code, today),
    )
    conn.commit()
    redeemed = cur.rowcount > 0
    conn.close()
    return redeemed


def is_user_authorized(user_id: int) -> bool:
    """A user is authorized once they've ever redeemed a code — this is
    permanent, not tied to the code's later expiry or reuse elsewhere."""
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM invites WHERE used_by = ? LIMIT 1", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None
