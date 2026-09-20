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
here (looks_unfinished() pre-check, then a separate "did she finish?" check run in
parallel with the reply). See CLAUDE.md
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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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
# "Did she finish?" is a separate small call, run in parallel with the reply. Inside the big
# conversation prompt Sonnet judged "Mañana voy a la peluquería con mi hermana" unfinished in
# 2 of 5 runs of a real conversation (third live test, 2026-09-19); the focused prompt below
# got 0 of 32 wrong on both Sonnet 4.6 and Haiku 4.5. Haiku: it only returns a yes/no, so
# its Spanish doesn't matter, and at ~0.8 s a "keep listening" answer comes back faster.
TALK_CHECK_MODEL = os.environ.get("TALK_CHECK_MODEL", "claude-haiku-4-5")
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

def build_instructions(target_words, facts=()) -> str:
    word_lines = "\n".join(f"- {w['phrase']} — {w['meaning']}" for w in target_words)
    facts_block = ("\n".join(f"- {f}" for f in facts) if facts
                   else "- (todavía no sabes nada de ella — es vuestra primera conversación)")
    # Priorities rewritten after the first live test (2026-09-19): v1 put the target
    # words first and said "switch word every 2-3 turns" + "recast her idea" — the
    # result was a questionnaire that jumped topics and parroted her back before
    # every question. Conversation flow now outranks the words.
    # Third live test: "stay on one topic" read as "stick to MY topic" — she asked what a
    # word meant, got the answer and then "anyway, back to…". She leads now.
    return f"""You are a friendly Spanish-speaking friend chatting with a Russian-speaking learner at level A1–A2. She is a woman.

Priorities, in this order:
1. A real, coherent conversation — like two people talking, not an interview. She leads: whatever she brings up or asks about becomes the conversation.
2. She talks more than you.
3. Chances for her to use the target words below. Sacrifice this whenever it would break the flow.

How a turn sounds:
- First a genuine reaction to what she said: surprise, agree, joke, share a short opinion or a tiny story of your own (you may invent one, like a friend would). 1–2 short sentences.
- Then, at the end, one question — a follow-up about what she just said.
- Do NOT start by repeating what she said. Never echo her whole sentence back.
- Simple A1–A2 words, short sentences (up to ~12 words). Keep your turn to 2–3 sentences.

What to ask about — simple words, grown-up content:
- She is an adult: simple LANGUAGE, but the question itself should be worth answering. Ask about experience, opinion, reasons, comparisons, choices, plans, funny or annoying moments.
- Good: "¿Qué es lo más difícil de vivir con tres gatos?", "¿Prefieres cocinar sola o con alguien? ¿Por qué?", "¿Qué te sorprendió de Georgia?", "Si tuvieras un mes libre, ¿adónde irías?".
- Weak, avoid: colours, sizes, counts, yes/no facts and anything answerable in one word — "¿De qué color es tu gorro?", "¿Tienes gatos?", "¿Vas mucho a la peluquería?".
- Aim for an answer of 2–3 sentences. Never ask two questions at once, and don't chain "¿por qué?" every turn — sometimes react, share your own take, and ask something new.
- If the answer needs a word she probably doesn't know, put that word in your question, so she can reuse it.

Keeping the thread:
- The topic is whatever she is talking about right now. Go deeper into it with follow-up questions (why, how, with whom, what happened next).
- If she asks you something — what a word means, how to say something, your opinion, anything — answer it first, clearly and simply. Your follow-up question is then about what SHE asked (that word in her life, that thing she's curious about), not a way back to the earlier topic: after "¿Qué es peluquería?" ask "¿Vas mucho a la peluquería?", not "¿Fuiste a la peluquería en Georgia?". Never steer back after she moved on ("bueno, volviendo a…").
- You change topic only through a natural bridge from what was just said, never abruptly.
- At the start, pick an everyday topic that several target words fit into, and open with it — it's just an opener, not a plan to stick to.

Target words:
- Use one only when it fits the current topic naturally: ask something whose natural answer needs it.
- First give her the chance: ask around the word so she reaches for it, and don't say it yourself in that turn — this includes your opening line, where it is tempting.
- If the chance passed and she didn't produce it (she went around it, said it in Russian, or got stuck), hand it to her right there in a natural sentence and let her use it straight away: "Ah, se dice «el gorro». ¿Y tú, llevas gorro cuando hace frío?" Quote the Russian word only if she actually said it in Russian; if she described it in Spanish, just give the Spanish. Then move on — don't drill the same word again later in the conversation.
- Never list them, never mention that they are targets.
- It's fine if only 2–3 of them come up in the whole conversation.

Mistakes:
- Don't explain grammar and don't stop the conversation to correct.
- Only when she made a real mistake: slip the correct form into your reaction in passing (e.g. "¡Ah, fuiste al cine! ¿Y qué viste?"). If there was no mistake, don't repeat anything.
- Never "correct" feminine forms she uses about herself.
- If the Russian word is literally there in her message, SAY it out loud at the start of your reply — the Russian word, then the Spanish: "«Шапка» en español es «el gorro»." (write the Russian word in Cyrillic; it is read aloud). Then react to what she said and continue. If instead she described the thing in Spanish without knowing the word ("una cosa en la cabeza"), don't bring Russian into it at all — just "Ah, se dice «el gorro»" and continue.
- If she asks what a Spanish word means, you may give the Russian translation in one short phrase — keep the Spanish word in Latin letters: "«Frontera» es «граница»." Then continue in Spanish.
- If she seems lost or asks (even in Russian) to repeat, say it again more simply.

What you already know about her (from your earlier conversations — she expects you to remember):
{facts_block}

- Never ask about something on that list; build on it instead ("¿Cómo están tus gatos?" rather than "¿Tienes gatos?").
- Don't recite the list back to her and don't say you read it somewhere — you simply remember.

Start: greet her briefly and open your chosen topic with a question.

Target words (Spanish — Russian meaning):
{word_lines or "- (no words yet — just have a simple everyday conversation)"}"""


TURN_FORMAT = """

How her messages reach you:
Each user message is a live speech-to-text transcript of what she just said (verbatim — her mistakes and Russian words are kept on purpose). She mixes Spanish and Russian, and may ask you things in Russian. Russian usually comes in Cyrillic, but the speech-to-text sometimes writes it in Latin letters ("shapka", "chto znachit") — treat those as Russian too. By the time you get it, she has finished her turn.

Answer with JSON:
- "reply" is your next spoken turn, read aloud by text-to-speech: Spanish, plus Russian only where the rules above say so (naming the Russian word she used, a short translation she asked for). No emoji, no markdown, no stage directions, no translations in brackets.
- If her message ends with [LARGA PAUSA], she went quiet for a long time: if it breaks off mid-sentence, she's stuck — help gently, offer the word she seems to be looking for or ask your question again more simply; if it's a finished thought, just reply normally.
- [TERMINÉ] at the end only means she tapped "I'm done" — reply normally.
- "translations": every Russian word or phrase in THIS message of hers that she used because she didn't know it in Spanish, with the Spanish the conversation needed — "ru" as she said it but always in Cyrillic (turn a Latin-letter transliteration like "shapka" back into "шапка"), "es" for a single word in dictionary form (nouns with their article, verbs in the infinitive): [{"ru": "шапка", "es": "el gorro"}]; for a whole Russian phrase, the natural Spanish phrase as she would say it here ("я много работаю" → "trabajo mucho"). Empty list if she used no Russian. Russian she used to ask you something ("что значит…", "как сказать…") is not a translation — answer the question instead. These are shown to her on screen and offered for her vocabulary, so the Spanish must be the natural, common A1–A2 word for her meaning."""

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ru": {"type": "string"}, "es": {"type": "string"}},
                "required": ["ru", "es"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reply", "translations"],
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


CHECK_PROMPT = """You check live speech-to-text from a Spanish learner (A1–A2, Russian speaker) in a voice chat. She went quiet; decide whether she has finished her turn or paused mid-sentence to search for a word.

"complete": false ONLY if her last words cannot end an utterance — it stops right after a conjunction, preposition, article, possessive or filler ("y", "pero", "porque", "que", "de", "con", "a", "la", "un", "mi", "eh…"), or breaks off where more is clearly needed ("Yo quiero comprar", "Mañana voy a", "El sábado yo visité").
Everything else is complete: full sentences, short answers ("Sí", "No sé", "Bien"), questions, sentences with grammar mistakes, Russian words or Russian questions mixed in, answers that don't answer the question. When in doubt: complete."""

CHECK_SCHEMA = {
    "type": "object",
    "properties": {"complete": {"type": "boolean"}},
    "required": ["complete"],
    "additionalProperties": False,
}


def classify_complete(last_bot: str, user_text: str) -> tuple[bool, dict]:
    """Sync (blocking) — call through asyncio.to_thread."""
    response = ai_helper.client.messages.create(
        model=TALK_CHECK_MODEL,
        max_tokens=50,
        system=CHECK_PROMPT,
        messages=[{"role": "user", "content": f"The bot last said: {last_bot}\nHer message: {user_text}"}],
        output_config={"format": {"type": "json_schema", "schema": CHECK_SCHEMA}},
    )
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    complete = json.loads(text).get("complete", True)
    return complete, {"check_in": response.usage.input_tokens, "check_out": response.usage.output_tokens}


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


def claude_turn(target_words, history, user_text: str | None, facts=()) -> tuple[dict, dict]:
    """Sync (blocking) — call through asyncio.to_thread."""
    response = ai_helper.client.messages.create(
        model=TALK_LLM_MODEL,
        max_tokens=600,
        system=build_instructions(target_words, facts) + TURN_FORMAT,
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


def _clean_translations(raw) -> list[dict]:
    pairs = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        ru, es = str(p.get("ru", "")).strip(), str(p.get("es", "")).strip()
        if ru and es and len(es) <= 60:
            pairs.append({"ru": ru, "es": es})
    return pairs[:5]


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
        data, usage = await asyncio.to_thread(claude_turn, target_words, [], None, db.get_talk_memory(user_id))
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
        heard = await asyncio.to_thread(stt_helper.transcribe_mixed, audio, "turn.wav")
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
        reply_task = asyncio.ensure_future(asyncio.to_thread(
            claude_turn, session["target_words"], history, user_text + marker, db.get_talk_memory(user_id)
        ))
        if mode == "auto":
            last_bot = next((str(h.get("text", "")) for h in reversed(history) if h.get("role") == "assistant"), "")
            try:
                complete, check_usage = await asyncio.to_thread(classify_complete, last_bot, user_text)
                usage.update(check_usage)
            except Exception:
                logger.exception("talk: completeness check failed, answering anyway")
                complete = True
            timings["check_ms"] = round((time.monotonic() - t) * 1000)
            if not complete:
                # the reply is already being written — let it finish unused rather than cancel a thread
                reply_task.add_done_callback(lambda f: f.exception())
                return web.json_response({"status": "wait", "user_text": user_text, "usage": usage, "timings": timings})
        data, llm_usage = await reply_task
        timings["llm_ms"] = round((time.monotonic() - t) * 1000)
        usage.update(llm_usage)
        translations = _clean_translations(data.get("translations"))
        if translations:
            db.add_voice_found_words(session_id, translations)
        reply = (data.get("reply") or "").strip()
        if not reply:
            reply = "Perdona, ¿puedes repetirlo?"
        audio_b64 = await _speak(reply, usage, timings)
    except Exception as e:
        return _voice_error(e)

    word_use = track_word_use(session, user_text, reply)
    if word_use != (session.get("word_use") or {}):
        db.set_voice_word_use(session_id, word_use)

    return web.json_response({
        "status": "reply",
        "user_text": user_text,
        "reply": reply,
        "translations": translations,
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

    if ended and not already_done:
        # memory and the review are independent — run them side by side, then report
        memory_task = asyncio.ensure_future(asyncio.to_thread(update_memory, user_id, transcript))
        analysis_task = asyncio.ensure_future(asyncio.to_thread(analyze_conversation, transcript))
        try:
            await memory_task
        except Exception:
            logger.exception("talk: failed to update memory")
        analysis = {}
        try:
            analysis = await analysis_task
            db.set_voice_analysis(session_id, analysis)
        except Exception:
            logger.exception("talk: failed to analyse the conversation")

        if _bot is not None:
            try:
                session = db.get_voice_session(session_id)
                await _bot.send_message(
                    user_id,
                    build_summary(session),
                    parse_mode="HTML",
                    reply_markup=found_words_keyboard(session),
                )
                message = build_analysis_message(analysis)
                if message:
                    await _bot.send_message(user_id, message, parse_mode="HTML")
            except Exception:
                logger.exception("failed to send voice session summary")
    return web.json_response({"ok": True})


MEMORY_MAX_FACTS = 30
MEMORY_PROMPT = """You keep the memory of a Spanish-speaking friend who chats with a Russian-speaking learner (A1–A2) by voice.

You get what you already remember about her plus the transcript of the conversation that just ended. Return the updated memory.

Rules:
- Keep durable facts about her life: family, pets (with names), work, city, travel, tastes, habits, plans, notable things that happened to her. One short sentence each, in simple Spanish.
- Merge, don't duplicate: update a fact if the transcript contradicts or refines it, drop anything that turned out wrong.
- Do NOT keep: her Spanish mistakes, vocabulary she looked up, what the bot said, one-off small talk, anything about the conversation itself.
- The transcript is speech-to-text and may be garbled; keep only what is clearly stated.
- At most {max_facts} facts, most useful first."""

MEMORY_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"}}},
    "required": ["facts"],
    "additionalProperties": False,
}


def update_memory(user_id: int, transcript) -> list:
    """Sync (blocking) — call through asyncio.to_thread. Rewrites her memory from the
    previous one plus this conversation, so the bot stops asking what it already knows."""
    spoken = "\n".join(f"{'ОНА' if t['role'] == 'user' else 'BOT'}: {t['text']}" for t in transcript if t.get("text"))
    if not spoken.strip():
        return db.get_talk_memory(user_id)
    known = db.get_talk_memory(user_id)
    response = ai_helper.client.messages.create(
        model=TALK_LLM_MODEL,
        max_tokens=1000,
        system=MEMORY_PROMPT.format(max_facts=MEMORY_MAX_FACTS),
        messages=[{"role": "user", "content": f"Lo que ya recuerdas:\n" + ("\n".join(f"- {f}" for f in known) or "- (nada)") + f"\n\nConversación:\n{spoken}"}],
        output_config={"format": {"type": "json_schema", "schema": MEMORY_SCHEMA}},
    )
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    facts = [str(f).strip()[:200] for f in (json.loads(text).get("facts") or []) if str(f).strip()]
    facts = facts[:MEMORY_MAX_FACTS]
    db.set_talk_memory(user_id, facts)
    return facts


ANALYSIS_MAX_ERRORS = 5
ANALYSIS_MAX_UPGRADES = 4
ANALYSIS_PROMPT = """You review a voice conversation that a Russian-speaking learner of Spanish (A1–A2, a woman) has just had with a Spanish-speaking friend. You write two blocks for her: what was wrong, and how it could sound better. Explanations in Russian, Spanish stays in Spanish.

The transcript is speech-to-text of the real conversation. ОНА = her, BOT = the bot. Only her lines are reviewed.

"errors" — real mistakes in her Spanish: wrong word for the meaning, wrong verb form or tense, gender/number agreement, articles, prepositions, ser/estar, word order.
- "said": her own words, short (quote only the part that is wrong, with just enough around it).
- "fix": the same thing said correctly.
- "why": a few words in Russian naming the rule — «прошедшее время ir», «род прилагательного», «предлог с ir».
- Only real mistakes. Correct but plain phrasing belongs in "upgrades", not here.
- She is a woman: adjectives about herself are feminine ("estoy cansada"). Never "correct" that.
- Ignore speech-to-text artifacts: punctuation, missing accent marks, repeated words, «э-э».
- A Russian word she used is a vocabulary gap, not a mistake — skip it.
- Both European and Latin American Spanish are correct.
- At most {max_errors}, most important first: what breaks understanding, or what she repeats.

"upgrades" — the same idea said better, for phrases that were already correct but plain.
- "said": her phrase from the transcript. "better": the improved version, A2–B1, natural, still sayable by her. "why": a short Russian note — «звучит естественнее», «готовый оборот для причины».
- Include at least one ready-made chunk she can reuse (es que…, la verdad es que…, me da igual, al final…, o sea…) when it fits something she actually said.
- Never invent phrases she didn't say.
- At most {max_upgrades}.

If she barely spoke or there is nothing worth saying, return empty lists."""

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"said": {"type": "string"}, "fix": {"type": "string"}, "why": {"type": "string"}},
                "required": ["said", "fix", "why"],
                "additionalProperties": False,
            },
        },
        "upgrades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"said": {"type": "string"}, "better": {"type": "string"}, "why": {"type": "string"}},
                "required": ["said", "better", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["errors", "upgrades"],
    "additionalProperties": False,
}


def analyze_conversation(transcript) -> dict:
    """Sync (blocking) — call through asyncio.to_thread. Two blocks she asked for:
    mistakes, and how the same thing could sound better."""
    spoken = "\n".join(f"{'ОНА' if t['role'] == 'user' else 'BOT'}: {t['text']}" for t in transcript if t.get("text"))
    if sum(1 for t in transcript if t.get("role") == "user" and t.get("text")) < 2:
        return {"errors": [], "upgrades": []}
    response = ai_helper.client.messages.create(
        model=TALK_LLM_MODEL,
        max_tokens=1500,
        system=ANALYSIS_PROMPT.format(max_errors=ANALYSIS_MAX_ERRORS, max_upgrades=ANALYSIS_MAX_UPGRADES),
        messages=[{"role": "user", "content": spoken}],
        output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
    )
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    data = json.loads(text)
    return {
        "errors": [e for e in (data.get("errors") or []) if e.get("said") and e.get("fix")][:ANALYSIS_MAX_ERRORS],
        "upgrades": [u for u in (data.get("upgrades") or []) if u.get("said") and u.get("better")][:ANALYSIS_MAX_UPGRADES],
    }


def build_analysis_message(analysis: dict) -> str | None:
    errors, upgrades = analysis.get("errors") or [], analysis.get("upgrades") or []
    if not errors and not upgrades:
        return None
    lines = ["📝 <b>Разбор разговора</b>", ""]
    lines.append("<b>Ошибки</b>")
    if errors:
        for e in errors:
            lines.append(
                f"• <s>{html.escape(e['said'])}</s> → <b>{html.escape(e['fix'])}</b>"
                + (f"\n  <i>{html.escape(e['why'])}</i>" if e.get("why") else "")
            )
    else:
        lines.append("• Ошибок не нашла 🎉")
    if upgrades:
        lines += ["", "<b>Как сказать лучше</b>"]
        for u in upgrades:
            lines.append(
                f"• {html.escape(u['said'])} → <b>{html.escape(u['better'])}</b>"
                + (f"\n  <i>{html.escape(u['why'])}</i>" if u.get("why") else "")
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

_ARTICLE_RE = re.compile(r"^(el|la|los|las|un|una|unos|unas)\s+", re.IGNORECASE)
_VERB_END_RE = re.compile(r"^(.{3,})(ar|er|ir)$", re.IGNORECASE)


def _word_used(phrase: str, text: str) -> bool:
    """Exact form, plus a crude stem match for single verbs so "hablé"/"hablamos" count
    for "hablar". Still not morphology: an irregular form ("fui" for "ir") is missed."""
    core = _ARTICLE_RE.sub("", phrase.strip().lower())
    if not core:
        return False
    text = text.lower()
    if re.search(rf"(?<!\w){re.escape(core)}(?!\w)", text):
        return True
    verb = _VERB_END_RE.match(core) if " " not in core else None
    return bool(verb) and re.search(rf"(?<!\w){re.escape(verb.group(1))}\w{{0,4}}(?!\w)", text) is not None


def track_word_use(session: dict, user_text: str, reply: str) -> dict:
    """Who said each target word first — she (retrieval) or the bot (a hint she then reused).
    Retrieval practice is what makes a word stick, but an unsuccessful attempt followed
    right away by the word works too — this is how we tell the two apart over time."""
    use = dict(session.get("word_use") or {})
    for word in session.get("target_words") or []:
        phrase = word["phrase"]
        state = use.get(phrase)
        if state in ("spontaneous", "after_hint"):
            continue
        if _word_used(phrase, user_text):
            use[phrase] = "after_hint" if state == "hinted" else "spontaneous"
        elif state is None and _word_used(phrase, reply):
            use[phrase] = "hinted"
    return use


# Prices as of 2026-09-19, for the rough estimate in the summary only. TTS is billed per
# audio token; ~$0.015 per minute of speech is OpenAI's own estimate for gpt-4o-mini-tts.
PRICE_STT_PER_MIN = 0.003    # gpt-4o-mini-transcribe
PRICE_LLM_IN = 3.0           # claude-sonnet-4-6, $ per 1M input tokens (cache reads 0.1x, writes 1.25x)
PRICE_LLM_OUT = 15.0
PRICE_CHECK_IN = 1.0          # claude-haiku-4-5 for the "did she finish?" check
PRICE_CHECK_OUT = 5.0
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
    use = session.get("word_use") or {}
    # the transcript is the fallback for words whose turn wasn't recorded (e.g. an old session)
    spontaneous = [p for p in targets if use.get(p) == "spontaneous"]
    after_hint = [p for p in targets if use.get(p) == "after_hint"]
    heard = set(spontaneous) | set(after_hint)
    for p in targets:
        if p not in heard and _word_used(p, user_text):
            (after_hint if use.get(p) == "hinted" else spontaneous).append(p)
            heard.add(p)
    unused = [p for p in targets if p not in heard]

    usage = session["usage"] or {}
    stt = usage.get("stt_seconds", 0) / 60 * PRICE_STT_PER_MIN
    llm = (
        usage.get("llm_in", 0) * PRICE_LLM_IN
        + usage.get("llm_cache_read", 0) * PRICE_LLM_IN * 0.1
        + usage.get("llm_cache_write", 0) * PRICE_LLM_IN * 1.25
        + usage.get("llm_out", 0) * PRICE_LLM_OUT
        + usage.get("check_in", 0) * PRICE_CHECK_IN
        + usage.get("check_out", 0) * PRICE_CHECK_OUT
    ) / 1e6
    tts = usage.get("tts_seconds", 0) / 60 * PRICE_TTS_PER_MIN

    lines = [
        f"🎙 <b>Разговор сохранён</b> — {minutes} мин, твоих реплик: {user_turns}.",
        "",
        "💪 Вспомнила сама: " + (html.escape(", ".join(spontaneous)) if spontaneous else "—"),
        "💡 Сказала после подсказки: " + (html.escape(", ".join(after_hint)) if after_hint else "—"),
        "▫️ Не прозвучали: " + (html.escape(", ".join(unused)) if unused else "—"),
        "<i>(совпадение по форме слова — неправильные глаголы вроде «fui» пока не ловятся)</i>",
        "",
    ]
    found = session.get("found_words") or []
    if found:
        lines += [
            "🔎 <b>Искала по-испански:</b>",
            *(f"• {html.escape(p['ru'])} → {html.escape(p['es'])}" for p in found),
            "<i>Нажми на слово внизу, чтобы добавить его в словарь.</i>",
            "",
        ]
    lines += [
        f"💸 ≈ ${stt + llm + tts:.3f}: распознавание ${stt:.3f}, Claude ${llm:.3f}, озвучка ${tts:.3f} "
        "<i>(оценка по прайсу на 2026-09-19)</i>",
    ]
    return "\n".join(lines)


FOUND_WORDS_BUTTONS = 8


def found_words_keyboard(session: dict):
    """One "add" button per word she looked for — added only if she taps it (each add is a
    Claude call and a card; she decides which ones are worth learning)."""
    found = (session.get("found_words") or [])[:FOUND_WORDS_BUTTONS]
    if not found:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➕ {p['es']}", callback_data=f"talkadd:{session['id']}:{i}")]
        for i, p in enumerate(found)
    ])


def register(api: web.Application, *, bot, bot_token: str, owner_id: int):
    global _bot, _bot_token, _owner_id
    _bot, _bot_token, _owner_id = bot, bot_token, owner_id
    api.router.add_get("/talk", handle_page)
    api.router.add_post("/talk/start", handle_start)
    api.router.add_post("/talk/turn", handle_turn)
    api.router.add_post("/talk/transcript", handle_transcript)
