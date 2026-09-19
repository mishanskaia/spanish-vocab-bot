"""Real-time voice conversation — owner-only Telegram Mini App (spike, 2026-09-19).

The page (static/talk.html) talks to OpenAI Realtime over WebRTC straight from the
browser; this server only (1) checks the caller is the owner, (2) opens the call —
the browser's SDP offer goes through here to /v1/realtime/calls with the real API
key, so no key of any kind ever reaches the browser — and (3) stores the transcript
the page sends back after every turn, plus a short summary to the owner's chat.

Two ways to authenticate, both owner-only:
- Telegram Mini App initData (opened from the /talk button inside Telegram);
- a signed link token (the "open in Safari" link from /talk) — the fallback if the
  mic or screen wake lock doesn't work inside Telegram's iOS WebView.
"""

import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import web

import db
from stt_helper import STT_MODEL, VERBATIM_PROMPT

logger = logging.getLogger(__name__)

REALTIME_MODEL = os.environ.get("REALTIME_MODEL", "gpt-realtime-2.1")
REALTIME_VOICE = "marin"
REALTIME_SPEED = 0.9  # a bit slower than default; subtitles carry the rest
TARGET_WORD_COUNT = 10
LINK_TOKEN_TTL_SECONDS = 12 * 3600
INIT_DATA_MAX_AGE_SECONDS = 24 * 3600
PAGE_PATH = Path(__file__).parent / "static" / "talk.html"

_bot = None
_bot_token = ""
_owner_id = 0


def is_enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def public_base_url() -> str | None:
    explicit = os.environ.get("PUBLIC_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")  # set by Railway once a domain is generated
    return f"https://{domain}" if domain else None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _init_data_user_id(init_data: str) -> int | None:
    """Telegram Mini App initData check (core.telegram.org/bots/webapps,
    "Validating data received via the Mini App")."""
    if not init_data:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", None)
    if not received_hash:
        return None
    secret = hmac.new(b"WebAppData", _bot_token.encode(), hashlib.sha256).digest()

    def matches(items: dict) -> bool:
        check_string = "\n".join(f"{k}={v}" for k, v in sorted(items.items()))
        calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(calc, received_hash)

    # Newer clients also send `signature` (for third-party ed25519 checks); the docs
    # keep it in the HMAC check string, but accept either way rather than lock out on it.
    without_signature = {k: v for k, v in fields.items() if k != "signature"}
    if not (matches(fields) or matches(without_signature)):
        return None
    try:
        if time.time() - int(fields.get("auth_date", "0")) > INIT_DATA_MAX_AGE_SECONDS:
            return None
        return int(json.loads(fields["user"])["id"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _link_key() -> bytes:
    return hmac.new(_bot_token.encode(), b"talk-link-v1", hashlib.sha256).digest()


def make_link_token(user_id: int) -> str:
    expires = int(time.time()) + LINK_TOKEN_TTL_SECONDS
    payload = f"{user_id}.{expires}"
    sig = hmac.new(_link_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def _link_token_user_id(token: str) -> int | None:
    try:
        user_id, expires, sig = token.split(".")
        payload = f"{user_id}.{expires}"
        calc = hmac.new(_link_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(calc, sig) or int(expires) < time.time():
            return None
        return int(user_id)
    except (ValueError, AttributeError):
        return None


def _request_owner_id(request: web.Request) -> int | None:
    user_id = _init_data_user_id(request.headers.get("X-Tg-Init-Data", "")) or _link_token_user_id(
        request.headers.get("X-Talk-Token", "")
    )
    if user_id and _owner_id and user_id == _owner_id:
        return user_id
    return None


# ---------------------------------------------------------------------------
# Conversation setup
# ---------------------------------------------------------------------------

def build_instructions(target_words) -> str:
    word_lines = "\n".join(f"- {w['phrase']} — {w['meaning']}" for w in target_words)
    return f"""You are a warm, patient Spanish conversation partner for a Russian-speaking learner at level A1–A2. She is a woman.

How you speak:
- Only Spanish. Simple A1–A2 vocabulary and grammar, short sentences (up to ~12 words).
- Speak slowly and clearly. One question per turn. Keep your turns short — she should talk more than you.
- React briefly and naturally to what she said, then ask the next question, so the conversation keeps flowing.

Target words — this is the point of the conversation:
- Steer the talk so she naturally NEEDS these words: ask questions whose natural answer uses one of them.
- Never say a target word yourself before she has used it, and never list them or tell her they are targets.
- Switch to another target word every 2–3 turns; aim to give her a chance with as many as possible.

Mistakes:
- Do not explain grammar and do not stop the conversation to correct her.
- When she makes a mistake, naturally repeat her idea correctly inside your reply (a recast), then continue.
- Never "correct" feminine forms she uses about herself.
- If she uses a Russian word because she doesn't know the Spanish one, give the Spanish word once, briefly, and continue.
- If she seems lost or asks (even in Russian) to slow down, say it again more simply.

Start: greet her briefly and ask your first question.

Target words (Spanish — Russian meaning):
{word_lines or "- (no words yet — just have a simple everyday conversation)"}"""


def build_session_config(target_words) -> dict:
    return {
        "type": "realtime",
        "model": REALTIME_MODEL,
        "instructions": build_instructions(target_words),
        "audio": {
            "input": {
                # Same model + verbatim prompt as the voice-message practice: the
                # realtime default would happily "fix" her mistakes in the subtitles.
                "transcription": {"model": STT_MODEL, "language": "es", "prompt": VERBATIM_PROMPT},
                # A1 speakers pause mid-sentence to search for words — low eagerness
                # waits longer before deciding she's done talking.
                "turn_detection": {"type": "semantic_vad", "eagerness": "low"},
                "noise_reduction": {"type": "far_field"},  # phone lying on the table
            },
            "output": {"voice": REALTIME_VOICE, "speed": REALTIME_SPEED},
        },
    }


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_page(request: web.Request) -> web.Response:
    return web.FileResponse(PAGE_PATH, headers={"Cache-Control": "no-store"})


async def handle_call(request: web.Request) -> web.Response:
    user_id = _request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not is_enabled():
        return web.json_response({"error": "OPENAI_API_KEY не задан"}, status=503)
    try:
        body = await request.json()
        offer_sdp = body["sdp"]
    except Exception:
        return web.json_response({"error": "sdp is required"}, status=400)

    target_words = db.get_voice_target_words(user_id, TARGET_WORD_COUNT)
    session = build_session_config(target_words)

    with aiohttp.MultipartWriter("form-data") as form:
        part = form.append(offer_sdp, {"Content-Type": "application/sdp"})
        part.set_content_disposition("form-data", name="sdp")
        part = form.append(json.dumps(session), {"Content-Type": "application/json"})
        part.set_content_disposition("form-data", name="session")

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            async with http.post(
                "https://api.openai.com/v1/realtime/calls",
                data=form,
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
            ) as resp:
                answer = await resp.text()
                if resp.status >= 300:
                    logger.error("realtime call failed: %s %s", resp.status, answer[:500])
                    return web.json_response(
                        {"error": f"OpenAI {resp.status}: {answer[:300]}"}, status=502
                    )
    except aiohttp.ClientError as e:
        logger.exception("realtime call failed")
        return web.json_response({"error": f"network: {e}"}, status=502)

    session_id = db.create_voice_session(user_id, target_words)
    return web.json_response({
        "sdp": answer,
        "session_id": session_id,
        "target_words": [w["phrase"] for w in target_words],
    })


async def handle_transcript(request: web.Request) -> web.Response:
    user_id = _request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        session_id = int(body["session_id"])
        transcript = [
            {"role": str(t.get("role", "")), "text": str(t.get("text", ""))}
            for t in body.get("transcript", [])
            if isinstance(t, dict)
        ]
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        ended = bool(body.get("ended"))
    except Exception:
        return web.json_response({"error": "bad body"}, status=400)

    session = db.get_voice_session(session_id)
    if session is None or session["user_id"] != user_id:
        return web.json_response({"error": "not found"}, status=404)
    already_done = session["status"] == "done"
    db.save_voice_transcript(session_id, transcript, usage, ended)

    if ended and not already_done and _bot is not None:
        try:
            await _bot.send_message(
                user_id,
                build_summary(db.get_voice_session(session_id)),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("failed to send voice session summary")
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

_ARTICLE_RE = re.compile(r"^(el|la|los|las|un|una|unos|unas)\s+", re.IGNORECASE)


def _word_used(phrase: str, user_text: str) -> bool:
    """Exact-form match only (like _make_blank for cards) — a conjugated verb won't
    count. Good enough for the spike; the real review will be Claude over the transcript."""
    core = _ARTICLE_RE.sub("", phrase.strip().lower())
    return bool(core) and re.search(rf"(?<!\w){re.escape(core)}(?!\w)", user_text) is not None


def build_summary(session: dict) -> str:
    transcript = session["transcript"]
    user_text = " ".join(t["text"] for t in transcript if t["role"] == "user").lower()
    user_turns = sum(1 for t in transcript if t["role"] == "user")
    try:
        started = datetime.fromisoformat(session["started_at"])
        minutes = max(1, round((datetime.now(timezone.utc) - started).total_seconds() / 60))
    except (TypeError, ValueError):
        minutes = "?"

    targets = [w["phrase"] for w in session["target_words"]]
    used = [p for p in targets if _word_used(p, user_text)]
    unused = [p for p in targets if p not in used]

    usage = session["usage"] or {}
    inp = usage.get("input_token_details", {}) or {}
    out = usage.get("output_token_details", {}) or {}

    lines = [
        f"🎙 <b>Разговор сохранён</b> — {minutes} мин, твоих реплик: {user_turns}.",
        "",
        "✅ Прозвучали у тебя: " + (html.escape(", ".join(used)) if used else "—"),
        "▫️ Не прозвучали: " + (html.escape(", ".join(unused)) if unused else "—"),
        "<i>(точное совпадение формы — спряжённые глаголы пока не ловятся)</i>",
        "",
        "Токены (для подсчёта цены): "
        f"вход — аудио {inp.get('audio_tokens', '?')}, текст {inp.get('text_tokens', '?')}, "
        f"кэш {inp.get('cached_tokens', '?')}; "
        f"выход — аудио {out.get('audio_tokens', '?')}, текст {out.get('text_tokens', '?')}.",
    ]
    return "\n".join(lines)


def register(api: web.Application, *, bot, bot_token: str, owner_id: int):
    global _bot, _bot_token, _owner_id
    _bot, _bot_token, _owner_id = bot, bot_token, owner_id
    api.router.add_get("/talk", handle_page)
    api.router.add_post("/talk/call", handle_call)
    api.router.add_post("/talk/transcript", handle_transcript)
