"""
Local test harness for the evening practice flow (stage 1: session + buttons, no voice yet).

Real db.py / bot.py functions against a throwaway SQLite DB, Telegram calls mocked.

Usage:
    python tests/simulate_practice.py
"""
import os
import sys
from datetime import date, datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

USER_ID = 424242
OTHER_ID = 777

# Must be set before `import bot` / `import db` — both read env at import time
# (load_dotenv() in bot.py doesn't override values already set here).
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["OWNER_TELEGRAM_ID"] = str(USER_ID)
os.environ["OPENAI_API_KEY"] = "test"
DB_FILE = os.path.join(ROOT, "tests", "_scratch_practice.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
os.environ["DB_PATH"] = DB_FILE

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import db
import bot

failures = []


def check(name, condition):
    print(("  ✅ " if condition else "  ❌ ") + name)
    if not condition:
        failures.append(name)


def seed():
    db.init_db()
    conn = db.get_connection()
    today = date.today().isoformat()
    for phrase, meaning, shown in [
        ("la cuenta", "счёт", today),
        ("cansado", "уставший", today),
        ("la playa", "пляж", today),
        ("el tren", "поезд", None),
    ]:
        conn.execute(
            """INSERT INTO words (user_id, phrase, meaning, status, interval_stage, speech_activation_last_shown)
               VALUES (?, ?, ?, 'familiar', 3, ?)""",
            (USER_ID, phrase, meaning, shown),
        )
    conn.commit()
    conn.close()


def fake_bot():
    return SimpleNamespace(send_message=AsyncMock())


def sent_texts(b):
    return [c.args[1] for c in b.send_message.call_args_list]


def fake_query(b, user_id=USER_ID):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(chat_id=user_id),
        edit_message_reply_markup=AsyncMock(),
        get_bot=lambda: b,
    )


async def main():
    seed()

    print("\n1. Хук в hourly-джобе: срабатывает только в 21:00 по времени владельца, один раз за дату")
    utc_hour = datetime.now(timezone.utc).hour
    db.set_user_utc_offset(USER_ID, (bot.EVENING_PRACTICE_LOCAL_HOUR + 1 - utc_hour) % 24)  # сейчас 22:00
    b = fake_bot()
    await bot._maybe_start_evening_practice(SimpleNamespace(bot=b))
    check("не в 21:00 — ничего не отправлено", b.send_message.call_count == 0)

    db.set_user_utc_offset(USER_ID, (bot.EVENING_PRACTICE_LOCAL_HOUR - utc_hour) % 24)  # сейчас 21:00
    await bot._maybe_start_evening_practice(SimpleNamespace(bot=b))
    check("в 21:00 — отправлено первое слово", b.send_message.call_count == 1)
    first = sent_texts(b)[0]
    print("     " + first.replace("\n", "\n     "))
    check("шапка «Пора поговорить»", "Пора поговорить" in first)
    check("русское значение показано", "счёт" in first)
    check("испанское — под спойлером", "<tg-spoiler>la cuenta</tg-spoiler>" in first)
    check("parse_mode=HTML", b.send_message.call_args.kwargs.get("parse_mode") == "HTML")
    check("взяты утренние слова, не el tren",
          db.get_active_practice_session(USER_ID)["word_ids"] == [1, 2, 3])

    await bot._maybe_start_evening_practice(SimpleNamespace(bot=b))
    check("повторный запуск в тот же час (рестарт) — дубля нет", b.send_message.call_count == 1)

    session = db.get_active_practice_session(USER_ID)
    sid = session["id"]

    print("\n2. Кнопка «Дальше»")
    q = fake_query(b)
    await bot._handle_practice_button(q, "practice_next", ["practice_next", str(sid), "0"])
    check("пришло слово 2", "Слово 2 из 3" in sent_texts(b)[-1] and "уставший" in sent_texts(b)[-1])
    check("у второго слова нет шапки", "Пора поговорить" not in sent_texts(b)[-1])
    check("кнопки со старого сообщения убраны", q.edit_message_reply_markup.await_count == 1)

    count = b.send_message.call_count
    await bot._handle_practice_button(fake_query(b), "practice_next", ["practice_next", str(sid), "0"])
    check("двойное нажатие на старое сообщение не пропускает слово",
          b.send_message.call_count == count and db.get_practice_session(sid)["current_index"] == 1)

    await bot._handle_practice_button(fake_query(b, OTHER_ID), "practice_next", ["practice_next", str(sid), "1"])
    check("чужой пользователь не может листать сессию",
          b.send_message.call_count == count and db.get_practice_session(sid)["current_index"] == 1)

    await bot._handle_practice_button(fake_query(b), "practice_next", ["practice_next", str(sid), "1"])
    await bot._handle_practice_button(fake_query(b), "practice_next", ["practice_next", str(sid), "2"])
    s = db.get_practice_session(sid)
    check("после последнего слова — «закончена»", "закончена" in sent_texts(b)[-1])
    check("сессия done, 3 исхода skipped", s["status"] == "done" and s["outcomes"] == ["skipped"] * 3)

    print("\n3. /practice вручную + кнопка «Закончить»")
    b2 = fake_bot()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=USER_ID),
                             message=SimpleNamespace(reply_text=AsyncMock()))
    await bot.practice(update, SimpleNamespace(bot=b2))
    s2 = db.get_active_practice_session(USER_ID)
    check("/practice создал новую активную сессию", s2 is not None and s2["id"] != sid)
    await bot._handle_practice_button(fake_query(b2), "practice_stop", ["practice_stop", str(s2["id"]), "0"])
    check("«Закончить» — сессия done", db.get_practice_session(s2["id"])["status"] == "done")
    check("«Закончить» — сообщение об окончании", "закончена" in sent_texts(b2)[-1])

    other_update = SimpleNamespace(effective_user=SimpleNamespace(id=OTHER_ID),
                                   message=SimpleNamespace(reply_text=AsyncMock()))
    b3 = fake_bot()
    await bot.practice(other_update, SimpleNamespace(bot=b3))
    check("/practice от не-владельца — тишина",
          b3.send_message.call_count == 0 and other_update.message.reply_text.await_count == 0)

    print("\n4. Удалённое слово внутри сессии пропускается")
    b4 = fake_bot()
    s4 = db.create_practice_session(USER_ID, "2099-01-01", [1, 999, 3])
    await bot._send_practice_word(b4, USER_ID, s4)
    await bot._handle_practice_button(fake_query(b4), "practice_next", ["practice_next", str(s4["id"]), "0"])
    check("после слова 1 сразу слово 3", "Слово 3 из 3" in sent_texts(b4)[-1])
    check("новая сессия сделала предыдущие активные expired",
          all(db.get_practice_session(i)["status"] != "active" for i in (sid, s2["id"])))

    print()
    if failures:
        print(f"❌ Провалено: {len(failures)}")
        sys.exit(1)
    print("✅ Все проверки пройдены")


if __name__ == "__main__":
    asyncio.run(main())
    os.remove(DB_FILE)
