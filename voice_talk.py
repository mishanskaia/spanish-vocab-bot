"""Hands-free voice conversation — owner-only Telegram Mini App.

Built 2026-09-19 as a spike on OpenAI Realtime, rebuilt the same day as a cheaper
pipeline after the first live test (realtime came out at ~$0.06/min, ~6x her paid
speaking-bot subscription, and she said sub-second "liveliness" isn't what she needs —
no buttons is). Each turn: the page (static/talk.html) hears the end of her phrase
itself → POST /talk/turn with the audio → verbatim STT (stt_helper, same model and
prompt as the evening practice) → Claude decides "did she finish?" and writes the
reply → OpenAI TTS → audio + subtitles back to the page.

Pause handling (the hard part at A1 — she stops mid-sentence to find a word) is split
between the page (silence timing, merging an early-answered utterance, barge-in) and
here (looks_unfinished() pre-check, then Claude's "complete" flag). See CLAUDE.md
«Живой разговор голосом».

Two ways to authenticate, both owner-only:
- Telegram Mini App initData (opened from the /talk button inside Telegram);
- a signed link token (the "open in Safari" link from /talk).
"""

import asyncio
import base64
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

from aiohttp import web

import ai_helper
import db
import stt_helper

logger = logging.getLogger(__name__)

# Compared on the same turns 2026-09-19: Haiku 4.5 was fastest (~0.9 s) but made grammar
# mistakes in its own Spanish ("la playa es muy bonito", "yo también me encanta") —
# unacceptable for a tutor. Sonnet 5 was slower (1.5-4 s) and misjudged finished
# sentences as unfinished. Sonnet 4.6 got every "finished?" call right, clean grammar,
# natural recasts, ~1.8 s. Same model ai_helper uses. Override with TALK_LLM_MODEL.
TALK_LLM_MODEL = os.environ.get("TALK_LLM_MODEL", "claude-sonnet-4-6")
MAX_TURN_AUDIO_BYTES = 4 * 1024 * 1024  # ~2 min of 16 kHz WAV — the page caps an utterance well below
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
    # Priorities rewritten after the first live test (2026-09-19): v1 put the target
    # words first and said "switch word every 2-3 turns" + "recast her idea" — the
    # result was a questionnaire that jumped topics and parroted her back before
    # every question. Conversation flow now outranks the words.
    return f"""You are a friendly Spanish-speaking friend chatting with a Russian-speaking learner at level A1–A2. She is a woman.

Priorities, in this order:
1. A real, coherent conversation — like two people talking, not an interview.
2. She talks more than you.
3. Chances for her to use the target words below. Sacrifice this whenever it would break the flow.

How a turn sounds:
- First a genuine reaction to what she said: surprise, agree, joke, share a short opinion or a tiny story of your own (you may invent one, like a friend would). 1–2 short sentences.
- Then, at the end, one question — a follow-up about what she just said.
- Do NOT start by repeating what she said. Never echo her whole sentence back.
- Simple A1–A2 words, short sentences (up to ~12 words). Keep your turn to 2–3 sentences.

Keeping the thread:
- Stay on one topic and go deeper with follow-up questions (why, how, with whom, what happened next).
- Change topic only through a natural bridge from what was just said ("Hablando de viajes…"), never abruptly.
- At the start, pick an everyday topic that several target words fit into, and open with it.

Target words:
- Use one only when it fits the current topic naturally: ask something whose natural answer needs it.
- Don't say a target word yourself before she has used it, don't list them, don't mention that they are targets.
- It's fine if only 2–3 of them come up in the whole conversation.

Mistakes:
- Don't explain grammar and don't stop the conversation to correct.
- Only when she made a real mistake: slip the correct form into your reaction in passing (e.g. "¡Ah, fuiste al cine! ¿Y qué viste?"). If there was no mistake, don't repeat anything.
- Never "correct" feminine forms she uses about herself.
- If she uses a Russian word because she doesn't know the Spanish one, give the Spanish word once, briefly, and continue.
- If she seems lost or asks (even in Russian) to repeat, say it again more simply.

Start: greet her briefly and open your chosen topic with a question.

Target words (Spanish — Russian meaning):
{word_lines or "- (no words yet — just have a simple everyday conversation)"}"""


TURN_FORMAT = """

How her messages reach you:
Each user message is a live speech-to-text transcript of what she just said (verbatim — her mistakes and Russian words are kept on purpose). The page decides she has finished when she goes quiet, but at A1 she often pauses mid-sentence to search for a word.

Answer with JSON:
- "complete": judge ONLY by how her message ends. It is false only when the last words cannot end a sentence — it stops on a conjunction, preposition, article, possessive or filler ("y", "pero", "que", "de", "con", "la", "mi", "eh…"), or breaks off mid-phrase ("Yo quiero comprar", "Mañana voy a"). Then "reply" must be "" and the page keeps listening. Grammar mistakes, Russian words, very short answers, or not answering your question do NOT make it incomplete. When in doubt, it is complete.
- "complete": true → "reply" is your next spoken turn: plain Spanish text only (it is read aloud by text-to-speech — no emoji, no markdown, no stage directions, no translations in brackets).
- If her message ends with [LARGA PAUSA], she got stuck: "complete" must be true. Help gently — offer the word she seems to be looking for, or ask your question again more simply.
- If her message ends with [TERMINÉ], she tapped "I'm done": "complete" must be true."""

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "complete": {"type": "boolean"},
        "reply": {"type": "string"},
    },
    "required": ["complete", "reply"],
    "additionalProperties": False,
}

# Last words after which a sentence can't be finished — cheap pre-check before Claude,
# so an obvious "Yo trabajo en la…" pause costs no LLM call and no latency.
_UNFINISHED_ENDINGS = {
    "eh", "em", "ehm", "mm", "mmm", "este", "esta", "pues", "bueno",
    "y", "o", "pero", "que", "porque", "cuando", "donde", "si", "como",
    "de", "del", "a", "al", "en", "con", "por", "para", "sin", "sobre", "entre", "hasta",
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    "mi", "mis", "tu", "tus", "su", "sus", "nuestro", "nuestra", "muy", "más", "menos",
}
_WORD_RE = re.compile(r"[a-záéíóúüñа-яё]+", re.IGNORECASE)


def looks_unfinished(text: str) -> bool:
    stripped = text.strip()
    if stripped.endswith(("...", "…", ",")):
        return True
    words = _WORD_RE.findall(stripped.lower())
    return bool(words) and words[-1] in _UNFINISHED_ENDINGS


def _history_to_messages(history, user_text: str | None) -> list[dict]:
    # The API wants a user message first; the opening turn has nothing from her yet.
    messages = [{"role": "user", "content": "[Empieza la conversación.]"}]
    for turn in history:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        text = str(turn.get("text", "")).strip()
        if text:
            messages.append({"role": role, "content": text})
    if user_text is not None:
        messages.append({"role": "user", "content": user_text})
    return messages


def claude_turn(target_words, history, user_text: str | None) -> tuple[dict, dict]:
    """Sync (blocking) — call through asyncio.to_thread."""
    response = ai_helper.client.messages.create(
        model=TALK_LLM_MODEL,
        max_tokens=600,
        system=build_instructions(target_words) + TURN_FORMAT,
        messages=_history_to_messages(history, user_text),
        output_config={"format": {"type": "json_schema", "schema": TURN_SCHEMA}},
        # the system prompt and history repeat on every turn — cache reads are 0.1x
        cache_control={"type": "ephemeral"},
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)
    # input_tokens excludes cached tokens — reads and writes are reported separately
    usage = {
        "llm_in": response.usage.input_tokens,
        "llm_cache_read": response.usage.cache_read_input_tokens or 0,
        "llm_cache_write": response.usage.cache_creation_input_tokens or 0,
        "llm_out": response.usage.output_tokens,
    }
    return data, usage


async def _speak(reply: str, usage: dict, timings: dict) -> str:
    t = time.monotonic()
    audio = await asyncio.to_thread(stt_helper.synthesize, reply)
    timings["tts_ms"] = round((time.monotonic() - t) * 1000)
    usage["tts_chars"] = len(reply)
    return base64.b64encode(audio).decode()


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_page(request: web.Request) -> web.Response:
    return web.FileResponse(PAGE_PATH, headers={"Cache-Control": "no-store"})


def _voice_error(e: Exception) -> web.Response:
    logger.exception("talk turn failed")
    return web.json_response({"error": f"{type(e).__name__}: {e}"[:300]}, status=502)


async def handle_start(request: web.Request) -> web.Response:
    """Opens a session: picks the target words, and the bot says the first line."""
    user_id = _request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not is_enabled():
        return web.json_response({"error": "OPENAI_API_KEY не задан"}, status=503)

    target_words = db.get_voice_target_words(user_id, TARGET_WORD_COUNT)
    timings, usage = {}, {}
    try:
        t = time.monotonic()
        data, usage = await asyncio.to_thread(claude_turn, target_words, [], None)
        timings["llm_ms"] = round((time.monotonic() - t) * 1000)
        reply = data.get("reply", "").strip() or "¡Hola! ¿Qué tal tu día?"
        audio_b64 = await _speak(reply, usage, timings)
    except Exception as e:
        return _voice_error(e)

    session_id = db.create_voice_session(user_id, target_words)
    return web.json_response({
        "session_id": session_id,
        "target_count": len(target_words),
        "reply": reply,
        "audio": audio_b64,
        "usage": usage,
        "timings": timings,
    })


async def handle_turn(request: web.Request) -> web.Response:
    """One utterance from her: audio → verbatim STT → "finished?" check → Claude → TTS.

    mode: "auto" (the page heard a pause), "stuck" (a long pause — she's searching for
    a word, so answer and help), "done" (she tapped: answer no matter what).
    prefix: text of an earlier piece of the same utterance, when the bot answered too
    early and she carried on talking — the page dropped that answer and merges here.
    """
    user_id = _request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        form = await request.post()
        session_id = int(form["session_id"])
        history = json.loads(form.get("history") or "[]")
        prefix = str(form.get("prefix") or "").strip()
        mode = str(form.get("mode") or "auto")
        audio = form["audio"].file.read()
    except Exception:
        return web.json_response({"error": "bad form"}, status=400)
    if len(audio) > MAX_TURN_AUDIO_BYTES:
        return web.json_response({"error": "audio too long"}, status=413)

    session = db.get_voice_session(session_id)
    if session is None or session["user_id"] != user_id:
        return web.json_response({"error": "not found"}, status=404)

    timings = {}
    # 16 kHz mono 16-bit WAV from the page: 32000 bytes per second after the 44-byte header
    usage = {"stt_seconds": max(0, len(audio) - 44) / 32000}
    try:
        t = time.monotonic()
        heard = await asyncio.to_thread(stt_helper.transcribe, audio, "turn.wav")
        timings["stt_ms"] = round((time.monotonic() - t) * 1000)
    except Exception as e:
        return _voice_error(e)

    user_text = " ".join(p for p in (prefix, heard.strip()) if p)
    if not user_text:
        return web.json_response({"status": "empty", "usage": usage, "timings": timings})
    if mode == "auto" and looks_unfinished(user_text):
        return web.json_response({"status": "wait", "user_text": user_text, "usage": usage, "timings": timings})

    marker = {"stuck": " [LARGA PAUSA]", "done": " [TERMINÉ]"}.get(mode, "")
    try:
        t = time.monotonic()
        data, llm_usage = await asyncio.to_thread(
            claude_turn, session["target_words"], history, user_text + marker
        )
        timings["llm_ms"] = round((time.monotonic() - t) * 1000)
        usage.update(llm_usage)
        reply = (data.get("reply") or "").strip()
        if mode == "auto" and (not data.get("complete") or not reply):
            return web.json_response({"status": "wait", "user_text": user_text, "usage": usage, "timings": timings})
        if not reply:
            reply = "Perdona, ¿puedes repetirlo?"
        audio_b64 = await _speak(reply, usage, timings)
    except Exception as e:
        return _voice_error(e)

    return web.json_response({
        "status": "reply",
        "user_text": user_text,
        "reply": reply,
        "audio": audio_b64,
        "usage": usage,
        "timings": timings,
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


# Prices as of 2026-09-19, for the rough estimate in the summary only. TTS is billed per
# audio token; ~$0.015 per minute of speech is OpenAI's own estimate for gpt-4o-mini-tts.
PRICE_STT_PER_MIN = 0.003    # gpt-4o-mini-transcribe
PRICE_LLM_IN = 3.0           # claude-sonnet-4-6, $ per 1M input tokens (cache reads 0.1x, writes 1.25x)
PRICE_LLM_OUT = 15.0
PRICE_TTS_PER_MIN = 0.015


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
    stt = usage.get("stt_seconds", 0) / 60 * PRICE_STT_PER_MIN
    llm = (
        usage.get("llm_in", 0) * PRICE_LLM_IN
        + usage.get("llm_cache_read", 0) * PRICE_LLM_IN * 0.1
        + usage.get("llm_cache_write", 0) * PRICE_LLM_IN * 1.25
        + usage.get("llm_out", 0) * PRICE_LLM_OUT
    ) / 1e6
    tts = usage.get("tts_seconds", 0) / 60 * PRICE_TTS_PER_MIN

    lines = [
        f"🎙 <b>Разговор сохранён</b> — {minutes} мин, твоих реплик: {user_turns}.",
        "",
        "✅ Прозвучали у тебя: " + (html.escape(", ".join(used)) if used else "—"),
        "▫️ Не прозвучали: " + (html.escape(", ".join(unused)) if unused else "—"),
        "<i>(точное совпадение формы — спряжённые глаголы пока не ловятся)</i>",
        "",
        f"💸 ≈ ${stt + llm + tts:.3f}: распознавание ${stt:.3f}, Claude ${llm:.3f}, озвучка ${tts:.3f} "
        "<i>(оценка по прайсу на 2026-09-19)</i>",
    ]
    return "\n".join(lines)


def register(api: web.Application, *, bot, bot_token: str, owner_id: int):
    global _bot, _bot_token, _owner_id
    _bot, _bot_token, _owner_id = bot, bot_token, owner_id
    api.router.add_get("/talk", handle_page)
    api.router.add_post("/talk/start", handle_start)
    api.router.add_post("/talk/turn", handle_turn)
    api.router.add_post("/talk/transcript", handle_transcript)
