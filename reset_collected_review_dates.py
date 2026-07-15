# One-off maintenance script.
# Run once against the production DB: railway run python reset_collected_review_dates.py
#
# Spreads every word still stuck in status='collected' across next_review_date
# in batches of BATCH_SIZE per user (oldest word first), instead of dumping
# them all into a single review session.

from collections import defaultdict
from datetime import date, timedelta
import db

BATCH_SIZE = 15

conn = db.get_connection()
rows = conn.execute(
    """SELECT id, user_id FROM words
       WHERE status = 'collected' AND pool = 'scheduled'
       ORDER BY user_id, added_date, id"""
).fetchall()

by_user = defaultdict(list)
for r in rows:
    by_user[r["user_id"]].append(r["id"])

today = date.today()
total = 0
for user_id, word_ids in by_user.items():
    batches = (len(word_ids) + BATCH_SIZE - 1) // BATCH_SIZE
    for i, word_id in enumerate(word_ids):
        batch_index = i // BATCH_SIZE
        next_review = (today + timedelta(days=batch_index)).isoformat()
        conn.execute(
            "UPDATE words SET next_review_date = ? WHERE id = ?",
            (next_review, word_id),
        )
        total += 1
    print(f"user {user_id}: {len(word_ids)} word(s) spread over {batches} day(s) starting {today.isoformat()}")

conn.commit()
conn.close()
print(f"Updated {total} word(s) total")
