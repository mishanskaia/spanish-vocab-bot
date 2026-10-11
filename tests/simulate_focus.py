"""
Local test harness for the focus loop of the voice conversations (voice_talk «Focus loop»):
one mistake at a time, carried into the next conversations, closed after two correct uses.

Real db.py / voice_talk.py on a throwaway DB; Claude is stubbed.

Usage:
    python tests/simulate_focus.py
"""
import json
import os
import sys
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

USER = 424242
os.environ.setdefault("ANTHROPIC_API_KEY", "test")  # ai_helper builds its client at import
DB_FILE = os.path.join(ROOT, "tests", "_scratch_focus.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
os.environ["DB_PATH"] = DB_FILE

import ai_helper
import db
import voice_talk

failures = []


def check(name, condition):
    print(("  ✅ " if condition else "  ❌ ") + name)
    if not condition:
        failures.append(name)


FUE = {"rule": "ir в прошедшем: fui", "said": "yo fue al cine", "fix": "yo fui al cine",
       "why": "прошедшее время ir", "trigger": "¿Adónde fuiste el sábado?"}
PLAYA = {"rule": "род: la playa", "said": "el playa", "fix": "la playa", "why": "род существительного",
         "trigger": "¿Te gusta ir a la playa?"}
UPGRADE = {"said": "pienso que es bueno", "better": "la verdad es que me encanta", "why": "готовый оборот"}


def analysis(focus=None, result="not_used", upgrade=None, errors=None):
    return {"errors": errors or [], "focus": focus or {}, "focus_result": result, "upgrade": upgrade or {}}


def finish_session(a):
    """What handle_transcript does after the review: store it, move the focus loop on."""
    sid = db.create_voice_session(USER, [], None, "x")
    db.save_voice_transcript(sid, [{"role": "user", "text": "hola"}], {}, True)
    db.set_voice_analysis(sid, a)
    state = voice_talk.apply_focus(USER, a)
    return state, voice_talk.build_analysis_message(a, state)


def main():
    db.init_db()

    print("\n1. Первый разговор: появляется фокус, сообщение из двух вещей")
    state, msg = finish_session(analysis(FUE, upgrade=UPGRADE, errors=[FUE, PLAYA]))
    focus = db.get_active_focus(USER)
    check("фокус создан из выбранной ошибки", focus and focus["right_form"] == "yo fui al cine" and focus["streak"] == 0)
    check("в сообщении «Новый фокус» и «Оборот»", "Новый фокус" in msg and "Оборот" in msg)
    check("остальные ошибки в сообщение не попали", "el playa" not in msg)
    check("блока «знаешь, но не использовала» нет", "не использовала" not in msg)

    print("\n2. Следующий разговор получает фокус и оборот")
    f, phrase = voice_talk.current_practice(USER)
    prompt = voice_talk.build_instructions([], [], "food", (), f, phrase)
    check("в промпте — правильная форма и вопрос-повод", "yo fui al cine" in prompt and "¿Adónde fuiste el sábado?" in prompt)
    check("если обошла — бот говорит сам и предлагает употребить", "say exactly the right form" in prompt)
    check("оборот — бот употребляет сам, без проверки", "la verdad es que me encanta" in prompt and "Don't ask her to repeat" in prompt)
    scene_prompt = voice_talk.build_scene_instructions({"seen": "x", "words": [{"es": "a", "ru": "b"}]}, [], (), f, phrase)
    check("в картинке фокус — в части про её жизнь", "phase 3" in scene_prompt and "yo fui al cine" in scene_prompt)
    check("без фокуса и оборота блока нет", "current focus" not in voice_talk.build_instructions([], [], "food"))

    print("\n3. Сказала правильно — 1 из 2; новый фокус не появляется, пока старый не снят")
    state, msg = finish_session(analysis(PLAYA, result="correct"))
    check("серия 1", db.get_active_focus(USER)["streak"] == 1)
    check("в сообщении прогресс «1 из 2»", "1 из 2" in msg)
    check("кандидат не заменил текущий фокус", db.get_active_focus(USER)["right_form"] == "yo fui al cine"
          and "Новый фокус" not in msg)

    print("\n4. Ошибка снова — серия обнуляется")
    state, msg = finish_session(analysis(PLAYA, result="wrong"))
    check("серия 0", db.get_active_focus(USER)["streak"] == 0)
    check("сообщение «пока в работе» с правкой", "пока в работе" in msg and "yo fui al cine" in msg)

    print("\n5. Не прозвучало — серия не меняется")
    finish_session(analysis(PLAYA, result="correct"))
    state, msg = finish_session(analysis(PLAYA, result="not_used"))
    check("серия осталась 1", db.get_active_focus(USER)["streak"] == 1)
    check("сообщение «не прозвучало»", "не прозвучало" in msg)

    print("\n6. Второй раз правильно — фокус снят, новый из этого разговора")
    state, msg = finish_session(analysis(PLAYA, result="correct", upgrade=UPGRADE))
    active = db.get_active_focus(USER)
    check("старый снят, новый — la playa", active and active["right_form"] == "la playa")
    check("в сообщении «снят» и «Новый фокус»", "снят" in msg and "Новый фокус" in msg)
    check("не больше трёх строк-пунктов", msg.count("<b>") <= 6)

    print("\n7. Разбор: контекст и проверка ответа модели")
    ctx = voice_talk.review_context(USER)
    check("в контекст идут прошлые ошибки", any(e["fix"] == "la playa" for e in ctx["past_errors"]))
    check("и уже предложенные обороты", "la verdad es que me encanta" in ctx["past_upgrades"])
    sent = {}

    def fake_create(**kw):
        sent.update(kw)
        reply = {"errors": [FUE], "focus": {**PLAYA}, "focus_result": "maybe", "upgrade": {"said": "", "better": "x", "why": ""}}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))])

    ai_helper.client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    transcript = [{"role": "assistant", "text": "¿Qué tal?"}, {"role": "user", "text": "yo fue al cine"},
                  {"role": "assistant", "text": "¿Y qué viste?"}, {"role": "user", "text": "una película"}]
    a = voice_talk.analyze_conversation(transcript, ctx["focus"], ctx["past_errors"], ctx["past_upgrades"])
    content = sent["messages"][0]["content"]
    check("модели передан текущий фокус и прошлые ошибки", "Текущий фокус" in content and "Её ошибки в прошлых" in content)
    check("и уже предложенные обороты с просьбой выбрать другой", "выбери другой" in content)
    check("неизвестный focus_result → not_used", a["focus_result"] == "not_used")
    check("оборот без своей фразы выброшен", a["upgrade"] == {})
    check("без текущего фокуса результат всегда not_used",
          voice_talk.analyze_conversation(transcript)["focus_result"] == "not_used")
    check("почти без её реплик — пустой разбор без вызова",
          voice_talk.analyze_conversation(transcript[:2]) == voice_talk._EMPTY_ANALYSIS)

    print("\n8. Недельный отчёт — вокруг фокусов")
    report = voice_talk.build_weekly_report(USER)
    check("снятый фокус и текущий", "Фокусы сняты" in report and "yo fui al cine" in report
          and "Сейчас в работе" in report and "la playa" in report)
    check("блока «повторяющиеся ошибки» больше нет", "Повторяющиеся ошибки" not in report)

    other = USER + 1
    check("нет ни фокусов, ни разговоров — отчёта нет", voice_talk.build_weekly_report(other) is None)

    print("\n" + ("✅ Все проверки прошли" if not failures else f"❌ Упало: {len(failures)}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
