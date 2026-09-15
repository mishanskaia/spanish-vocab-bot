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


OK = {"unclear": False, "uses_target_word": True, "is_correct": True, "corrected": None,
      "explanation": "", "too_simple": False, "suggestion": None}


def verdict(**overrides):
    return {**OK, **overrides}


class Script:
    """Queued fake STT transcripts / Claude verdicts, patched over the real helpers."""

    def __init__(self):
        self.transcripts, self.verdicts = [], []
        self.claude_calls = 0

    def transcribe(self, audio_bytes, filename="voice.ogg"):
        item = self.transcripts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def check(self, phrase, meaning, transcript):
        self.claude_calls += 1
        return self.verdicts.pop(0)


def voice_update(user_id=USER_ID):
    tg_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"OggS")))
    message = SimpleNamespace(
        chat_id=user_id,
        voice=SimpleNamespace(get_file=AsyncMock(return_value=tg_file)),
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), message=message)


def replies(update):
    return [c.args[0] for c in update.message.reply_text.call_args_list]


async def say(script, b, transcript, result=None):
    script.transcripts.append(transcript)
    if result is not None:
        script.verdicts.append(result)
    u = voice_update()
    await bot.handle_voice(u, SimpleNamespace(bot=b, user_data={}))
    return u


def today():
    return bot._user_local_today(USER_ID)


async def voice_scenarios():
    script = Script()
    bot.stt_helper.transcribe = script.transcribe
    bot.stt_helper.transcribe_word = script.transcribe
    bot.ai_helper.check_practice_phrase = script.check
    active = db.get_active_practice_session(USER_ID, today())
    if active:
        db.finish_practice(active["id"])

    print("\n5. Маршрутизация голосовых")
    b = SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock())
    script.transcripts.append("счёт")
    u = voice_update()
    await bot.handle_voice(u, SimpleNamespace(bot=b, user_data={}))
    check("владелец без практики → добавление слова", "Добавить в словарь?" in replies(u)[0])
    yesterday = db.create_practice_session(USER_ID, "2000-01-01", [1, 2, 3])
    script.transcripts.append("пляж")
    u = voice_update()
    await bot.handle_voice(u, SimpleNamespace(bot=b, user_data={}))
    check("незаконченная вчерашняя практика не перехватывает голосовое",
          "Добавить в словарь?" in replies(u)[0]
          and db.get_practice_session(yesterday["id"])["attempts"] == 0)

    print("\n6. Ошибка → перезапись → верно")
    s = db.create_practice_session(USER_ID, today(), [1, 2, 3])
    sid = s["id"]
    u = await say(script, b, "Yo pido <la cuenta>", verdict(is_correct=False, corrected="Pido la cuenta",
                                                          explanation="Лишнее yo — звучит неестественно"))
    r = replies(u)
    print("     " + "\n     ".join(x.replace("\n", "\n     ") for x in r))
    check("сначала «Услышала» с транскриптом", r[0] == "🎧 Услышала: «Yo pido <la cuenta>»")
    check("разбор с правильным вариантом", "💡" in r[1] and "<i>Pido la cuenta</i>" in r[1])
    check("просит записать ещё раз, с кнопками", "Запиши ещё раз" in r[1]
          and u.message.reply_text.call_args.kwargs.get("reply_markup") is not None)
    check("остались на слове 1, попытка записана",
          db.get_practice_session(sid)["current_index"] == 0 and db.get_practice_session(sid)["attempts"] == 1)

    u = await say(script, b, "Pido la cuenta, por favor", verdict())
    check("верно → «✅ Верно!»", replies(u)[-1].startswith("✅ Верно!"))
    check("сразу пришло слово 2", "Слово 2 из 3" in sent_texts(b)[-1])
    check("исход слова 1 — after_fix", db.get_practice_session(sid)["outcomes"] == ["after_fix"])

    print("\n7. Верно с первого раза, но слишком просто")
    u = await say(script, b, "Estoy cansada", verdict(too_simple=True, suggestion="Добавь, почему и после чего"))
    check("засчитано, но предложено усложнить", "простая" in replies(u)[-1] and "почему" in replies(u)[-1])
    check("слово не закрыто автоматически", db.get_practice_session(sid)["current_index"] == 1)
    await bot._handle_practice_button(fake_query(b), "practice_next", ["practice_next", str(sid), "1"])
    check("«Дальше» после простой верной фразы — исход first_try",
          db.get_practice_session(sid)["outcomes"] == ["after_fix", "first_try"])

    print("\n8. Нечёткая запись и сбой распознавания не тратят попытку")
    u = await say(script, b, "mmm eh", verdict(unclear=True))
    check("нечётко → «Не разобрала»", "Не разобрала" in replies(u)[-1])
    calls = script.claude_calls
    u = await say(script, b, RuntimeError("openai down"))
    check("сбой STT → просьба записать ещё раз", "Не получилось распознать" in replies(u)[-1])
    check("при сбое STT Claude не вызывался", script.claude_calls == calls)
    u = await say(script, b, "")
    check("пустой транскрипт → «Ничего не расслышала»", "Ничего не расслышала" in replies(u)[-1])
    check("попытки на слове 3 не засчитаны", db.get_practice_session(sid)["attempts"] == 0)

    print("\n9. Три неудачи подряд → идём дальше")
    bad = verdict(is_correct=False, corrected="Voy a la playa", explanation="Не то время")
    await say(script, b, "Yo fue a la playa", bad)
    await say(script, b, "Yo fue a la playa", bad)
    u = await say(script, b, "Yo fue a la playa", bad)
    s = db.get_practice_session(sid)
    check("после 3-й попытки — «идём дальше»", "идём дальше" in replies(u)[-1])
    check("сессия закончена, исход not_yet", s["status"] == "done" and s["outcomes"][-1] == "not_yet")
    check("финальное сообщение", "закончена" in sent_texts(b)[-1])

    print("\n9б. Итог сессии")
    summary = sent_texts(b)[-1]
    print("     " + summary.replace("\n", "\n     "))
    check("итог в HTML", b.send_message.call_args.kwargs.get("parse_mode") == "HTML")
    check("слово 1 — после правки, с исправленной фразой",
          "🟡 после правки — <b>la cuenta</b>" in summary and "<i>Pido la cuenta</i>" in summary)
    check("слово 2 — сразу, без фразы-правки", "✅ сразу — <b>cansado</b>" in summary)
    check("слово 3 — пока не получилось, с правкой",
          "🔸 пока не получилось — <b>la playa</b>" in summary and "<i>Voy a la playa</i>" in summary)

    print("\n10. Нажали «Дальше», пока шёл разбор")
    s = db.create_practice_session(USER_ID, today(), [1, 2, 3])
    script.transcripts.append("Pido cuenta")

    def check_and_press(phrase, meaning, transcript):
        db.advance_practice(s["id"], "skipped")  # imitate the button press landing mid-processing
        return verdict(is_correct=False, corrected="Pido la cuenta", explanation="Нужен артикль")

    bot.ai_helper.check_practice_phrase = check_and_press
    u = voice_update()
    await bot.handle_voice(u, SimpleNamespace(bot=b, user_data={}))
    s_after = db.get_practice_session(s["id"])
    check("разбор всё равно показан", "Pido la cuenta" in replies(u)[-1])
    check("попытка не записана на уже закрытое слово", s_after["attempts"] == 0 and s_after["current_index"] == 1)

    print("\n11. «Закончить» посреди слова с попыткой — итог включает текущее слово")
    bot.ai_helper.check_practice_phrase = script.check
    s = db.create_practice_session(USER_ID, today(), [1, 2, 3])
    await say(script, b, "Estoy cansado <mucho>", verdict(is_correct=False, corrected="Estoy muy cansado",
                                                        explanation="Перед прилагательным — muy"))
    await bot._handle_practice_button(fake_query(b), "practice_stop", ["practice_stop", str(s["id"]), "0"])
    summary = sent_texts(b)[-1]
    print("     " + summary.replace("\n", "\n     "))
    s_after = db.get_practice_session(s["id"])
    check("сессия done, один исход not_yet", s_after["status"] == "done" and s_after["outcomes"] == ["not_yet"])
    check("в итоге только слово 1 с правкой",
          "la cuenta" in summary and "Estoy muy cansado" in summary and "cansado</b>" not in summary)

    await voice_word_scenarios(script, b)


async def voice_word_scenarios(script, b):
    print("\n12. Надиктовать новое слово")
    explained = []

    def fake_explain(word):
        explained.append(word)
        return {"phrase": "la ventana", "meaning": "окно", "part_of_speech": "существительное",
                "cefr_level": "A1", "examples": ["Abro la ventana — Открываю окно"],
                "collocations": [], "conjugation": None, "gerund": None}

    bot.ai_helper.explain_word = fake_explain
    ctx = SimpleNamespace(bot=b, user_data={})

    def word_query(user_id=USER_ID):
        return SimpleNamespace(from_user=SimpleNamespace(id=user_id),
                               message=SimpleNamespace(reply_text=AsyncMock()),
                               edit_message_reply_markup=AsyncMock())

    def query_replies(q):
        return [c.args[0] for c in q.message.reply_text.call_args_list]

    script.transcripts.append("окно")
    u = voice_update()
    await bot.handle_voice(u, ctx)
    check("показано, что услышала, и спрошено подтверждение",
          replies(u)[-1].startswith("🎧 Услышала: «окно»") and "Добавить в словарь?" in replies(u)[-1])
    token = ctx.user_data["pending_voice_word"]["token"]

    q = word_query()
    await bot._handle_voice_add_button(q, ctx, ["voice_add", token, "no"])
    check("«Отмена» — не добавляет и не зовёт Claude",
          "не добавляю" in query_replies(q)[-1] and not explained and "pending_voice_word" not in ctx.user_data)

    script.transcripts.append("окно")
    await bot.handle_voice(voice_update(), ctx)
    token = ctx.user_data["pending_voice_word"]["token"]
    q = word_query()
    await bot._handle_voice_add_button(q, ctx, ["voice_add", token, "yes"])
    check("«Добавить» — Claude получил распознанный текст", explained == ["окно"])
    check("слово в словаре", db.find_word_by_phrase(USER_ID, "la ventana") is not None)
    check("пришла обычная карточка слова", any(r.startswith("✅ *la ventana*") for r in query_replies(q)))
    check("кнопки подтверждения убраны", q.edit_message_reply_markup.await_count == 1)

    q = word_query()
    await bot._handle_voice_add_button(q, ctx, ["voice_add", token, "yes"])
    check("повторное нажатие старой кнопки — «устарела», без второго вызова Claude",
          "устарела" in query_replies(q)[-1] and explained == ["окно"])

    script.transcripts.append("la ventana")
    await bot.handle_voice(voice_update(), ctx)
    token = ctx.user_data["pending_voice_word"]["token"]
    q = word_query()
    await bot._handle_voice_add_button(q, ctx, ["voice_add", token, "yes"])
    check("дубль — «уже есть», Claude не вызывался", "уже есть" in query_replies(q)[-1] and len(explained) == 1)

    ctx.user_data.clear()
    script.transcripts.append("Bueno, pues hoy he estado pensando en muchas cosas que quiero aprender en español")
    u = voice_update()
    await bot.handle_voice(u, ctx)
    check("длинная запись — просьба надиктовать короче, без подтверждения",
          "длинновато" in replies(u)[-1] and "pending_voice_word" not in ctx.user_data)

    script.transcripts.append("кошка")
    friend_ctx = SimpleNamespace(bot=b, user_data={})
    u = voice_update(OTHER_ID)
    await bot.handle_voice(u, friend_ctx)
    check("не-владелец тоже может надиктовать слово", "Добавить в словарь?" in replies(u)[-1])


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
          db.get_active_practice_session(USER_ID, today())["word_ids"] == [1, 2, 3])

    await bot._maybe_start_evening_practice(SimpleNamespace(bot=b))
    check("повторный запуск в тот же час (рестарт) — дубля нет", b.send_message.call_count == 1)

    session = db.get_active_practice_session(USER_ID, today())
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
    s2 = db.get_active_practice_session(USER_ID, today())
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

    await voice_scenarios()

    print()
    if failures:
        print(f"❌ Провалено: {len(failures)}")
        sys.exit(1)
    print("✅ Все проверки пройдены")


if __name__ == "__main__":
    asyncio.run(main())
    os.remove(DB_FILE)
