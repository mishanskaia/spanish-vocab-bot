"""
Local test harness for the /review and /all flow.

Runs the real handlers from bot.py against a throwaway SQLite DB, with every
Telegram network call mocked out (no bot token, no real chat, no cost).
Prints the full simulated conversation so you can eyeball card text, and
walks the whole due queue clicking through show -> grade, alternating
"Помню"/"Сложно" so both grading paths get exercised.

Usage:
    python tests/simulate_review.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Must be set before `import bot` / `import db` — both read env at import time.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
DB_FILE = os.path.join(ROOT, "tests", "_scratch_review.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
os.environ["DB_PATH"] = DB_FILE

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import db
import bot

USER_ID = 424242


def seed_words():
    db.init_db()
    words = [
        # blank should succeed (word matches example verbatim)
        dict(phrase="el pan", meaning="хлеб", part_of_speech="существительное", cefr_level="A1",
             examples=["Me gusta el pan. — Мне нравится хлеб."]),
        # verb: conjugated form in the example won't match the infinitive -> tier 2 (RU context)
        dict(phrase="hablar", meaning="говорить", part_of_speech="глагол", cefr_level="A1",
             examples=["Hablo español un poco. — Я говорю по-испански немного."],
             conjugation="yo hablo, tú hablas, él/ella habla, nosotros hablamos, vosotros habláis, ellos hablan",
             gerund="hablando"),
        # blank should succeed (adjective matches example verbatim)
        dict(phrase="feliz", meaning="счастливый", part_of_speech="прилагательное", cefr_level="A1",
             examples=["Estoy muy feliz hoy. — Я очень счастлив сегодня."]),
        # no examples at all -> tier 3 (bare meaning)
        dict(phrase="rara vez", meaning="редко", part_of_speech="наречие", cefr_level="A2",
             examples=[]),
    ]
    ids = []
    for w in words:
        word_id, _ = db.add_word(user_id=USER_ID, **w)
        ids.append(word_id)
    conn = db.get_connection()
    conn.execute("UPDATE words SET next_review_date = date('now') WHERE user_id = ?", (USER_ID,))
    conn.commit()
    conn.close()
    return ids


class Recorder:
    def __init__(self):
        self.last_keyboard = None
        self.messages = []


rec = Recorder()


async def fake_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    rec.messages.append(text)
    print(f"\n[БОТ] {text}")
    rec.last_keyboard = reply_markup
    if reply_markup:
        print("      кнопки:", [[b.text for b in row] for row in reply_markup.inline_keyboard])
    return SimpleNamespace(message_id=len(rec.messages))


def make_update(user_id, chat_id):
    async def reply_text(text, parse_mode=None, reply_markup=None):
        await fake_send_message(chat_id, text, reply_markup, parse_mode)
    message = SimpleNamespace(reply_text=reply_text)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
    )


def make_context():
    return SimpleNamespace(
        user_data={},
        bot=SimpleNamespace(send_message=fake_send_message),
    )


def make_callback_update(user_id, chat_id, data):
    async def edit_message_text(text, parse_mode=None, reply_markup=None):
        rec.messages.append(text)
        print(f"\n[БОТ правит сообщение] {text}")
        rec.last_keyboard = reply_markup
        if reply_markup:
            print("      кнопки:", [[b.text for b in row] for row in reply_markup.inline_keyboard])

    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat_id=chat_id),
        answer=AsyncMock(),
        edit_message_text=edit_message_text,
    )
    return SimpleNamespace(callback_query=query)


async def click_through_queue(ctx):
    """Clicks whatever button the bot last offered until it stops offering any
    (queue exhausted), cycling Помню/Почти помню/Сложно on grading steps."""
    round_no = 0
    guard = 0
    while rec.last_keyboard is not None and guard < 30:
        guard += 1
        buttons = [b for row in rec.last_keyboard.inline_keyboard for b in row]
        if len(buttons) == 3 and buttons[0].callback_data.startswith("grade:"):
            btn = buttons[round_no % 3]
            round_no += 1
        else:
            btn = buttons[0]
        rec.last_keyboard = None
        cb_update = make_callback_update(USER_ID, USER_ID, btn.callback_data)
        await bot.on_button(cb_update, ctx)


async def main():
    ids = seed_words()
    print(f"Засеяли слова, id={ids}\n")

    ctx = make_context()
    update = make_update(USER_ID, USER_ID)

    print("=" * 60)
    print("/review")
    print("=" * 60)
    await bot.review(update, ctx)
    await click_through_queue(ctx)

    print("\n" + "=" * 60)
    print("/all")
    print("=" * 60)
    ctx2 = make_context()
    await bot.review_all(update, ctx2)
    await click_through_queue(ctx2)

    print("\nГотово, без исключений — карточки выше можно просмотреть глазами.")


if __name__ == "__main__":
    asyncio.run(main())
