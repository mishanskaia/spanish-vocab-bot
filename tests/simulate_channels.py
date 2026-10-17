"""
Local test harness for the channel digest (channel_digest.py + the bot handlers): reading the
web preview, the archive limit, labelling, the daily pick (rotation, what she knows, duplicates)
and the buttons.

Real db.py / bot.py on a throwaway DB; the channel pages are synthetic and Claude is stubbed.

Usage:
    python tests/simulate_channels.py
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
DB_FILE = os.path.join(ROOT, "tests", "_scratch_channels.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
os.environ["DB_PATH"] = DB_FILE

import asyncio
import json
from types import SimpleNamespace

import ai_helper
import bot
import channel_digest as cd
import db

failures = []


def check(name, condition):
    print(("  ✅ " if condition else "  ❌ ") + name)
    if not condition:
        failures.append(name)


def post_html(channel, post_id, text, photo=False, poll=False, reply=False):
    photo_html = '<a class="tgme_widget_message_photo_wrap" href="#"></a>' if photo else ""
    poll_html = '<div class="tgme_widget_message_poll"><div>¿?</div></div>' if poll else ""
    reply_html = ('<a class="tgme_widget_message_reply"><div class="tgme_widget_message_text js-message_reply_text">'
                  'QUOTED</div></a>') if reply else ""
    return (f'<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message '
            f'js-widget_message" data-post="{channel}/{post_id}"><div class="tgme_widget_message_bubble">'
            f'{reply_html}{photo_html}{poll_html}'
            f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
            f'<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="#">'
            f'<time datetime="2026-10-0{post_id % 9 + 1}T10:00:00+00:00" class="time">10:00</time></a></span>'
            f'</div></div></div>')


LONG = "Сегодня разберём фразу, которую испанцы говорят каждый день. Очень полезно!"


class FakeChannel:
    """Posts 1..n; a page is 20 posts before `before` (newest first in the archive)."""

    def __init__(self, name, n):
        self.name, self.n, self.requests = name, n, 0

    def page(self, before):
        self.requests += 1
        top = (before - 1) if before else self.n
        ids = range(max(1, top - 19), top + 1)
        parts = []
        for i in ids:
            if i % 10 == 0:
                parts.append(post_html(self.name, i, "Опрос", poll=True))
            elif i % 10 == 5:
                parts.append(post_html(self.name, i, "Сохраняй 👇", photo=True))
            else:
                parts.append(post_html(self.name, i, f"<b>Пост {i}</b><br/>{LONG}<br/>¡Hola!", photo=(i % 10 == 3),
                                       reply=(i % 10 == 7)))
        return "<html>" + "".join(parts) + "</html>"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((text, kw.get("reply_markup"), kw))


def fake_claude(reply_for):
    def create(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply_for(kw)))])
    ai_helper.client = SimpleNamespace(messages=SimpleNamespace(create=create))


def main():
    db.init_db()
    cd.REQUEST_PAUSE_S = 0
    cd.CHANNELS = ["chan_a", "chan_b"]
    chans = {"chan_a": FakeChannel("chan_a", 120), "chan_b": FakeChannel("chan_b", 30)}
    fetch = lambda channel, before=None: chans[channel].page(before)

    print("\n1. Разбор страницы")
    posts = cd.parse_page(chans["chan_a"].page(None))
    by_id = {p["post_id"]: p for p in posts}
    check("20 постов со страницы", len(posts) == 20)
    check("текст с переносами, без тегов", by_id[111]["text"].startswith("Пост 111\nСегодня") and "<b>" not in by_id[111]["text"])
    check("цитата ответа не попала в текст", "QUOTED" not in by_id[117]["text"] and "Пост 117" in by_id[117]["text"])
    check("картинка и опрос замечены", by_id[113]["has_photo"] and by_id[120]["poll"] and not by_id[111]["has_photo"])
    check("дата поста", by_id[111]["posted_at"].startswith("2026-10-"))

    print("\n2. Выкачка: лимит архива, продолжение, новые посты")
    cd.POSTS_PER_CHANNEL, cd.PAGES_PER_RUN = 50, 1
    cd.sync_channel("chan_a", fetch)
    check("первый прогон: свежая страница + одна страница архива", db.count_channel_posts("chan_a") == 40
          and not db.get_channel_state("chan_a")["backfill_done"])
    cd.sync_channel("chan_a", fetch)
    check("второй прогон дочитал до лимита", db.count_channel_posts("chan_a") == 60
          and db.get_channel_state("chan_a")["oldest_id"] == 61)
    cd.sync_channel("chan_a", fetch)
    check("лимит достигнут — архив больше не читается", db.count_channel_posts("chan_a") == 60
          and db.get_channel_state("chan_a")["backfill_done"])
    chans["chan_a"].n = 125
    cd.sync_channel("chan_a", fetch)
    check("новые посты сверху подтянулись", db.get_channel_newest_id("chan_a") == 125)
    cd.PAGES_PER_RUN = 15
    cd.sync_channel("chan_b", fetch)
    check("короткий канал прочитан до первого поста", db.count_channel_posts("chan_b") == 30
          and db.get_channel_state("chan_b")["backfill_done"])
    skipped = lambda ch: db.get_connection().execute(
        "SELECT COUNT(*) FROM channel_posts WHERE channel = ? AND status = 'skip'", (ch,)).fetchone()[0]
    check("опросы и подписи к картинкам — сразу пропущены", skipped("chan_b") == 6)

    print("\n3. Разметка: пост → пункты")

    def items_for(post):
        i = post["post_id"] % 4
        a = post["channel"] == "chan_a"
        if i == 0:
            return [{"kind": "phrase", "es": "¿Qué más da?" if a else "qué más da", "ru": "всё равно", "level": "A2", "score": 5 if a else 4}]
        if i == 1:
            return [{"kind": "grammar", "es": "ser / estar" if a else "por / para", "ru": "x",
                     "level": "B2" if post["post_id"] % 10 == 1 else "A2", "score": 5 if a else 4}]
        if i == 2:
            es = "el vuelo, el asiento, la azafata" if a else "la cocina, el horno, la sartén"
            return [{"kind": "words", "es": es, "ru": "x", "level": "A2", "score": 5 if a else 4}]
        # a list post: three phrases (one repeated by the model), each its own item
        p = post["post_id"]
        return [{"kind": "phrase", "es": f"frase {p} {n}", "ru": "x", "level": "B1", "score": 4} for n in ("a", "b", "c", "a")]

    def label_reply(kw):
        out = []
        for line in kw["messages"][0]["content"].split("\n"):
            if line.startswith("=== id "):
                pid = int(line.split()[2])
                post = db.get_channel_post(pid)
                if post["post_id"] % 10 == 9:
                    continue  # the model skipped this one
                out.append({"id": pid, "items": items_for(post)})
        return {"posts": out}

    fake_claude(label_reply)
    n = cd.label_pending()
    check("размечены все с текстом", n > 0 and not db.get_unlabeled_channel_posts(10))
    q = lambda sql, *a: db.get_connection().execute(sql, a).fetchall()
    list_post = q("SELECT id FROM channel_posts WHERE channel = 'chan_a' AND post_id = 103")[0][0]
    check("пост-список стал тремя пунктами (повтор внутри поста отброшен)",
          [r[0] for r in q("SELECT es FROM channel_items WHERE post_row = ? ORDER BY id", list_post)]
          == ["frase 103 a", "frase 103 b", "frase 103 c"])
    check("пост, который модель не вернула, пропущен (не размечается по кругу)",
          q("SELECT status FROM channel_posts WHERE channel = 'chan_a' AND post_id = 109")[0][0] == "skip")
    check("ключ нормализован: «¿Qué más da?» = «qué más da»", cd.make_key("¿Qué más da?") == "qué más da"
          and cd.make_key("El aeropuerto") == "aeropuerto")
    check("у тройки слов — три ключа", cd.item_parts({"kind": "words", "key": "x", "es": "el vuelo, el asiento, la azafata"})
          == ["vuelo", "asiento", "azafata"])

    print("\n4. Выбор на день: чередование, «уже знаю», дубли, пауза между пунктами одного поста")
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    db.add_word(OWNER_ID, "el vuelo", "рейс", "существительное", "A2", ["a — б"])  # one of three — the group still goes
    for w in ("cocina", "horno", "sartén"):
        db.set_catalog_known(OWNER_ID, w, w, True)  # all three — the group doesn't
    composed = {"title": "T", "gist": "G", "examples": [{"es": "E", "ru": "R"}], "add_es": "", "chunk": ""}

    day1 = cd.pick(OWNER_ID, now)
    check("первым — фраза с лучшей оценкой", day1["kind"] == "phrase" and day1["key"] == "qué más da")
    db.mark_channel_item_shown(day1["id"], day1["key"], composed)
    check("тот же ключ в других постах и каналах — дубли",
          q("SELECT COUNT(*) FROM channel_items WHERE key = 'qué más da' AND status = 'ready'")[0][0] == 0
          and q("SELECT COUNT(*) FROM channel_items WHERE key = 'qué más da' AND status = 'dup'")[0][0] > 0)
    day2 = cd.pick(OWNER_ID, now)
    check("на следующий день — грамматика, не уровня B2", day2["kind"] == "grammar" and day2["level"] == "A2")
    db.mark_channel_item_shown(day2["id"], day2["key"], composed)
    day3 = cd.pick(OWNER_ID, now)
    check("потом — тройка слов; «el vuelo» в словаре не мешает, а кухня (все три знает) не идёт",
          day3["kind"] == "words" and "vuelo" in day3["es"])
    known = cd._known_keys(OWNER_ID)
    kitchen = {"kind": "words", "key": "x", "es": "la cocina, el horno, la sartén"}
    check("…кухня отсеяна, потому что все три слова уже знакомы", all(k in known for k in cd.item_parts(kitchen))
          and not all(k in known for k in cd.item_parts(day3)))
    db.mark_channel_item_shown(day3["id"], day3["key"], composed)

    cd.KIND_ORDER = ("phrase",)
    # force the list post's first phrase to be today's
    db.get_connection().execute("UPDATE channel_items SET score = 5 WHERE post_row = ?", (list_post,)).connection.commit()
    day4 = cd.pick(OWNER_ID, now)
    check("фраза из поста-списка — по одной", day4["post_row"] == list_post and day4["es"] == "frase 103 a")
    db.mark_channel_item_shown(day4["id"], day4["key"], composed)
    picks = [cd.pick(OWNER_ID, now + timedelta(days=1)) for _ in range(15)]
    check("назавтра — не из того же поста", all(p["post_row"] != list_post for p in picks))
    later = cd.pick(OWNER_ID, now + timedelta(days=cd.POST_GAP_DAYS + 1))
    check(f"через {cd.POST_GAP_DAYS + 1} дней — следующая фраза этого поста", later["post_row"] == list_post and later["es"] == "frase 103 b")
    cd.KIND_ORDER = ("phrase", "grammar", "words")

    print("\n5. Сообщение и кнопки")

    def compose_reply(kw):
        content = kw["messages"][0]["content"]
        check("в пересказ ушёл сам пункт, текст поста и канал", "Пункт (фраза): frase 103 b" in content and "Пост из канала @chan_a" in content)
        return {"title": "Me da pereza", "gist": "Мне лень <когда>.",
                "examples": [{"es": "Me da pereza cocinar.", "ru": "Мне лень готовить."}] * 4,
                "add_es": "la pereza", "chunk": "me da pereza"}

    fake_claude(compose_reply)
    fake = FakeBot()
    real_pick = cd.pick
    cd.pick = lambda user_id: later
    sent = asyncio.run(bot._send_channel_digest(fake, OWNER_ID))
    cd.pick = real_pick
    text, markup, kw = fake.sent[-1]
    check("сообщение ушло", sent and "Me da pereza" in text)
    check("HTML экранирован, ссылка на пост", "&lt;когда&gt;" in text and 'href="https://t.me/chan_a/103"' in text)
    check("не больше трёх примеров", text.count("•") == 3)
    labels = [row[0].text for row in markup.inline_keyboard]
    check("кнопки: добавить, сделать оборотом, знаю", labels == ["➕ la pereza", "🎯 Сделать оборотом: me da pereza", "✓ Знаю"])
    shown = db.get_channel_item(later["id"])
    check("пункт помечен показанным, пересказ сохранён", shown["status"] == "shown" and shown["digest"]["chunk"] == "me da pereza")

    replies = []
    message = SimpleNamespace(reply_markup=markup)

    async def reply_text(text, **kw):
        replies.append(text)
    message.reply_text = reply_text
    edited = []

    async def edit_markup(reply_markup=None):
        edited.append(reply_markup)

    def query(data, user=OWNER_ID):
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user), message=message,
                               edit_message_reply_markup=edit_markup)

    sid = str(shown["id"])
    db.create_chunk(OWNER_ID, "para mí", "", "", "")
    asyncio.run(bot._handle_channel_button(query(f"chchunk:{sid}"), "chchunk", ["chchunk", sid]))
    check("оборот стал текущим, прошлый снят", db.get_active_chunk(OWNER_ID)["chunk"] == "me da pereza"
          and "para mí" in replies[-1])
    check("нажатая кнопка убрана", len(edited[-1].inline_keyboard) == 2)

    added = []

    async def fake_add(msg, user_id, word):
        added.append(word)
    bot._add_word = fake_add
    asyncio.run(bot._handle_channel_button(query(f"chadd:{sid}"), "chadd", ["chadd", sid]))
    check("«➕» идёт обычным путём добавления слова", added == ["la pereza"])

    asyncio.run(bot._handle_channel_button(query(f"chknown:{sid}"), "chknown", ["chknown", sid]))
    check("«знаю» закрывает ключ", db.get_channel_item(shown["id"])["status"] == "known" and shown["key"] in db.get_channel_shown_keys())

    added.clear()
    asyncio.run(bot._handle_channel_button(query(f"chadd:{sid}", user=1), "chadd", ["chadd", sid]))
    check("не владельцу кнопки ничего не делают", added == [])

    stats = {row[0]: row[1:] for row in db.get_channel_stats()}
    check("статистика: постов / полезных / в очереди / показано", stats["chan_a"][0] == 65 and stats["chan_a"][3] == 4  # 5 shown, one of them then «знаю»
          and stats["chan_a"][2] > 0)

    print("\n6. Когда всё показано — тишина")
    db.get_channel_candidates = lambda *a, **kw: []
    fake = FakeBot()
    check("нечего прислать — сообщения нет", asyncio.run(bot._send_channel_digest(fake, OWNER_ID)) is False and not fake.sent)

    print("\n" + ("✅ Все проверки прошли" if not failures else f"❌ Упало: {len(failures)}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
