# One-off maintenance script.
# Run once against the production DB: railway run python reset_collected_review_dates.py
# Sets next_review_date = today for every word still in status='collected',
# so they become due at the next /review instead of whatever date was
# computed when they were added.

from datetime import date
import db

conn = db.get_connection()
today = date.today().isoformat()
cur = conn.execute(
    "UPDATE words SET next_review_date = ? WHERE status = 'collected' AND pool = 'scheduled'",
    (today,),
)
conn.commit()
print(f"Updated {cur.rowcount} word(s) to next_review_date={today}")
conn.close()
