"""Top-3000 word catalog — owner-only Telegram Mini App (static/catalog.html).

The whole A1-A2 list (word, translation, topic) in one scrollable screen: what's already
in the dictionary is marked, and «добавить» runs the normal add path (Claude writes the
card, the weekly limit applies) so a word added here behaves like one typed to the bot.

Source of the list: tools/parse_top3000.py turns Ольга's .docx into static/top3000.json.

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

_ARTICLES = ("el ", "la ", "los ", "las ", "un ", "una ", "unos ", "unas ")

_add_word = None  # injected by register(); bot.py owns the lock + weekly limit


def normalize(phrase: str) -> str:
    """Key for "is this already in the dictionary?". The catalog lists bare words
    ("casa"), the bot stores nouns with their article ("la casa") — strip it so the two
    forms meet. Everything else (accents, case) is compared as written.

    Two catalog entries can collapse to one key — «¿verdad?» and «verdad» are the only
    such pair in the current list, and adding either just ticks both rows."""
    text = (phrase or "").strip().lower().strip("¿?¡!.,;:")
    for article in _ARTICLES:
        if text.startswith(article):
            return text[len(article):]
    return text


def load_words() -> list[dict]:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))["words"]
    except (OSError, ValueError, KeyError):
        logger.exception("top3000.json is missing or unreadable")
        return []


def known_keys(user_id: int) -> set[str]:
    return {normalize(row["phrase"]) for row in db.get_user_words(user_id)}


async def handle_page(request: web.Request) -> web.Response:
    return web.FileResponse(PAGE_PATH, headers={"Cache-Control": "no-store"})


async def handle_data(request: web.Request) -> web.Response:
    if voice_talk.request_owner_id(request) is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.FileResponse(DATA_PATH, headers={"Cache-Control": "no-store"})


async def handle_state(request: web.Request) -> web.Response:
    """Which catalog words the user already has — the page marks them and counts progress."""
    user_id = voice_talk.request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    known = known_keys(user_id)
    catalog = load_words()
    return web.json_response({
        "known": sorted(k for k in {normalize(w["es"]) for w in catalog} if k in known),
        "total": len(catalog),
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


def register(api: web.Application, *, add_word):
    """add_word: async (user_id, word) -> {"status": added|exists|limit|error, "phrase": …}"""
    global _add_word
    _add_word = add_word
    api.router.add_get("/catalog", handle_page)
    api.router.add_get("/catalog/data", handle_data)
    api.router.add_get("/catalog/state", handle_state)
    api.router.add_post("/catalog/add", handle_add)
