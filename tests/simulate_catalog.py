"""
Local test harness for the top-3000 catalog Mini App (word_catalog.py).

Real routes on a real aiohttp test server against a throwaway DB; auth and the add path
are stubbed (adding for real would call Claude).

Usage:
    python tests/simulate_catalog.py
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OWNER_ID = 424242

os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["OWNER_TELEGRAM_ID"] = str(OWNER_ID)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")  # ai_helper builds its client at import
DB_FILE = os.path.join(ROOT, "tests", "_scratch_catalog.db")
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)
os.environ["DB_PATH"] = DB_FILE

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import db
import voice_talk
import word_catalog

failures = []
added_calls = []


def check(name, condition):
    print(("  ✅ " if condition else "  ❌ ") + name)
    if not condition:
        failures.append(name)


async def fake_add(user_id, word):
    added_calls.append((user_id, word))
    if word == "casa":
        return {"status": "added", "phrase": "la casa", "meaning": "дом"}
    if word == "agua":
        return {"status": "exists", "phrase": "el agua"}
    if word == "gato":
        return {"status": "limit", "phrase": "gato"}
    return {"status": "error", "phrase": word}


def seed():
    db.init_db()
    for phrase, meaning in [("el agua", "вода"), ("hablar", "говорить"), ("la playa", "пляж")]:
        db.add_word(OWNER_ID, phrase, meaning, "другое", "A1", ["a — б"])


async def main():
    seed()
    print("\n1. Данные списка")
    words = word_catalog.load_words()
    check("список загружается", len(words) > 2900)
    check("у каждого слова есть перевод и тема",
          all(w.get("es") and w.get("ru") and w.get("cat") for w in words))
    keys = [word_catalog.normalize(w["es"]) for w in words]
    collisions = {k for k in keys if keys.count(k) > 1}
    # «¿verdad?» и «verdad» — две строки списка, после нормализации один ключ: галочка
    # встанет у обеих. Единственный такой случай на 3000 слов, живём с ним.
    check("после нормализации совпадает только известная пара verdad", collisions == {"verdad"})
    check("артикль отбрасывается", word_catalog.normalize("La Casa") == "casa"
          and word_catalog.normalize("¿cuánto?") == "cuánto")

    print("\n2. Доступ")
    app = web.Application()
    word_catalog.register(app, add_word=fake_add)
    client = TestClient(TestServer(app))
    await client.start_server()

    for path in ("/catalog/data", "/catalog/state"):
        res = await client.get(path)
        check(f"{path} без авторизации → 401", res.status == 401)
    res = await client.post("/catalog/add", json={"es": "casa"})
    check("/catalog/add без авторизации → 401", res.status == 401)
    check("страница отдаётся без авторизации (авторизация внутри)",
          (await client.get("/catalog")).status == 200)
    check("Claude не звали", not added_calls)

    voice_talk.request_owner_id = lambda request: OWNER_ID  # авторизованный владелец

    print("\n3. Что уже в словаре")
    state = await (await client.get("/catalog/state")).json()
    known = set(state["known"])
    check("всего слов в ответе", state["total"] == len(words))
    check("«el agua» из базы найдено как «agua» в списке", "agua" in known)
    check("глагол из базы найден", "hablar" in known)
    check("«playa» есть и там, и там", "playa" in known)
    check("не добавленного слова в известных нет", "casa" not in known)

    print("\n4. Добавление")
    res = await client.post("/catalog/add", json={"es": "casa"})
    data = await res.json()
    check("добавление возвращает канонический вид", data == {
        "status": "added", "phrase": "la casa", "meaning": "дом"})
    check("слово ушло в общий путь добавления", added_calls[-1] == (OWNER_ID, "casa"))
    check("дубль отдаёт exists", (await (await client.post(
        "/catalog/add", json={"es": "agua"})).json())["status"] == "exists")
    check("лимит отдаёт limit", (await (await client.post(
        "/catalog/add", json={"es": "gato"})).json())["status"] == "limit")
    check("сбой Claude отдаёт error", (await (await client.post(
        "/catalog/add", json={"es": "zzz"})).json())["status"] == "error")
    res = await client.post("/catalog/add", json={})
    check("пустой запрос → 400", res.status == 400)

    print("\n5. Данные для страницы")
    payload = await (await client.get("/catalog/data")).json()
    check("отдаётся весь список с темами",
          len(payload["words"]) == len(words) and len(payload["categories"]) >= 50)

    await client.close()
    print()
    if failures:
        print(f"❌ Провалено: {len(failures)}")
        for f in failures:
            print("   -", f)
        sys.exit(1)
    print("✅ Все проверки прошли")


asyncio.run(main())
