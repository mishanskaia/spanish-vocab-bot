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
import random
import re
import time
from datetime import datetime, timedelta, timezone
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


def request_owner_id(request: web.Request) -> int | None:
    """Owner-only auth shared with the other Mini App pages (see word_catalog.py):
    same initData / signed-link check, one implementation."""
    return _request_owner_id(request)


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

# Shared by the free conversation and the picture mode (scene) — same rules, one copy.
MISTAKES_RULES = """Mistakes:
- Don't explain grammar and don't stop the conversation to correct.
- Only when she made a real mistake: slip the correct form into your reaction in passing (e.g. "¡Ah, fuiste al cine! ¿Y qué viste?"). If there was no mistake, don't repeat anything.
- Never "correct" feminine forms she uses about herself.
- If the Russian word is literally there in her message, SAY it out loud at the start of your reply — the Russian word, then the Spanish: "«Шапка» en español es «el gorro»." (write the Russian word in Cyrillic; it is read aloud). Then react to what she said and continue. If instead she described the thing in Spanish without knowing the word ("una cosa en la cabeza"), don't bring Russian into it at all — just "Ah, se dice «el gorro»" and continue.
- If she asks what a Spanish word means, you may give the Russian translation in one short phrase — keep the Spanish word in Latin letters: "«Frontera» es «граница»." Then continue in Spanish.
- If she seems lost or asks (even in Russian) to repeat, say it again more simply.

"""


# Focus loop (2026-10-11): the mistake she is working on and the phrase from the last review go
# into the next conversations, so the correction comes back as a chance to say it — not as text to
# remember. The bot provokes the form with a question; if she goes around it, it says the form
# itself and invites her to use it. Whether she managed is judged by the review afterwards.
def _focus_rules(focus, phrase: str | None, scene: bool = False) -> str:
    if not focus and not phrase:
        return ""
    out = []
    if focus:
        when = ("in the her-life part (phase 3)" if scene else "by your third turn at the latest")
        example = f' — e.g. "{focus["trigger"]}"' if focus.get("trigger") else ""
        out.append(f"""Her current focus (a mistake from earlier conversations): {focus["rule"]} — the right form is "{focus["right_form"]}" (she tends to say "{focus["wrong"]}").
- {when[0].upper() + when[1:]}, ask one natural question whose answer needs this form{example}. Fit it to what you're talking about; never announce it — it's a conversation, not a test.
- If she uses the right form, just react to what she said (no grammar praise).
- If she goes around it or uses the wrong form: say exactly the right form ("{focus["right_form"]}" — the same person and tense, not a related form like "fuiste" for "fui") in a natural sentence of your own, then invite her to say something with it ("Yo también fui a un café el sábado. ¿Y tú, adónde fuiste después?"). For this one mistake this replaces the general "slip the correct form in passing" rule above — echoing her with "¡Ah, fuiste…!" doesn't show her own form.
- Once or twice in the conversation is enough — don't drill.""")
    if phrase:
        out.append(f"""A phrase worth hearing: "{phrase}". Use it yourself once or twice where it fits naturally. Don't ask her to repeat it and don't point it out.""")
    return "\n".join(out) + "\n\n"


def current_practice(user_id: int) -> tuple:
    """(active focus or None, the phrase from the most recent review or None)."""
    phrase = None
    for s in db.get_recent_voice_sessions(user_id, 3):
        up = (s.get("analysis") or {}).get("upgrade") or {}
        if up.get("better"):
            phrase = up["better"]
            break
    return db.get_active_focus(user_id), phrase


# Memory is background, not a source of topics (2026-10-11). The old rule "build on it" made the
# most vivid fact a hook: she mentioned a trip to South Africa once, and every conversation after
# that drifted back to it — which fed more trip facts into the memory, which made the hook stronger.
def _facts_rules(facts, avoid=()) -> str:
    block = ("\n".join(f"- {f}" for f in facts) if facts
             else "- (todavía no sabes nada de ella — es vuestra primera conversación)")
    avoid_line = ""
    if avoid:
        avoid_line = ("- Last time you brought these up yourself — don't raise them yourself this time: "
                      + "; ".join(avoid) + ".\n")
    return f"""What you already know about her (from your earlier conversations — she expects you to remember):
{block}

This is background, not a list of topics:
- Use it so you never ask what you already know ("¿Cómo están tus gatos?" rather than "¿Tienes gatos?"), and so you react like a friend who remembers when SHE brings something up.
- Don't steer the conversation toward these facts. A big event in her life (a trip, a move, a new job) is exactly the hook to resist: she wants to talk about many different things, not the same one every time.
- At most once in the whole conversation you may bring up one of them yourself, briefly, and only where it fits what she is saying.
{avoid_line}- Don't recite the list back to her and don't say you read it somewhere — you simply remember.

"""


# Opener topics, rotated by code so consecutive conversations start in different places — the
# model, left to choose, kept opening with the same comfortable topic. Only an opener: she leads after.
# Each topic is a scenario with two or three sides that need different tenses and persons
# (2026-10-11: she wanted to be pushed out of «presente + yo» by the topic itself, not by drills).
OPENER_TOPICS = [
    "then and now — her city: what it was like when she was a child, what it is like now, what will change",
    "then and now — herself: what she was like and did at 15, what she is like now, how she imagines herself in ten years",
    "a past trip and the next one: where she went, what happened there, where she is going next and what she plans to do",
    "yesterday and tomorrow: what she did yesterday, what she is going to do tomorrow and at the weekend",
    "something that happened to her: a funny, strange or annoying story — what happened, what she was doing when it happened",
    "her family: what her relatives do now, what they did together when she was a child, what they plan to do together",
    "a friend: how they met, what they used to do together, what her friend is doing now",
    "food through time: what her family cooked when she was a child, what she cooks now, a dish she wants to learn",
    "work then and now: her first job or studies, her work now, what she wants to do in the future",
    "celebrations: how her family celebrated holidays before, how they celebrate now, the next celebration she is planning",
    "the home: the house she grew up in, where she lives now, what her dream home will be like",
    "habits that changed: something she used to do and stopped, something she does now, something she is going to start",
    "a recent weekend and the next one: what she and her partner or friends did, what they are planning",
    "people around her: a neighbour or colleague — what they are like, a story about them, what they are going to do",
    "a year from now: what will be different in her life, her city, her Spanish — and what was different a year ago",
    "films or books: one she saw or read long ago, one recently, one she is going to see — and who she watched it with",
    "a small problem: something that went wrong recently, what she did, what she will do next time",
    "seasons: what she did last summer, what she does in winter, her plans for the next holidays",
]
OPENER_NO_REPEAT = 7


def pick_opener(user_id: int) -> str:
    recent = set(db.get_recent_openers(user_id, OPENER_NO_REPEAT))
    fresh = [t for t in OPENER_TOPICS if t not in recent] or OPENER_TOPICS
    return random.choice(fresh)


def build_instructions(target_words, facts=(), opener: str | None = None, avoid_facts=(),
                       focus=None, phrase: str | None = None) -> str:
    word_lines = "\n".join(f"- {w['phrase']} — {w['meaning']}" for w in target_words)
    facts_rules = _facts_rules(facts, avoid_facts)
    opener_line = (f"Today's opener topic: {opener}. Open with a question on one side of it — one where a target word "
                   "might fit if possible. The topic has several sides (before, now, later; her and the people around "
                   "her): move through them as the conversation goes, so she needs different tenses. It is only an "
                   "opener, not a plan to stick to — if she takes the talk elsewhere, follow her; and it is NOT a cue "
                   "to talk about a fact you remember about her."
                   if opener else "Pick an everyday topic that several target words fit into, and open with it.")
    # Priorities rewritten after the first live test (2026-09-19): v1 put the target
    # words first and said "switch word every 2-3 turns" + "recast her idea" — the
    # result was a questionnaire that jumped topics and parroted her back before
    # every question. Conversation flow now outranks the words.
    # Third live test: "stay on one topic" read as "stick to MY topic" — she asked what a
    # word meant, got the answer and then "anyway, back to…". She leads now.
    return f"""You are a friendly Spanish-speaking friend chatting with a Russian-speaking learner at level A1–A2. She is a woman.

Priorities, in this order:
1. A real, coherent conversation — like two people talking, not an interview. She leads: whatever she brings up or asks about becomes the conversation.
2. A balanced conversation: you bring real content, she answers at length.
3. Chances for her to use the target words below. Sacrifice this whenever it would break the flow.

How a turn sounds:
- First a genuine reaction with real content — your opinion, a small story from "your" life (you may invent one, like a friend would), a detail, a comparison ("En México la gente cena muy tarde, a veces a las diez. A mí me encanta, pero mi hermana dice que es malo para dormir…"). Your lines are also her listening practice, so give her something worth hearing.
- Then, at the end, one question — a follow-up about what she just said.
- Do NOT start by repeating what she said. Never echo her whole sentence back.
- Usually 3–5 sentences (about 30–60 words, never more than ~60), and let the length vary like a real person's: about one turn in three is short — just a quick reaction and the question, 1–2 sentences — and now and then a little story of 5 sentences. Never the same length every time.
- Simple A2 words, but connected sentences (porque, pero, entonces, aunque, cuando), not choppy fragments.
- Latin American Spanish: "ustedes", never "vosotros" (and no vosotros verb forms like "hicisteis").

Tenses and persons — make her use more than the present and "yo":
- Ask about the past (what she did, what happened, what it was like before — "¿Qué hiciste…?", "¿Cómo era…?"), the future and plans ("¿Qué vas a hacer…?", "¿Qué cambiará…?"), not only the present.
- Ask about other people, so she needs other persons: her family, friends, partner, cats ("¿Y tu hermana qué hace los domingos?" → él/ella; "¿Qué hicieron tú y tu pareja?" → nosotros; "¿Tus amigos también…?" → ellos).
- Vary it from question to question; never quiz, never mention grammar. When a part of the topic runs out, move to its other side: "¿Y antes, cómo era?", "¿Y qué planean para el verano?".

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
- When a topic runs dry, bridge somewhere NEW — not back to a topic you already know she has (a trip, her job, her pets). Look wide: everyday life has many corners.
- {opener_line}

Target words:
- Use one only when it fits the current topic naturally: ask something whose natural answer needs it.
- First give her the chance: ask around the word so she reaches for it, and don't say it yourself in that turn — this includes your opening line, where it is tempting.
- If the chance passed and she didn't produce it (she went around it, said it in Russian, or got stuck), hand it to her right there in a natural sentence and let her use it straight away: "Ah, se dice «el gorro». ¿Y tú, llevas gorro cuando hace frío?" Quote the Russian word only if she actually said it in Russian; if she described it in Spanish, just give the Spanish. Then move on — don't drill the same word again later in the conversation.
- Never list them, never mention that they are targets.
- It's fine if only 2–3 of them come up in the whole conversation.

{MISTAKES_RULES}{_focus_rules(focus, phrase)}{facts_rules}Start: greet her briefly and open the opener topic with a question.

Target words (Spanish — Russian meaning):
{word_lines or "- (no words yet — just have a simple everyday conversation)"}"""


def build_scene_instructions(scene: dict, facts=(), avoid_facts=(), focus=None, phrase: str | None = None) -> str:
    """Picture mode (/scene → «Описать голосом», 2026-09-28). Unlike the free conversation,
    here the words come first: the picture takes the choice of topic away from her, which is
    the point — in free talk she steers to comfortable topics and her passive words never
    come up. Three phases: her description → questions about what she hasn't named, with a
    hint ladder per word → her own life, where she reuses the same words unprompted (the
    second, independent retrieval is what makes them stick)."""
    word_lines = "\n".join(
        f"- {w['es']} — {w['ru']}" + (f" (in the picture: {w['shown']})" if w.get("shown") else "")
        for w in scene["words"]
    )
    return f"""You are a friendly Spanish-speaking friend. You and a Russian-speaking learner (A1–A2, a woman) are looking at the same picture on her phone. She describes it aloud and you talk about it. You can't see it yourself — this is what is in it:
{scene["seen"]}

Target words — each can be seen in the picture:
{word_lines}

The conversation has three phases. Move on when a phase is done; never announce or name the phases.

1. Her description. Open with one short greeting and ask her to describe the picture. Let her talk; react briefly and warmly.
2. Details. Ask about parts of the picture she hasn't mentioned, where the natural answer is a target word she hasn't said yet. One word at a time, one question per turn. Questions here may be concrete ("¿Qué tiene en la mano?", "¿Qué tiempo hace?").
   - Don't say a target word yourself before she has had her chance.
   - Up to two questions around a word. If she still doesn't produce it: a hint — what it's for, where it is, or how it starts ("empieza por «pa…»"). If that doesn't work either: give the word in a natural sentence and ask her to say something with it — "Es «el paraguas». ¿Qué hace la mujer con el paraguas?"
   - A correct synonym is fine: accept it ("¡Sí!"), mention the target in passing ("también se dice «…»"), and let her use it.
   - A word she has already said, in any form, is done — don't come back to it in this phase.
3. Her life. When every target word has been said — by her or handed to her — bridge from the picture to her own life — through the situation in the picture (the rain, the cooking, the waiting), not to a topic you remember about her — and have a real conversation: she leads, you react and ask one follow-up question. Ask grown-up questions (experience, opinion, reasons, choices, plans) whose natural answers reuse the target words in her own situation: "¿Y a ti? ¿Qué haces cuando llueve y no tienes paraguas?". Don't hand her the words again here — let her find them herself.

How a turn sounds:
- Phases 1–2: simple A1–A2 words, short sentences (up to ~12 words), 1–3 sentences — she is the one describing.
- Phase 3: like a real conversation — usually 3–5 connected sentences with your own opinion or a little story, length varying, one question at the end. Ask about her past, her plans and other people in her life, not only the present and "yo".
- Latin American Spanish: "ustedes", never "vosotros".
- Never describe the picture for her, never list the target words, never mention that they are targets.
- If she asks what something in the picture is called, tell her — then ask her to use it.
- Do NOT start by repeating what she said.

{MISTAKES_RULES}{_focus_rules(focus, phrase, scene=True)}{_facts_rules(facts, avoid_facts)}Start: greet her in one short sentence and ask her to describe the picture."""


TURN_FORMAT = """

How her messages reach you:
Each user message is a live speech-to-text transcript of what she just said (verbatim — her mistakes and Russian words are kept on purpose). She mixes Spanish and Russian, and may ask you things in Russian. Russian usually comes in Cyrillic, but the speech-to-text sometimes writes it in Latin letters ("shapka", "chto znachit") — treat those as Russian too. By the time you get it, she has finished her turn.

Answer with JSON:
- "reply" is your next spoken turn, read aloud by text-to-speech: Spanish, plus Russian only where the rules above say so (naming the Russian word she used, a short translation she asked for). No emoji, no markdown, no stage directions, no translations in brackets.
- If her message ends with [LARGA PAUSA], she went quiet for a long time: if it breaks off mid-sentence, she's stuck — help gently, offer the word she seems to be looking for or ask your question again more simply; if it's a finished thought, just reply normally.
- [TERMINÉ] at the end only means she tapped "I'm done" — reply normally.
- "translations": every Russian word or phrase in THIS message of hers that she used because she didn't know it in Spanish, with the Spanish the conversation needed — "ru" as she said it but always in Cyrillic (turn a Latin-letter transliteration like "shapka" back into "шапка"), "es" for a single word in dictionary form (nouns with their article, verbs in the infinitive): [{"ru": "шапка", "es": "el gorro"}]; for a whole Russian phrase, the natural Spanish phrase as she would say it here ("я много работаю" → "trabajo mucho"). Empty list if she used no Russian. Only her LAST message counts: a Russian word from her earlier messages was already translated — never list it again unless she says it in Russian again now. Russian she used to ask you something ("что значит…", "как сказать…") is not a translation — answer the question instead. These are shown to her on screen and offered for her vocabulary, so the Spanish must be the natural, common A1–A2 word for her meaning.
- "used_words": the target words she herself used in THIS message, in any grammatical form ("fui" counts for "ir", "paraguas" for "el paraguas") — copied exactly as they appear in the target list. Only her message, not your reply. Empty list if none.
- "raised_fact": if YOUR reply brings up something from what you remember about her that she did not mention in this conversation, copy that fact exactly as it is in your list; otherwise "". Reacting to something she said herself doesn't count."""

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
        "used_words": {"type": "array", "items": {"type": "string"}},
        "raised_fact": {"type": "string"},
    },
    "required": ["reply", "translations", "used_words", "raised_fact"],
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


def session_instructions(target_words, facts=(), scene: dict | None = None,
                         opener: str | None = None, avoid_facts=(), focus=None, phrase: str | None = None) -> str:
    base = (build_scene_instructions(scene, facts, avoid_facts, focus, phrase) if scene
            else build_instructions(target_words, facts, opener, avoid_facts, focus, phrase))
    return base + TURN_FORMAT


def scene_target_words(scene: dict) -> list[dict]:
    """Scene words in the shape the rest of the session code uses."""
    return [{"phrase": w["es"], "meaning": w["ru"]} for w in scene["words"]]


def claude_turn(system: str, history, user_text: str | None) -> tuple[dict, dict]:
    """Sync (blocking) — call through asyncio.to_thread."""
    response = ai_helper.client.messages.create(
        model=TALK_LLM_MODEL,
        max_tokens=600,
        system=system,
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


_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
_CYR_WORD_RE = re.compile(r"[а-яё]+")
_ANY_WORD_RE = re.compile(r"[a-zа-яё]+")


def _said_in(ru: str, text: str) -> bool:
    """Is this Russian word/phrase really in her message? Claude sees the whole history and
    sometimes re-lists a word she said several turns ago — the plaque then reappeared under
    every later reply. Loose on purpose: a shared stem counts ("шапку" for "шапка"), and so
    does a Latin transliteration, which STT sometimes writes ("shapka")."""
    words = [w for w in _CYR_WORD_RE.findall(ru.lower()) if len(w) >= 3]
    if not words:
        return True  # nothing to check against — don't hide it
    said = _ANY_WORD_RE.findall(text.lower())

    def stem(w):
        return w[:max(3, len(w) - 2)]

    def found(w):
        variants = {stem(w)}
        latin = "".join(_TRANSLIT.get(c, c) for c in w)
        if len(latin) >= 5:  # "дом" → "dom" would match "domingo"
            variants.add(stem(latin))
        return any(s.startswith(v) for s in said for v in variants)

    return any(found(w) for w in words)


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


def _note_raised_fact(session_id: int, raised, facts) -> None:
    """Remember a memory fact the bot brought up itself, so the next conversation doesn't
    open the same door. Only facts really on her list count — the model may paraphrase."""
    raised = str(raised or "").strip().lower()
    if not raised:
        return
    for fact in facts:
        if raised == fact.lower() or raised in fact.lower() or fact.lower() in raised:
            db.add_voice_raised_fact(session_id, fact)
            return


async def handle_start(request: web.Request) -> web.Response:
    """Opens a session: picks the target words, and the bot says the first line."""
    user_id = _request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not is_enabled():
        return web.json_response({"error": "OPENAI_API_KEY не задан"}, status=503)

    try:
        body = await request.json()
    except Exception:
        body = {}
    scene = None
    if isinstance(body, dict) and body.get("scene_id"):
        try:
            scene = db.get_scene(int(body["scene_id"]))
        except (TypeError, ValueError):
            scene = None
        if scene is None or scene["user_id"] != user_id:
            return web.json_response({"error": "картинка не найдена"}, status=404)

    target_words = scene_target_words(scene) if scene else db.get_voice_target_words(user_id, TARGET_WORD_COUNT)
    opener = None if scene else pick_opener(user_id)
    facts = db.get_talk_memory(user_id)
    system = session_instructions(target_words, facts, scene, opener, db.get_last_raised_facts(user_id),
                                  *current_practice(user_id))
    timings, usage = {}, {}
    try:
        t = time.monotonic()
        data, usage = await asyncio.to_thread(claude_turn, system, [], None)
        timings["llm_ms"] = round((time.monotonic() - t) * 1000)
        fallback = "¡Hola! ¿Qué ves en la imagen?" if scene else "¡Hola! ¿Qué tal tu día?"
        reply = data.get("reply", "").strip() or fallback
        audio_b64 = await _speak(reply, usage, timings)
    except Exception as e:
        return _voice_error(e)

    session_id = db.create_voice_session(user_id, target_words, scene["id"] if scene else None, opener)
    _note_raised_fact(session_id, data.get("raised_fact"), facts)
    return web.json_response({
        "session_id": session_id,
        "target_count": len(target_words),
        "scene": {"title": scene["title"], "image": f"/talk/scene/{scene['id']}/image"} if scene else None,
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
        scene = db.get_scene(session["scene_id"]) if session.get("scene_id") else None
        facts = db.get_talk_memory(user_id)
        system = session_instructions(session["target_words"], facts, scene, session.get("opener"),
                                      db.get_last_raised_facts(user_id, before_id=session_id),
                                      *current_practice(user_id))
        reply_task = asyncio.ensure_future(asyncio.to_thread(claude_turn, system, history, user_text + marker))
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
        translations = [p for p in _clean_translations(data.get("translations")) if _said_in(p["ru"], user_text)]
        if translations:
            db.add_voice_found_words(session_id, translations)
        reply = (data.get("reply") or "").strip()
        if not reply:
            reply = "Perdona, ¿puedes repetirlo?"
        audio_b64 = await _speak(reply, usage, timings)
    except Exception as e:
        return _voice_error(e)

    _note_raised_fact(session_id, data.get("raised_fact"), facts)
    word_use = track_word_use(session, user_text, reply, data.get("used_words"))
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


async def handle_scene_image(request: web.Request) -> web.Response:
    """The picture for the page. Fetched with the same auth headers as the API calls (an
    <img src> can't send them), so it's owner-only like everything else here."""
    user_id = _request_owner_id(request)
    if user_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        found = db.get_scene_image(int(request.match_info["scene_id"]))
    except ValueError:
        found = None
    if found is None or found[0] != user_id:
        return web.json_response({"error": "not found"}, status=404)
    return web.Response(body=found[1], content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


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
        memory_task = asyncio.ensure_future(asyncio.to_thread(
            update_memory, user_id, transcript, bool(session.get("scene_id"))
        ))
        ctx = review_context(user_id)
        analysis_task = asyncio.ensure_future(asyncio.to_thread(
            analyze_conversation, transcript, ctx["focus"], ctx["past_errors"], ctx["past_upgrades"]
        ))
        try:
            await memory_task
        except Exception:
            logger.exception("talk: failed to update memory")
        analysis, focus_state = {}, {}
        try:
            analysis = await analysis_task
            db.set_voice_analysis(session_id, analysis)
            focus_state = apply_focus(user_id, analysis)
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
                message = build_analysis_message(analysis, focus_state)
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


SCENE_MEMORY_NOTE = (
    "Ojo: la primera parte de esta conversación describe una imagen inventada (personas, objetos, "
    "lo que pasa en ella). Eso NO es su vida — no guardes nada de la imagen, solo lo que cuenta de sí misma.\n\n"
)


def update_memory(user_id: int, transcript, about_picture: bool = False) -> list:
    """Sync (blocking) — call through asyncio.to_thread. Rewrites her memory from the
    previous one plus this conversation, so the bot stops asking what it already knows.
    about_picture: a /scene conversation — the picture's people aren't her."""
    spoken = "\n".join(f"{'ОНА' if t['role'] == 'user' else 'BOT'}: {t['text']}" for t in transcript if t.get("text"))
    if not spoken.strip():
        return db.get_talk_memory(user_id)
    known = db.get_talk_memory(user_id)
    response = ai_helper.client.messages.create(
        model=TALK_LLM_MODEL,
        max_tokens=1000,
        system=MEMORY_PROMPT.format(max_facts=MEMORY_MAX_FACTS),
        messages=[{"role": "user", "content": (SCENE_MEMORY_NOTE if about_picture else "") + f"Lo que ya recuerdas:\n" + ("\n".join(f"- {f}" for f in known) or "- (nada)") + f"\n\nConversación:\n{spoken}"}],
        output_config={"format": {"type": "json_schema", "schema": MEMORY_SCHEMA}},
    )
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    facts = [str(f).strip()[:200] for f in (json.loads(text).get("facts") or []) if str(f).strip()]
    facts = facts[:MEMORY_MAX_FACTS]
    db.set_talk_memory(user_id, facts)
    return facts


# Post-conversation review, rebuilt 2026-10-11. The old review listed up to 12 items (5 mistakes,
# 4 upgrades, 3 «known but unused» words); she read it and forgot it by the next conversation,
# and it kept naming the same mistakes because it had no idea they were old news. Now she sees
# two things — one mistake (the «focus») and one phrase — and the focus goes INTO the next
# conversations: the bot gives her chances to use the right form until she has done it twice in a
# row. The full list of mistakes is still found and stored: it is what tells a repeated mistake
# from a one-off slip. «Known but unused» was dropped entirely (she found it irrelevant).
ANALYSIS_MAX_ERRORS = 5
FOCUS_DONE_STREAK = 2          # correct in this many conversations in a row → the focus is done
PAST_ERRORS_SESSIONS = 10      # how far back the review looks for repeated mistakes
PAST_UPGRADES = 8              # recent phrases not to suggest again

ANALYSIS_PROMPT = """You review a voice conversation that a Russian-speaking learner of Spanish (A1–A2, a woman) has just had with a Spanish-speaking friend. Explanations in Russian, Spanish stays in Spanish.

The transcript is speech-to-text of the real conversation. ОНА = her, BOT = the bot. Only her lines are reviewed.

"errors" — real mistakes in her Spanish: wrong word for the meaning, wrong verb form or tense, gender/number agreement, articles, prepositions, ser/estar, word order.
- "said": her own words, short (quote only the part that is wrong, with just enough around it).
- "fix": the same thing said correctly.
- "why": a few words in Russian naming the rule — «прошедшее время ir», «род прилагательного», «предлог с ir».
- Only real mistakes. Correct but plain phrasing is not a mistake.
- She is a woman: adjectives about herself are feminine ("estoy cansada"). Never "correct" that.
- The present tense about how she is or feels NOW is correct, even inside a story about the past ("El sábado fui a la playa. Estoy cansada" — she is tired now). Don't turn it into a past tense.
- Ignore speech-to-text artifacts: punctuation, missing accent marks, repeated words, «э-э».
- A Russian word she used is a vocabulary gap, not a mistake — skip it.
- Both European and Latin American Spanish are correct.
- If "fix" would be the same as "said", it isn't a mistake — leave it out.
- At most {max_errors}.

"focus" — ONE of those errors that she will work on next. She sees only this one, so choose carefully. Priority:
1. It gets in the way of understanding (wrong tense that changes when it happened, a wrong word).
2. It repeats — the same rule is in her past mistakes (listed with the transcript, if any).
3. A frequent basic form (past tense of common verbs, ser/estar, gender, basic prepositions) beats a rare construction.
It must be easy to provoke with a question in a conversation: a verb tense — yes ("¿Qué hiciste el sábado?"); a preposition inside one rare construction — no, choose another.
- "rule": the rule in Russian, short — «ir в прошедшем: fui».
- "said", "fix", "why": as in errors.
- "trigger": one natural Spanish question whose natural answer needs the right form ("¿Adónde fuiste el fin de semana?").
- If there is a current focus (given with the transcript), still choose the best NEW candidate — the code decides whether it is used. If no error is suitable, all fields empty.

"focus_result" — only if a current focus is given: did she use that right form in THIS conversation?
- "correct": she used the right form at least once and never the wrong one after the bot showed it;
- "wrong": she made that same mistake again;
- "not_used": the form didn't come up (or the bot said it and she didn't).
No current focus → "not_used".

"upgrade" — ONE phrase of hers that was correct but plain, and how to say it better.
- "said": her phrase from the transcript. "better": the improved version, A2–B1, natural, still sayable by her. "chunk": the reusable chunk itself, as it appears in "better" ("la verdad es que", "lo bueno es que"). "why": a short Russian note.
- Prefer a phrase she leans on — one she said more than once in this conversation ("pienso que es bueno…") is the best candidate.
- "better" should hinge on a ready-made chunk she can reuse in many conversations (es que…, la verdad es que…, me da igual, al final…, o sea…, sobre todo…, lo bueno es que…), not just extra details or a one-off fancy word.
- The chunk must do the same job as what she said: for an opinion ("pienso que es bueno") — "me parece que", "para mí", "lo bueno es que", not a filler like "o sea".
- The chunks listed with the transcript as already suggested are taken: pick a different chunk. Never invent phrases she didn't say.
- Nothing worth it → all fields empty.

If she barely spoke, return empty lists and empty fields."""

_PAIR = {"said": {"type": "string"}, "fix": {"type": "string"}, "why": {"type": "string"}}
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "errors": {
            "type": "array",
            "items": {"type": "object", "properties": _PAIR, "required": ["said", "fix", "why"],
                      "additionalProperties": False},
        },
        "focus": {
            "type": "object",
            "properties": {**_PAIR, "rule": {"type": "string"}, "trigger": {"type": "string"}},
            "required": ["rule", "said", "fix", "why", "trigger"],
            "additionalProperties": False,
        },
        "focus_result": {"type": "string", "enum": ["correct", "wrong", "not_used"]},
        "upgrade": {
            "type": "object",
            "properties": {"said": {"type": "string"}, "better": {"type": "string"}, "chunk": {"type": "string"},
                           "why": {"type": "string"}},
            "required": ["said", "better", "chunk", "why"],
            "additionalProperties": False,
        },
    },
    "required": ["errors", "focus", "focus_result", "upgrade"],
    "additionalProperties": False,
}

_EMPTY_ANALYSIS = {"errors": [], "focus": {}, "focus_result": "not_used", "upgrade": {}}


def review_context(user_id: int) -> dict:
    """What the review needs to know beyond the transcript: the current focus, her past
    mistakes (to spot a repeated one) and the phrases already suggested (not to repeat them)."""
    sessions = db.get_recent_voice_sessions(user_id, PAST_ERRORS_SESSIONS)
    past_errors = [e for s in sessions for e in ((s.get("analysis") or {}).get("errors") or [])]
    past_upgrades = []
    for s in sessions:
        a = s.get("analysis") or {}
        ups = ([a["upgrade"]] if (a.get("upgrade") or {}).get("better") else []) + list(a.get("upgrades") or [])
        past_upgrades += [u.get("chunk") or u["better"] for u in ups if u.get("better")]
    return {"focus": db.get_active_focus(user_id), "past_errors": past_errors,
            "past_upgrades": past_upgrades[:PAST_UPGRADES]}


def _norm(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(text.lower()))


def _reused_chunk(upgrade: dict, past) -> bool:
    chunk, better = _norm(upgrade.get("chunk", "")), _norm(upgrade.get("better", ""))
    for p in past:
        p = _norm(p)
        if p and (p in better or (chunk and (chunk in p or p in chunk))):
            return True
    return False


def analyze_conversation(transcript, focus=None, past_errors=(), past_upgrades=()) -> dict:
    """Sync (blocking) — call through asyncio.to_thread. All the mistakes (stored), one focus
    candidate, whether the current focus was used right, and one phrase."""
    spoken = "\n".join(f"{'ОНА' if t['role'] == 'user' else 'BOT'}: {t['text']}" for t in transcript if t.get("text"))
    if sum(1 for t in transcript if t.get("role") == "user" and t.get("text")) < 2:
        return dict(_EMPTY_ANALYSIS)
    content = spoken
    if focus:
        content += (f"\n\nТекущий фокус: {focus['rule']} — правильно «{focus['right_form']}», "
                    f"она говорила «{focus['wrong']}».")
    if past_errors:
        content += "\n\nЕё ошибки в прошлых разговорах:\n" + "\n".join(
            f"- {e.get('said')} → {e.get('fix')} ({e.get('why', '')})" for e in past_errors[:30])
    if past_upgrades:
        content += "\n\nУже предлагали эти обороты (выбери другой):\n" + "\n".join(f"- {u}" for u in past_upgrades)
    response = ai_helper.client.messages.create(
        model=TALK_LLM_MODEL,
        max_tokens=2000,
        system=ANALYSIS_PROMPT.format(max_errors=ANALYSIS_MAX_ERRORS),
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}},
    )
    data = json.loads(next((b.text for b in response.content if b.type == "text"), "{}"))
    focus_c = data.get("focus") or {}
    upgrade = data.get("upgrade") or {}
    result = data.get("focus_result") if focus else "not_used"
    return {
        "errors": [e for e in (data.get("errors") or [])
                   if e.get("said") and e.get("fix") and _norm(e["said"]) != _norm(e["fix"])][:ANALYSIS_MAX_ERRORS],
        "focus": focus_c if (focus_c.get("fix") and focus_c.get("said") and focus_c.get("rule")
                             and _norm(focus_c["said"]) != _norm(focus_c["fix"])) else {},
        "focus_result": result if result in ("correct", "wrong", "not_used") else "not_used",
        # a chunk she was already given is dropped in code too — the model reused it 6 times of 6
        "upgrade": upgrade if upgrade.get("said") and upgrade.get("better") and not _reused_chunk(upgrade, past_upgrades) else {},
    }


def apply_focus(user_id: int, analysis: dict) -> dict:
    """Moves the focus loop one conversation forward. Correct → streak+1 (FOCUS_DONE_STREAK →
    done), wrong → streak back to 0, not used → unchanged. A new focus only replaces a done one
    (or none): one thing at a time. Returns what the message needs."""
    active = db.get_active_focus(user_id)
    out = {"active": active, "result": None, "streak": 0, "done": False, "new": None}
    if active:
        result = analysis.get("focus_result") or "not_used"
        streak = active["streak"] + 1 if result == "correct" else 0 if result == "wrong" else active["streak"]
        done = streak >= FOCUS_DONE_STREAK
        db.set_focus_streak(active["id"], streak, done)
        out.update(result=result, streak=streak, done=done)
    cand = analysis.get("focus") or {}
    if (not active or out["done"]) and cand.get("fix"):
        db.create_focus(user_id, cand["rule"], cand["said"], cand["fix"], cand.get("why", ""), cand.get("trigger", ""))
        out["new"] = cand
    return out


def build_analysis_message(analysis: dict, focus_state: dict | None = None) -> str | None:
    """Two things at most: the focus (its progress, or a new one) and one phrase."""
    focus_state = focus_state or {}
    lines = []
    active = focus_state.get("active")
    if active:
        right = html.escape(active["right_form"])
        result = focus_state.get("result")
        if result == "correct" and focus_state.get("done"):
            lines.append(f"✅ <b>Фокус снят:</b> «{right}» — правильно {FOCUS_DONE_STREAK} раза подряд.")
        elif result == "correct":
            lines.append(f"✅ <b>Фокус:</b> «{right}» — сказала правильно "
                         f"({focus_state['streak']} из {FOCUS_DONE_STREAK}).")
        elif result == "wrong":
            lines.append(f"🎯 <b>Фокус пока в работе:</b> <s>{html.escape(active['wrong'])}</s> → <b>{right}</b>"
                         + (f"\n<i>{html.escape(active['why'])}</i>" if active.get("why") else ""))
        else:
            lines.append(f"🎯 <b>Фокус:</b> «{right}» — в этот раз не прозвучало, вернёмся к нему.")
    new = focus_state.get("new")
    if new:
        lines.append(f"🎯 <b>Новый фокус:</b> <s>{html.escape(new['said'])}</s> → <b>{html.escape(new['fix'])}</b>"
                     + (f"\n<i>{html.escape(new['why'])}</i>" if new.get("why") else ""))
    upgrade = analysis.get("upgrade") or {}
    if upgrade.get("better"):
        lines.append(f"💬 <b>Оборот:</b> {html.escape(upgrade['said'])} → <b>{html.escape(upgrade['better'])}</b>"
                     + (f"\n<i>{html.escape(upgrade['why'])}</i>" if upgrade.get("why") else ""))
    if not lines:
        return None
    return "📝 <b>После разговора</b>\n\n" + "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Weekly report — what her speech looks like across conversations
# ---------------------------------------------------------------------------

REPORT_MIN_TURNS = 12          # below this there's nothing to count
REPORT_SESSIONS = 10           # how many recent conversations to look at
REPORT_MAX_PHRASES = 12        # candidates handed to Claude
_REPORT_MIN_COUNT = {1: 8, 2: 4, 3: 3, 4: 3}
# Counting is done in code on purpose: asked to spot "what you repeat", a model invents
# plausible answers. Claude only suggests alternatives for phrases the counter found.
_REPORT_STOPWORDS = {
    "que", "de", "la", "el", "y", "a", "en", "un", "una", "es", "no", "sí", "si", "me", "mi", "te", "tu",
    "se", "lo", "los", "las", "por", "para", "con", "muy", "más", "pero", "como", "ya", "yo", "o", "al",
    "del", "eh", "este", "esta", "bien", "también", "porque", "cuando", "hay", "son", "soy", "ser", "estar",
}
_TOKEN_RE = re.compile(r"[a-záéíóúüñ]+", re.IGNORECASE)


def count_phrases(sessions) -> list[dict]:
    """Her repeated words and phrases across recent conversations: how often, and in how
    many different conversations (a phrase used a lot in one story isn't a habit)."""
    counts, in_sessions = {}, {}
    for session in sessions:
        seen_here = set()
        for turn in session.get("transcript") or []:
            if turn.get("role") != "user":
                continue
            tokens = [t.lower() for t in _TOKEN_RE.findall(turn.get("text") or "")]
            for n in (1, 2, 3, 4):
                for i in range(len(tokens) - n + 1):
                    gram = tuple(tokens[i:i + n])
                    if n == 1 and (gram[0] in _REPORT_STOPWORDS or len(gram[0]) < 4):
                        continue
                    if n > 1 and all(t in _REPORT_STOPWORDS for t in gram):
                        continue
                    phrase = " ".join(gram)
                    counts[phrase] = counts.get(phrase, 0) + 1
                    seen_here.add(phrase)
        for phrase in seen_here:
            in_sessions[phrase] = in_sessions.get(phrase, 0) + 1

    candidates = [
        {"phrase": p, "count": c, "sessions": in_sessions.get(p, 0)}
        for p, c in counts.items()
        if c >= _REPORT_MIN_COUNT[len(p.split())] and (len(p.split()) == 1 or in_sessions.get(p, 0) >= 2)
    ]
    # a longer phrase beats the shorter one inside it when they're equally frequent
    by_count = sorted(candidates, key=lambda x: (-x["count"], -len(x["phrase"])))
    kept = []
    for cand in by_count:
        if any(cand["phrase"] in k["phrase"] and cand["count"] <= k["count"] for k in kept):
            continue
        kept.append(cand)
    return kept[:REPORT_MAX_PHRASES]


REPORT_PROMPT = """You write part of a weekly speech report for a Russian-speaking learner of Spanish (A1–A2, a woman) who talks with a Spanish-speaking bot by voice. Explanations in Russian, Spanish stays in Spanish.

You get phrases she repeats, counted exactly from her real conversations (don't recount or invent).

"overused" — the repeated phrases worth diversifying. Keep her phrase exactly as given.
- Only crutch-like expressions worth varying: openers, fillers, "pienso que", "me gusta", "es bueno", "muy bien". Skip words she simply talks about a lot (her cats, her work, place names) and skip fragments that are just grammar ("de la", "que el").
- "alternatives": 2–3 ways to say the same thing, A2–B1, natural, different in structure — not synonyms of one word only.
- "note": a few words in Russian on when to use them.
- At most 2 phrases, most repeated first. If nothing is really a crutch, return an empty list."""

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "overused": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "phrase": {"type": "string"},
                    "alternatives": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["phrase", "alternatives", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overused"],
    "additionalProperties": False,
}
REPORT_MAX_OVERUSED = 2


def build_weekly_report(user_id: int) -> str | None:
    """Sync (blocking) — call through asyncio.to_thread. Rebuilt 2026-10-11 around the focus
    loop: foci closed this week, the current one, and 1–2 phrases she leans on. The old «recurring
    mistakes» block is gone — a repeated mistake now becomes the focus by itself. None when
    there's nothing honest to say."""
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    done = db.get_focus_done_since(user_id, week_ago)
    active = db.get_active_focus(user_id)

    overused, counts = [], {}
    sessions = db.get_recent_voice_sessions(user_id, REPORT_SESSIONS)
    her_turns = [t for s in sessions for t in (s.get("transcript") or []) if t.get("role") == "user" and t.get("text")]
    phrases = count_phrases(sessions) if len(her_turns) >= REPORT_MIN_TURNS else []
    if phrases:
        lines = [f"- «{p['phrase']}» — {p['count']} раз в {p['sessions']} разговорах" for p in phrases]
        response = ai_helper.client.messages.create(
            model=TALK_LLM_MODEL,
            max_tokens=1200,
            system=REPORT_PROMPT,
            messages=[{"role": "user", "content": "Повторяющиеся фразы (посчитано точно):\n" + "\n".join(lines)}],
            output_config={"format": {"type": "json_schema", "schema": REPORT_SCHEMA}},
        )
        data = json.loads(next((b.text for b in response.content if b.type == "text"), "{}"))
        counts = {p["phrase"]: p for p in phrases}
        overused = [o for o in (data.get("overused") or []) if o.get("phrase") in counts][:REPORT_MAX_OVERUSED]

    if not done and not active and not overused:
        return None
    out = ["📊 <b>Неделя разговоров</b>"]
    if done:
        out += ["", "✅ <b>Фокусы сняты</b>"]
        out += [f"• {html.escape(f['rule'])}: «{html.escape(f['right_form'])}»" for f in done]
    if active:
        out += ["", "🎯 <b>Сейчас в работе</b>",
                f"• {html.escape(active['rule'])}: <s>{html.escape(active['wrong'])}</s> → "
                f"<b>{html.escape(active['right_form'])}</b> ({active['streak']} из {FOCUS_DONE_STREAK})"]
    if overused:
        out += ["", "🔁 <b>Часто повторяешь</b>"]
        for o in overused:
            c = counts[o["phrase"]]
            out.append(
                f"• «{html.escape(o['phrase'])}» — {c['count']} раз\n"
                f"  Вместо: {html.escape(', '.join(o['alternatives']))}\n"
                f"  <i>{html.escape(o['note'])}</i>"
            )
    return "\n".join(out)


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


def track_word_use(session: dict, user_text: str, reply: str, used_words=()) -> dict:
    """Who said each target word first — she (retrieval) or the bot (a hint she then reused).
    Retrieval practice is what makes a word stick, but an unsuccessful attempt followed
    right away by the word works too — this is how we tell the two apart over time.

    used_words: Claude's own list of target words she used this turn, in any form — it
    catches what the form match below can't ("fui" for "ir"). Only names from the target
    list count; the form match stays as a fallback."""
    use = dict(session.get("word_use") or {})
    claimed = {str(w).strip().lower() for w in used_words or ()}
    for word in session.get("target_words") or []:
        phrase = word["phrase"]
        state = use.get(phrase)
        if state in ("spontaneous", "after_hint"):
            continue
        if phrase.lower() in claimed or _word_used(phrase, user_text):
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

    scene = db.get_scene(session["scene_id"]) if session.get("scene_id") else None
    title = f"🖼 <b>«{html.escape(scene['title'])}»</b>" if scene else "🎙 <b>Разговор сохранён</b>"
    lines = [
        f"{title} — {minutes} мин, твоих реплик: {user_turns}.",
        "",
        "💪 Вспомнила сама: " + (html.escape(", ".join(spontaneous)) if spontaneous else "—"),
        "💡 Сказала после подсказки: " + (html.escape(", ".join(after_hint)) if after_hint else "—"),
        "▫️ Не прозвучали: " + (html.escape(", ".join(unused)) if unused else "—"),
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
    api.router.add_get("/talk/scene/{scene_id}/image", handle_scene_image)
