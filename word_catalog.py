"""Top-3000 word catalog — owner-only Telegram Mini App (static/catalog.html).

The whole A1-A2 list (word, translation, topic) in one scrollable screen: what's already
in the dictionary is marked, and «добавить» runs the normal add path (Claude writes the
card, the weekly limit applies) so a word added here behaves like one typed to the bot.

Source of the list: tools/parse_top3000.py turns Ольга's .docx into static/top3000.json.

A word she already knows but doesn't want to drill can be marked «знаю» instead
(/catalog/mark): no Claude call, no card, no weekly limit — just a row in catalog_known,
so it leaves «Новые» and counts towards progress. Tapping again unmarks it.

Auth is voice_talk's — same initData / signed-link check, owner only.
"""

import json
import logging
from pathlib import Path

from aiohttp import web

import db
import voice_talk

logger = logging.getLogger(__name__)

PAGE_PATH = Path(__file__).parent / "static" / "catalog.html"
DATA_PATH = Path(__file__).parent / "static" / "top3000.json"
PHRASES_PATH = Path(__file__).parent / "static" / "phrases.json"

_ARTICLES = ("el ", "la ", "los ", "las ", "un ", "una ", "unos ", "unas ")

_add_word = None  # injected by register(); bot.py owns the lock + weekly limit


def normalize(phrase: str) -> str:
    """Key for "is this already in the dictionary?". The catalog lists bare words
    ("casa"), the bot stores nouns with their article ("la casa") — strip it so the two
    forms meet. Everything else (accents, case) is compared as written.

    Two catalog entries can collapse to one key — «¿verdad?» and «verdad» are the only
    such pair in the current list, and adding either just ticks both rows."""
    # The ellipsis matters: chunks are listed as «tengo que…», the bot stores «tengo que».
    text = (phrase or "").strip().lower().strip("¿?¡!.,;:… ")
    for article in _ARTICLES:
        if text.startswith(article):
            return text[len(article):]
    return text


def _load(path: Path, key: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("%s is missing or unreadable", path.name)
        return {"categories": [], key: []}


def load_words() -> list[dict]:
    return _load(DATA_PATH, "words").get("words", [])


def load_phrases() -> list[dict]:
    """Conversational chunks (tools/build_phrases.py) — the second tab of the same page."""
    return _load(PHRASES_PATH, "phrases").get("phrases", [])


def known_keys(user_id: int) -> set[str]:
    return {normalize(row["phrase"]) for row in db.get_user_words(user_id)}


async def handle_page(request: web.Request) -> web.Response:
    return web.FileResponse(PAGE_PATH, headers={"Cache-Control": "no-store"})


async def handle_data(request: web.Request) -> web.Response:
    """Both tabs in one response: the 3000 words and the conversational chunks."""
    if voice_talk.request_owner_id(request) is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    words, phrases = _load(DATA_PATH, "words"), _load(PHRASES_PATH, "phrases")
    return web.json_response({
        "words": words.get("words", []),
        "categories": words.get("categories", []),
        "phrases": phrases.get("phrases", []),
        "phraseCategories": phrases.get("categories", []),
    })


async def handle_state(request: web.Request) -> web.Response:
    """Which catalog entries the user already has — the page ticks them and counts progress."""
    user_id = voice_talk.request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    known = known_keys(user_id)
    words, phrases = load_words(), load_phrases()
    catalog_keys = {normalize(item["es"]) for item in words + phrases}
    # A word both marked and later added counts as "in the dictionary" — the card wins.
    marked = db.get_catalog_known(user_id) - known
    return web.json_response({
        "known": sorted(k for k in catalog_keys if k in known),
        "marked": sorted(k for k in catalog_keys if k in marked),
        "total": len(words),
        "totalPhrases": len(phrases),
    })


async def handle_add(request: web.Request) -> web.Response:
    """One word from the list → the same path as typing it to the bot."""
    user_id = voice_talk.request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        payload = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    word = (payload.get("es") or "").strip()
    if not word:
        return web.json_response({"error": "es is required"}, status=400)
    if _add_word is None:
        return web.json_response({"error": "add is not wired"}, status=503)

    result = await _add_word(user_id, word)
    # phrase — Claude's canonical form ("casa" → "la casa"); the page keys its ✓ off
    # normalize() of both, so an article added on the way doesn't lose the match.
    return web.json_response(result)


async def handle_mark(request: web.Request) -> web.Response:
    """«Знаю» on a catalog entry: {"es": "casa", "known": true|false}. No Claude, no card."""
    user_id = voice_talk.request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        payload = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON body"}, status=400)

    word = (payload.get("es") or "").strip()
    key = normalize(word)
    if not key:
        return web.json_response({"error": "es is required"}, status=400)
    known = bool(payload.get("known", True))
    db.set_catalog_known(user_id, key, word, known)
    return web.json_response({"status": "marked" if known else "unmarked", "key": key})


def register(api: web.Application, *, add_word):
    """add_word: async (user_id, word) -> {"status": added|exists|limit|error, "phrase": …}"""
    global _add_word
    _add_word = add_word
    api.router.add_get("/catalog", handle_page)
    api.router.add_get("/catalog/data", handle_data)
    api.router.add_get("/catalog/state", handle_state)
    api.router.add_post("/catalog/add", handle_add)
    api.router.add_post("/catalog/mark", handle_mark)
