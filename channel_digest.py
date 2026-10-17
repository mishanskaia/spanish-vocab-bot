"""
One useful post a day from public Spanish-learning Telegram channels (owner-only, 2026-10-17).

Ольга is subscribed to a handful of channels whose archives already cover most of the useful
phrases and grammar — a «post of the day» would only skim the newest. So this keeps a library:

1. sync — read each channel's public web preview (t.me/s/<name>, no login, 20 posts a page,
   paged back with ?before=<id>); new posts on every run, plus the archive going back until
   POSTS_PER_CHANNEL posts are stored. Only the text is kept: a picture in a post is skipped
   (if the text is just a caption, labelling scores the post low). Polls are skipped.
2. label — Claude Haiku splits every post once into items, each one day's note: a phrase, a
   grammar point, or three related words. A post listing 16 linking words becomes 16 items (her
   idea: one at a time, days apart, each with examples). Each item: key, level, usefulness for
   everyday speech (1–5). Ads, quizzes, reading texts → no items.
3. pick — every morning, right after Study Coach: the best item not shown yet, levels A2–B1,
   kinds taking turns, items of one post at least POST_GAP_DAYS apart, skipping what she already
   has (vocabulary, catalog «знаю», the phrases of the voice conversations, keys already shown).
   One explanation per key: once an item is shown, items on the same key elsewhere retire.
4. compose — Claude Sonnet writes about that one item in its own words (never a copy — the posts
   are their authors' work) with 2–3 examples and a link to the original post; buttons: add to
   the vocabulary, make it the phrase of the voice conversations, «знаю».

Sync/label/compose are sync (urllib, blocking Anthropic client) — call through asyncio.to_thread.
"""
import html
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import ai_helper
import db
import word_catalog

logger = logging.getLogger(__name__)

CHANNELS = [
    "spanishfrom0",
    "sasha_studies_languages_spanish",
    "UchimSpanishu",
    "espanol_spanish_castellano_0",
    "espanol_spanish_castellano",
    "casadeleonline",
    "mucho_lirili_learn_spanish",
]
POSTS_PER_CHANNEL = 200        # archive depth for the first test; raise (or None) for the whole history
PAGES_PER_RUN = 15             # politeness cap per channel per run; the backfill resumes next run
NEW_PAGES_MAX = 10             # how far back a run looks for posts newer than the stored ones
REQUEST_PAUSE_S = 1.0
MIN_TEXT_CHARS = 60            # shorter than this there is nothing to learn from
LABEL_BATCH = 15
LEVELS = ("A2", "B1")          # her choice: A2–B1
MIN_SCORE = 3
KIND_ORDER = ("phrase", "grammar", "words")  # taking turns, day by day
POST_GAP_DAYS = 4             # items of one post (a list of 16 linking words) come at least this far apart

LABEL_MODEL = os.environ.get("CHANNEL_LABEL_MODEL", "claude-haiku-4-5")
COMPOSE_MODEL = os.environ.get("CHANNEL_COMPOSE_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Reading the web preview
# ---------------------------------------------------------------------------

class _PageParser(HTMLParser):
    """Posts of one t.me/s page: id, text of the post body (not the quoted reply), date,
    whether it has a photo or a poll. Text keeps line breaks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self._post = None
        self._text_depth = 0   # >0 while inside the post's own text div
        self._div_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div":
            self._div_depth += 1
            if "tgme_widget_message " in cls + " " and a.get("data-post"):
                post_id = a["data-post"].rsplit("/", 1)[-1]
                if post_id.isdigit():
                    self._post = {"post_id": int(post_id), "text": "", "posted_at": "", "has_photo": False, "poll": False}
                    self.posts.append(self._post)
            elif self._post is not None and "js-message_text" in cls and "reply" not in cls and not self._text_depth:
                self._text_depth = self._div_depth
            if self._post is not None and "tgme_widget_message_poll" in cls:
                self._post["poll"] = True
        if self._post is None:
            return
        if tag == "a" and "tgme_widget_message_photo" in cls:
            self._post["has_photo"] = True
        if tag == "time" and a.get("datetime") and not self._post["posted_at"]:
            self._post["posted_at"] = a["datetime"]
        if tag == "br" and self._text_depth:
            self._post["text"] += "\n"

    def handle_endtag(self, tag):
        if tag == "div":
            if self._text_depth and self._div_depth == self._text_depth:
                self._text_depth = 0
            self._div_depth -= 1

    def handle_data(self, data):
        if self._text_depth and self._post is not None:
            self._post["text"] += data


def parse_page(page: str) -> list[dict]:
    parser = _PageParser()
    parser.feed(page)
    for p in parser.posts:
        p["text"] = re.sub(r"\n{3,}", "\n\n", p["text"]).strip()
    return parser.posts


def _fetch(channel: str, before: int | None = None) -> str:
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def _store(channel: str, posts) -> int:
    rows = []
    for p in posts:
        skip = p["poll"] or len(p["text"]) < MIN_TEXT_CHARS
        rows.append({**p, "status": "skip" if skip else "new"})
    return db.add_channel_posts(channel, rows)


def sync_channel(channel: str, fetch=_fetch) -> int:
    """New posts since the last run, then one more stretch of the archive. Returns how many
    posts were added."""
    added = 0
    newest = db.get_channel_newest_id(channel)
    state = db.get_channel_state(channel)
    oldest = state["oldest_id"]

    # the newest page, and further back only while it's all new to us
    before = None
    for _ in range(NEW_PAGES_MAX):
        posts = parse_page(fetch(channel, before))
        if not posts:
            break
        added += _store(channel, posts)
        low = min(p["post_id"] for p in posts)
        oldest = low if oldest is None else min(oldest, low)
        if newest is None or low <= newest:
            break
        before = low
        time.sleep(REQUEST_PAUSE_S)

    done = bool(state["backfill_done"])
    pages = 0
    while not done and pages < PAGES_PER_RUN:
        if POSTS_PER_CHANNEL and db.count_channel_posts(channel) >= POSTS_PER_CHANNEL:
            done = True
            break
        time.sleep(REQUEST_PAUSE_S)
        posts = parse_page(fetch(channel, oldest))
        pages += 1
        posts = [p for p in posts if p["post_id"] < oldest]
        if not posts:
            done = True  # the channel's first post
            break
        added += _store(channel, posts)
        oldest = min(p["post_id"] for p in posts)
    db.set_channel_state(channel, oldest, done)
    return added


# ---------------------------------------------------------------------------
# Labelling — a post becomes items
# ---------------------------------------------------------------------------

LABEL_PROMPT = """You sort posts from Telegram channels for learners of Spanish into items for a daily note. The reader is a Russian-speaking woman at level A2 who wants Spanish she can use when she talks: ready phrases, constructions, everyday grammar. Each item will be one day's note, explained on its own with examples.

For every post return "id" (as given) and "items":
- A post on one expression or construction → one "phrase" item ("¿Qué más da?", "tener ganas de").
- A post listing several phrases (16 linking words, 10 ways to say goodbye) → one "phrase" item PER phrase, each on its own. At most 20; skip the weak ones.
- A grammar point (ser/estar, por/para, a tense) → one "grammar" item. A post with several separate grammar points → one per point.
- A vocabulary list on a topic (at the airport, the kitchen) → "words" items of 3 closely related, most useful words each ("el vuelo, el asiento, la azafata"). At most 4 such items; skip rare words.
- Ads, announcements, tours, polls and quizzes without an explanation, a reading text or a literary excerpt without a learning point, a caption where the content is in a picture → no items (empty list).

Each item:
- "kind": "phrase", "grammar" or "words".
- "es": the Spanish, short, in dictionary form: the expression ("qué más da", "por supuesto"), the grammar point ("ser / estar"), or the 3 words separated by commas (nouns with their article).
- "ru": a few Russian words on what it is («мне всё равно», «ser и estar», «полёт, место, стюардесса»).
- "level": of the item itself: A1, A2, B1, B2 or C1.
- "score": 1–5, how useful it is for her everyday speaking right now. 5 — frequent, natural, usable in many conversations; 3 — useful but narrow; 1 — rare or bookish.

Return every post, in the same order."""

LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": ["phrase", "grammar", "words"]},
                                "es": {"type": "string"},
                                "ru": {"type": "string"},
                                "level": {"type": "string", "enum": ["A1", "A2", "B1", "B2", "C1"]},
                                "score": {"type": "integer"},
                            },
                            "required": ["kind", "es", "ru", "level", "score"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["posts"],
    "additionalProperties": False,
}

LABEL_TEXT_CHARS = 2500        # long lists need their tail too
MAX_ITEMS_PER_POST = 20


def make_key(es: str) -> str:
    """The same item from different channels should meet: case, edge punctuation and a leading
    article don't matter ("¿Qué más da?" = "qué más da"; "el aeropuerto" = "aeropuerto")."""
    key = word_catalog.normalize(es)
    return re.sub(r"\s+", " ", key.replace("/", " / ")).strip()


def item_parts(item: dict) -> list[str]:
    """The keys inside an item — one for a phrase or a grammar point, three for a word group."""
    if item["kind"] != "words":
        return [item["key"]]
    return [make_key(w) for w in item["es"].split(",") if w.strip()]


def _clean_items(raw) -> list[dict]:
    items, seen = [], set()
    for x in raw or []:
        if not isinstance(x, dict) or x.get("kind") not in ("phrase", "grammar", "words"):
            continue
        es = str(x.get("es") or "").strip()[:120]
        key = make_key(es)
        if not es or not key or key in seen:
            continue
        seen.add(key)
        try:
            score = max(1, min(5, int(x.get("score") or 1)))
        except (TypeError, ValueError):
            score = 1
        items.append({"kind": x["kind"], "es": es, "ru": str(x.get("ru") or "").strip() or None,
                      "key": key, "level": x.get("level"), "score": score})
    return items[:MAX_ITEMS_PER_POST]


def label_batch(posts) -> int:
    content = "\n\n".join(
        f"=== id {p['id']} (@{p['channel']})\n{(p['text'] or '')[:LABEL_TEXT_CHARS]}" for p in posts
    )
    response = ai_helper.client.messages.create(
        model=LABEL_MODEL,
        max_tokens=8000,
        system=LABEL_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": LABEL_SCHEMA}},
    )
    data = json.loads(next((b.text for b in response.content if b.type == "text"), "{}"))
    by_id = {int(x["id"]): x for x in data.get("posts") or [] if isinstance(x, dict) and "id" in x}
    for p in posts:
        x = by_id.get(p["id"])
        # not returned → no items, so it isn't relabelled forever
        db.set_channel_post_items(p["id"], _clean_items(x.get("items")) if x else [])
    return len(posts)


def label_pending(max_posts: int = 2000) -> int:
    done = 0
    while done < max_posts:
        batch = db.get_unlabeled_channel_posts(LABEL_BATCH)
        if not batch:
            break
        try:
            done += label_batch(batch)
        except Exception:
            logger.exception("channel digest: labelling batch failed")
            break
    return done


def refresh(fetch=_fetch) -> dict:
    """Sync every channel, then label what's new. A channel that fails doesn't stop the rest."""
    added = {}
    for channel in CHANNELS:
        try:
            added[channel] = sync_channel(channel, fetch)
        except Exception:
            logger.exception("channel digest: sync of %s failed", channel)
    labeled = label_pending()
    return {"added": added, "labeled": labeled}


# ---------------------------------------------------------------------------
# Picking
# ---------------------------------------------------------------------------

def _known_keys(user_id: int) -> set:
    """Everything she already has, as keys comparable with make_key()."""
    keys = {make_key(r["phrase"]) for r in db.get_user_words(user_id)}
    keys |= {make_key(k) for k in db.get_catalog_known(user_id)}
    keys |= {make_key(c) for c in db.get_recent_chunks(user_id, 1000)}
    return keys | db.get_channel_shown_keys()


def pick(user_id: int, now: datetime | None = None):
    """Today's item, or None. Kinds take turns: after a phrase comes grammar, then words; when
    today's kind has nothing left, the next one in line. Items of a post shown in the last
    POST_GAP_DAYS days wait, so a list of 16 linking words comes one at a time, days apart."""
    now = now or datetime.now(timezone.utc)
    known = _known_keys(user_id)
    resting = db.get_channel_posts_shown_since((now - timedelta(days=POST_GAP_DAYS)).isoformat())
    candidates = [
        c for c in db.get_channel_candidates(LEVELS, MIN_SCORE)
        if c["key"] not in known and c["post_row"] not in resting
        # a word group goes only while at least one of its words is new to her
        and not all(k in known for k in item_parts(c))
    ]
    if not candidates:
        return None
    last = db.get_last_shown_channel_item()
    start = (KIND_ORDER.index(last["kind"]) + 1) % len(KIND_ORDER) if last and last.get("kind") in KIND_ORDER else 0
    for i in range(len(KIND_ORDER)):
        kind = KIND_ORDER[(start + i) % len(KIND_ORDER)]
        of_kind = [c for c in candidates if c["kind"] == kind]
        if of_kind:
            # best score first (the query orders by it); among equals, not yesterday's channel
            best = of_kind[0]["score"]
            top = [c for c in of_kind if c["score"] == best]
            other_channel = [c for c in top if not last or c["channel"] != last["channel"]]
            chosen = (other_channel or top)[0]
            # a list post goes in its own order: the first of its items still waiting
            return min((c for c in of_kind if c["post_row"] == chosen["post_row"]), key=lambda c: c["id"])
    return None


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------

COMPOSE_PROMPT = """You write a short daily note about ONE Spanish item for a Russian-speaking woman at level A2 who wants Spanish she can use when she talks (she aims at Latin American Spanish). Explanations in Russian, Spanish stays in Spanish.

You get the item and the Telegram post it comes from. The post may cover many things — write only about this item, in your own words; never copy the post. If the post has a mistake in Spanish, quietly give the correct form.

- "title": the item in Spanish, short ("¿Qué más da?", "ser y estar", "el vuelo, el asiento, la azafata").
- "gist": 1–3 short sentences in Russian: what it means or how it works, and when to say it. If it is typical only of Spain or only of Latin America, say so.
- "examples": 2–3 natural everyday sentences, A2–B1, each {"es", "ru"}, each using the item (for three words — each word at least once).
- "add_es": the Spanish worth adding to her vocabulary, in dictionary form (nouns with their article, verbs in the infinitive): for a phrase — the phrase itself; for three words — the most useful one; for a grammar point — "".
- "chunk": the item itself if it is a reusable phrase for speech that fits almost any conversation ("me da igual", "tener ganas de", "por eso"); otherwise ""."""

COMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "gist": {"type": "string"},
        "examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"es": {"type": "string"}, "ru": {"type": "string"}},
                "required": ["es", "ru"],
                "additionalProperties": False,
            },
        },
        "add_es": {"type": "string"},
        "chunk": {"type": "string"},
    },
    "required": ["title", "gist", "examples", "add_es", "chunk"],
    "additionalProperties": False,
}

KIND_RU = {"phrase": "фраза", "grammar": "грамматика", "words": "слова"}


def compose(item: dict) -> dict:
    content = (f"Пункт ({KIND_RU.get(item['kind'], item['kind'])}): {item['es']}"
               + (f" — {item['ru']}" if item.get("ru") else "")
               + f"\n\nПост из канала @{item['channel']}:\n\n{item['post_text']}")
    response = ai_helper.client.messages.create(
        model=COMPOSE_MODEL,
        max_tokens=1200,
        system=COMPOSE_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": COMPOSE_SCHEMA}},
    )
    data = json.loads(next((b.text for b in response.content if b.type == "text"), "{}"))
    examples = [e for e in data.get("examples") or [] if isinstance(e, dict) and e.get("es")][:3]
    return {
        "title": str(data.get("title") or item["es"]).strip(),
        "gist": str(data.get("gist") or "").strip(),
        "examples": [{"es": str(e["es"]).strip(), "ru": str(e.get("ru") or "").strip()} for e in examples],
        "add_es": str(data.get("add_es") or "").strip()[:80],
        "chunk": str(data.get("chunk") or "").strip()[:60],
    }


def post_link(item: dict) -> str:
    return f"https://t.me/{item['channel']}/{item['post_id']}"


def format_message(item: dict, digest: dict) -> str:
    lines = [f"📌 <b>{html.escape(digest['title'])}</b>"]
    if digest.get("gist"):
        lines += ["", html.escape(digest["gist"])]
    if digest.get("examples"):
        lines.append("")
        lines += [f"• <b>{html.escape(e['es'])}</b>" + (f" — {html.escape(e['ru'])}" if e.get("ru") else "")
                  for e in digest["examples"]]
    lines += ["", f'<a href="{post_link(item)}">Источник: @{html.escape(item["channel"])}</a>']
    return "\n".join(lines)
