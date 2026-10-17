"""
Local test harness for the words she asks about in a voice conversation ("¿qué significa…?",
"как сказать…?"): kept only if really asked this turn, stored with the found words, shown in
the summary under their own heading and offered with an add button.

Real db.py / voice_talk.py on a throwaway DB; no Claude calls.

Usage:
    python tests/simulate_asked.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

USER = 434343
os.environ.setdefault("ANTHROPIC_API_KEY", "test")  # ai_helper builds its client at import
DB_FILE = os.path.join(ROOT, "tests", "_scratch_asked.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
os.environ["DB_PATH"] = DB_FILE

import db
import voice_talk

failures = []


def check(name, condition):
    print(("  ✅ " if condition else "  ❌ ") + name)
    if not condition:
        failures.append(name)


def main():
    db.init_db()
    history = [{"role": "assistant", "text": "Hay que aguantar el frío en Moscú, ¿no?"}]

    print("\n1. Что считается вопросом этой реплики")
    asked = voice_talk._asked_now([{"es": "aguantar", "ru": "терпеть"}], "¿Qué significa aguantar?", history)
    check("слово из её вопроса", asked == [{"es": "aguantar", "ru": "терпеть", "kind": "asked"}])
    check("«¿qué es eso?» — слово из прошлой реплики бота",
          len(voice_talk._asked_now([{"es": "aguantar", "ru": "терпеть"}], "¿Qué es eso?", history)) == 1)
    check("«как сказать граница» — русское слово в её реплике",
          len(voice_talk._asked_now([{"es": "la frontera", "ru": "граница"}], "как сказать граница", history)) == 1)
    check("старое слово, которого нет ни у неё, ни у бота — выброшено",
          voice_talk._asked_now([{"es": "la frontera", "ru": "граница"}], "Sí, hace mucho frío", history) == [])
    check("мусор от модели выброшен", voice_talk._asked_now([{"es": "", "ru": "x"}, "x"], "¿qué?", history) == [])

    print("\n2. Хранение, итог и кнопки")
    sid = db.create_voice_session(USER, [], None, "x")
    db.add_voice_found_words(sid, [{"ru": "шапка", "es": "el gorro"}])
    db.add_voice_found_words(sid, asked)
    db.add_voice_found_words(sid, asked)  # re-sent utterance
    db.save_voice_transcript(sid, [{"role": "user", "text": "hola"}], {}, True)
    session = db.get_voice_session(sid)
    check("без дублей, kind сохранён", [p.get("kind") for p in session["found_words"]] == [None, "asked"])
    summary = voice_talk.build_summary(session)
    check("в итоге два раздела", "Искала по-испански" in summary and "шапка → el gorro" in summary
          and "Спрашивала" in summary and "aguantar — терпеть" in summary)
    keyboard = voice_talk.found_words_keyboard(session)
    labels = [row[0].text for row in keyboard.inline_keyboard]
    check("кнопка на каждое слово", labels == ["➕ el gorro", "➕ aguantar"])
    check("индекс кнопки совпадает со словом", keyboard.inline_keyboard[1][0].callback_data == f"talkadd:{sid}:1")

    print("\n3. Только вопросы — тоже итог с кнопкой")
    sid2 = db.create_voice_session(USER, [], None, "x")
    db.add_voice_found_words(sid2, asked)
    db.save_voice_transcript(sid2, [{"role": "user", "text": "hola"}], {}, True)
    summary2 = voice_talk.build_summary(db.get_voice_session(sid2))
    check("раздела «Искала» нет, «Спрашивала» есть", "Искала" not in summary2 and "Спрашивала" in summary2)

    print("\n" + ("✅ Все проверки прошли" if not failures else f"❌ Упало: {len(failures)}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
