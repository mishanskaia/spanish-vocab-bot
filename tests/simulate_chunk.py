"""
Local test harness for the phrase loop of the voice conversations (voice_talk «Chunk loop»):
one phrase at a time, carried into the next conversations and shown on the page, done after
she has used it in two of them. Mistakes are shown (1–2), not carried.

Real db.py / voice_talk.py on a throwaway DB; Claude is stubbed.

Usage:
    python tests/simulate_chunk.py
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
DB_FILE = os.path.join(ROOT, "tests", "_scratch_chunk.db")
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


FUE = {"said": "yo fue al cine", "fix": "yo fui al cine", "why": "прошедшее время ir", "repeated": True}
PLAYA = {"said": "el playa", "fix": "la playa", "why": "род существительного", "repeated": False}
GANAS = {"said": "no toco nada", "fix": "no toco nada", "why": "", "repeated": False}  # fix == said → not a mistake
OPINION = {"said": "pienso que es bueno", "better": "me parece que está bien", "chunk": "me parece que",
           "why": "мнение — готовый оборот"}
WANT = {"said": "quiero mucho ir", "better": "tengo ganas de ir", "chunk": "tener ganas de", "why": "хочется"}


def analysis(result="not_used", upgrade=None, errors=None):
    return {"errors": errors or [], "chunk_result": result, "upgrade": upgrade or {}}


def finish_session(a):
    """What handle_transcript does after the review: store it, move the phrase loop on."""
    sid = db.create_voice_session(USER, [], None, "x")
    db.save_voice_transcript(sid, [{"role": "user", "text": "hola"}], {}, True)
    db.set_voice_analysis(sid, a)
    state = voice_talk.apply_chunk(USER, a)
    return state, voice_talk.build_analysis_message(a, state)


def main():
    db.init_db()

    print("\n1. Первый разговор: две главные ошибки и новый оборот")
    state, msg = finish_session(analysis(upgrade=OPINION, errors=[FUE, PLAYA, {**PLAYA, "said": "el mano", "fix": "la mano"}]))
    chunk = db.get_active_chunk(USER)
    check("оборот стал текущим", chunk and chunk["chunk"] == "me parece que" and chunk["uses"] == 0)
    check("в сообщении две ошибки и «Новый оборот»", "yo fui al cine" in msg and "la playa" in msg and "Новый оборот" in msg)
    check("третья ошибка не показана", "la mano" not in msg)
    check("повторяющаяся ошибка помечена", "(повторяется)" in msg)
    check("фокуса больше нет", "Фокус" not in msg)

    print("\n2. Следующий разговор получает оборот — и в /talk, и в /scene")
    prompt = voice_talk.build_instructions([], [], "food", (), chunk)
    check("в промпте оборот и пример", "me parece que" in prompt and "me parece que está bien" in prompt)
    check("вопрос по текущей теме, не менять тему", "never change the topic" in prompt)
    check("любая форма засчитывается", "Any form counts" in prompt)
    scene_prompt = voice_talk.build_scene_instructions({"seen": "x", "words": [{"es": "a", "ru": "b"}]}, [], (), chunk)
    check("в картинке — оборот, держаться картинки", "me parece que" in scene_prompt and "Stay with the picture" in scene_prompt)
    check("без оборота блока нет", "learning to use" not in voice_talk.build_instructions([], [], "food"))

    print("\n3. Сказала — 1 из 2; новый оборот не появляется, пока старый не освоен")
    state, msg = finish_session(analysis("used", upgrade=WANT))
    check("использований 1", db.get_active_chunk(USER)["uses"] == 1)
    check("в сообщении прогресс «1 из 2»", "1 из 2" in msg)
    check("кандидат не заменил текущий", db.get_active_chunk(USER)["chunk"] == "me parece que" and "Новый оборот" not in msg)

    print("\n4. Не прозвучал — счётчик не меняется и не обнуляется")
    state, msg = finish_session(analysis("not_used"))
    check("использований всё ещё 1", db.get_active_chunk(USER)["uses"] == 1)
    check("сообщение «не прозвучал»", "не прозвучал" in msg)

    print("\n5. Второй раз — оборот освоен, новый из этого разговора")
    state, msg = finish_session(analysis("used", upgrade=WANT))
    active = db.get_active_chunk(USER)
    check("новый — tener ganas de", active and active["chunk"] == "tener ganas de")
    check("в сообщении «освоен» и «Новый оборот»", "освоен" in msg and "Новый оборот" in msg)

    print("\n6. Разбор: контекст и проверка ответа модели")
    ctx = voice_talk.review_context(USER)
    check("в контекст идут прошлые ошибки", any(e["fix"] == "la playa" for e in ctx["past_errors"]))
    check("и уже данные обороты", "me parece que" in ctx["past_chunks"] and "tener ganas de" in ctx["past_chunks"])
    sent = {}
    reply = {"errors": [FUE, GANAS], "chunk_result": "maybe", "upgrade": OPINION}

    def fake_create(**kw):
        sent.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))])

    ai_helper.client = SimpleNamespace(messages=SimpleNamespace(create=fake_create))
    transcript = [{"role": "assistant", "text": "¿Qué tal?"}, {"role": "user", "text": "yo fue al cine"},
                  {"role": "assistant", "text": "¿Y qué viste?"}, {"role": "user", "text": "una película"}]
    a = voice_talk.analyze_conversation(transcript, ctx["chunk"], ctx["past_errors"], ctx["past_chunks"])
    content = sent["messages"][0]["content"]
    check("модели передан текущий оборот и прошлые ошибки", "Текущий оборот: «tener ganas de»" in content
          and "Её ошибки в прошлых" in content)
    check("и уже данные обороты с просьбой выбрать другой", "выбери другой" in content)
    check("неизвестный chunk_result → not_used", a["chunk_result"] == "not_used")
    check("ошибка с fix == said выброшена", len(a["errors"]) == 1)
    check("уже данный оборот выброшен кодом", a["upgrade"] == {})
    reply["chunk_result"] = "used"
    check("без текущего оборота результат всегда not_used",
          voice_talk.analyze_conversation(transcript)["chunk_result"] == "not_used")
    check("почти без её реплик — пустой разбор без вызова",
          voice_talk.analyze_conversation(transcript[:2]) == voice_talk._EMPTY_ANALYSIS)

    print("\n7. Недельный отчёт — вокруг оборотов")
    report = voice_talk.build_weekly_report(USER)
    check("освоенный и текущий", "Обороты освоены" in report and "me parece que" in report
          and "Сейчас в работе" in report and "tener ganas de" in report)

    other = USER + 1
    check("нет ни оборотов, ни разговоров — отчёта нет", voice_talk.build_weekly_report(other) is None)

    print("\n" + ("✅ Все проверки прошли" if not failures else f"❌ Упало: {len(failures)}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
