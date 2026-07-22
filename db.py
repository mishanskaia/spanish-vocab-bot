import os
import sqlite3
import json
import math
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


def get_current_window() -> str:
    hour = get_moscow_now().hour
    if 6 <= hour < 14:
        return 'morning'
    elif 14 <= hour < 23:
        return 'evening'
    return 'night'


def first_review_for_window(window: str) -> tuple:
    """Returns (next_review_date_iso, stored_added_window)"""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    if window == 'morning':
        return today.isoformat(), 'morning'
    else:
        return tomorrow.isoformat(), 'evening'


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
    conn.execute(
        "UPDATE words SET status = 'learning' WHERE status = 'collected' AND times_reviewed > 0"
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


def add_word(user_id, phrase, meaning, part_of_speech, cefr_level, examples,
             conjugation=None):
    """Returns (word_id, is_new). If the phrase already exists for this
    user (case-insensitive), returns the existing row instead of inserting
    a duplicate."""
    existing = find_word_by_phrase(user_id, phrase)
    if existing:
        return existing["id"], False

    conn = get_connection()
    today = date.today().isoformat()
    window = get_current_window()
    first_review, added_window = first_review_for_window(window)
    cur = conn.execute(
        """INSERT INTO words
           (user_id, phrase, meaning, part_of_speech, cefr_level, examples,
            conjugation, added_date, interval_stage, next_review_date,
            correct_streak, times_reviewed, success_rate, status, pool, added_window)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 0, 0.0, 'collected', 'scheduled', ?)""",
        (user_id, phrase, meaning, part_of_speech, cefr_level,
         json.dumps(examples), conjugation, today, first_review, added_window),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id, True


def add_skipped_word(user_id, phrase, meaning, part_of_speech, cefr_level, examples,
                     conjugation=None) -> int:
    conn = get_connection()
    today = date.today().isoformat()
    cur = conn.execute(
        """INSERT OR IGNORE INTO words
           (user_id, phrase, meaning, part_of_speech, cefr_level, examples,
            conjugation, added_date, interval_stage, next_review_date,
            correct_streak, times_reviewed, success_rate, status, pool, added_window)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 0, 0, 0.0, 'skipped', 'scheduled', 'morning')""",
        (user_id, phrase, meaning, part_of_speech, cefr_level,
         json.dumps(examples), conjugation, today),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


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
    conn = get_connection()
    rows = conn.execute(
        "SELECT phrase FROM words WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return [r["phrase"] for r in rows]


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


def get_word_by_id(word_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    return row


def mark_review_result(word_id: int, grade: str):
    """
    grade: 'easy' | 'remember' | 'hard'
    easy:    advance stage, interval × 1.3
    remember: advance stage, normal interval
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
    is_correct = grade in ('easy', 'remember')
    new_rate = ((old_rate * (times - 1)) + (1.0 if is_correct else 0.0)) / times
    today = date.today().isoformat()

    base_interval = INTERVALS[min(stage, len(INTERVALS) - 1)]

    if grade == 'easy':
        days = math.ceil(base_interval * 1.3)
        new_stage = stage + 1
        streak += 1
    elif grade == 'remember':
        days = base_interval
        new_stage = stage + 1
        streak += 1
    else:  # hard
        days = max(1, math.floor(base_interval * 0.6))
        new_stage = stage
        streak = 0

    next_review = (date.today() + timedelta(days=days)).isoformat()

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


def get_distractors(user_id: int, exclude_id: int, count: int = 2):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM words WHERE user_id = ? AND id != ? ORDER BY RANDOM() LIMIT ?",
        (user_id, exclude_id, count),
    ).fetchall()
    conn.close()
    return rows


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
