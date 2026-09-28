"""
Local test harness for the /scene prototype (scene_practice.py + the bot handlers).

Real db.py / bot.py on a throwaway DB; Claude and the image model are stubbed.

Usage:
    python tests/simulate_scene.py
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OWNER_ID = 424242

os.environ["TELEGRAM_BOT_TOKEN"] = "123:test-token"
os.environ["OWNER_TELEGRAM_ID"] = str(OWNER_ID)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")  # ai_helper builds its client at import
os.environ["OPENAI_API_KEY"] = "test"
DB_FILE = os.path.join(ROOT, "tests", "_scratch_scene.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
os.environ["DB_PATH"] = DB_FILE

import asyncio
from types import SimpleNamespace

import ai_helper
import bot
import db
import scene_practice

failures = []


def check(name, condition):
    print(("  ✅ " if condition else "  ❌ ") + name)
    if not condition:
        failures.append(name)


def seed():
    db.init_db()
    for phrase, meaning in [
        ("el paraguas", "зонт"), ("la parada", "остановка"), ("mojado", "мокрый"),
        ("esperar", "ждать"), ("la sartén", "сковорода"), ("cocinar", "готовить"),
    ]:
        word_id, _ = db.add_word(OWNER_ID, phrase, meaning, "другое", "A1", ["a — б"])
        db.mark_review_result(word_id, "remember")  # stage 1 → learning
    # a fresh card nobody reviewed yet — not "knows or almost knows"
    db.add_word(OWNER_ID, "la escoba", "веник", "существительное", "A1", ["a — б"])
    # catalog «знаю» marks: one new, one that already has a card
    db.set_catalog_known(OWNER_ID, "casa", "casa", True)
    db.set_catalog_known(OWNER_ID, "paraguas", "paraguas", True)


SCENES_REPLY = {"scenes": [
    {"title": "Дождь на остановке", "words": ["el paraguas", "la parada", "mojado", "esperar"],
     "picture": "A woman waits at a bus stop in the rain.",
     "must_show": ["an open umbrella", "a bus stop", "wet street", "she is waiting"]},
    # an invented word and a duplicate: only two real words left → dropped
    {"title": "Кухня", "words": ["la sartén", "el horno", "la sartén"],
     "picture": "A kitchen.", "must_show": ["a pan", "an oven", "a pan"]},
    {"title": "Ужин дома", "words": ["La Sartén", "cocinar", "casa"],
     "picture": "A man cooks dinner at home.", "must_show": ["a frying pan", "cooking", "a cosy flat"]},
]}


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(("text", text, kw.get("reply_markup")))

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.sent.append(("photo", caption, photo))

    async def send_chat_action(self, *a, **kw):
        pass


class FakeMessage:
    chat_id = OWNER_ID

    def __init__(self, bot_):
        self.bot_ = bot_

    async def reply_text(self, text, **kw):
        self.bot_.sent.append(("text", text, kw.get("reply_markup")))


class FakeQuery:
    def __init__(self, bot_, data):
        self.bot_, self.data = bot_, data
        self.from_user = SimpleNamespace(id=OWNER_ID)
        self.message = FakeMessage(bot_)

    def get_bot(self):
        return self.bot_

    async def edit_message_reply_markup(self, reply_markup=None):
        pass


async def main():
    seed()

    print("\n1. Пул слов")
    pool = scene_practice.build_pool(OWNER_ID)
    es = {w["es"] for w in pool}
    check("карточки после повторения попали в пул", {"el paraguas", "cocinar"} <= es)
    check("свежая карточка без повторений — нет", "la escoba" not in es)
    check("отметка «знаю» из каталога — да, с переводом из списка",
          any(w["es"] == "casa" and w["ru"] for w in pool))
    check("слово с карточкой и отметкой не задвоилось",
          sum(1 for w in pool if w["es"] in ("el paraguas", "paraguas")) == 1)

    print("\n2. Сцены от Claude проверяются кодом")
    prompts = []

    def fake_ask(prompt, max_tokens=2500, system=None):
        prompts.append((prompt, system))
        return SCENES_REPLY

    ai_helper._ask_claude = fake_ask
    scenes = scene_practice.propose_scenes(pool)
    check("свой system-промпт, не промпт объяснения слов", prompts[0][1] == scene_practice.SCENES_SYSTEM)
    check("все слова пула ушли в промпт", all(w["es"] in prompts[0][0] for w in pool))
    check("сцена с выдуманным словом и дублем выброшена", [s["title"] for s in scenes] == ["Дождь на остановке", "Ужин дома"])
    check("слово приведено к написанию из пула", scenes[1]["words"][0]["es"] == "la sartén")
    check("перевод берётся из пула", scenes[0]["words"][0]["ru"] == "зонт")
    prompt = scene_practice.image_prompt(scenes[0])
    check("в промпте картинки — что должно быть видно", "an open umbrella" in prompt)
    check("и запрет на текст (он бы выдал ответ)", "no text" in prompt.lower())

    print("\n3. Команда и кнопки")
    fbot = FakeBot()
    context = SimpleNamespace(bot=fbot, user_data={})
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=OWNER_ID),
        effective_chat=SimpleNamespace(id=OWNER_ID),
        message=FakeMessage(fbot),
    )
    await bot.scene(update, context)
    kind, text, markup = fbot.sent[-1]
    buttons = [b for row in markup.inline_keyboard for b in row]
    check("предложены сцены кнопками + «Другие сцены»",
          [b.text for b in buttons] == ["Дождь на остановке", "Ужин дома", "🔄 Другие сцены"])
    check("callback_data влезает в 64 байта", all(len(b.callback_data.encode()) <= 64 for b in buttons))

    images = []

    def fake_image(scene):
        images.append(scene["title"])
        return b"PNG"

    scene_practice.generate_image = fake_image
    await bot.on_button(SimpleNamespace(callback_query=_answerable(FakeQuery(fbot, buttons[0].callback_data))), context)
    photos = [s for s in fbot.sent if s[0] == "photo"]
    check("нарисована выбранная сцена", images == ["Дождь на остановке"])
    check("слова под спойлером в подписи", photos and "<tg-spoiler>el paraguas — зонт" in photos[-1][1])
    check("после картинки снова можно выбрать другую сцену", fbot.sent[-1][2] is not None)

    await bot.on_button(SimpleNamespace(callback_query=_answerable(FakeQuery(fbot, "scene:deadbeef:0"))), context)
    check("старая кнопка → «устарели», без генерации", "устарели" in fbot.sent[-1][1] and len(images) == 1)

    def broken_image(scene):
        raise RuntimeError("boom")

    scene_practice.generate_image = broken_image
    await bot.on_button(SimpleNamespace(callback_query=_answerable(FakeQuery(fbot, buttons[1].callback_data))), context)
    check("сбой генерации — сообщение и кнопки, а не тишина",
          "не получилась" in fbot.sent[-1][1] and fbot.sent[-1][2] is not None)

    other = SimpleNamespace(
        effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=1), message=FakeMessage(fbot)
    )
    before = len(fbot.sent)
    await bot.scene(other, context)
    check("не владельцу — тишина", len(fbot.sent) == before)

    os.remove(DB_FILE)
    print("\n" + ("✅ Все проверки прошли" if not failures else f"❌ Упало: {len(failures)}"))
    sys.exit(1 if failures else 0)


def _answerable(query):
    async def answer(*a, **kw):
        pass
    query.answer = answer
    return query


asyncio.run(main())
