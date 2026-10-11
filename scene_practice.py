"""/scene — describe a picture built from words she knows (owner only, 2026-09-28).

The problem it goes after: in /talk she steers the conversation to comfortable topics, so
words she recognises but doesn't use never come up (avoidance). A picture takes the topic
out of her hands — what's drawn has to be described.

Flow: pool → Claude proposes 3 themed scenes → she picks → OpenAI draws one → Claude looks
at the actual picture (check_image): what's really drawn and which words can be seen → the
scene is saved (db.scenes) → «Описать голосом» opens the /talk page in picture mode
(voice_talk.build_scene_instructions): her description → questions about what she hasn't
named, with a hint ladder → her own life with the same words.

Pool = words she knows or almost knows: cards at stage 1+ (status learning and above) plus
catalog words marked «знаю». Scenes are built theme-first — 3-4 words that plausibly meet
in one everyday situation, not a collage of random objects.
"""

import base64
import json
import os
import random

import ai_helper
import db
import stt_helper
import word_catalog

# Rotation (2026-09-29): words that were never on a picture go to Claude first, then the ones
# described longest ago — however old the card itself is. Without it Claude got the whole pool
# every time and kept picking the same easy-to-draw words.
#
# Offered scenes count too (2026-10-11): a word that was offered but not drawn — the scene
# wasn't chosen, or the picture didn't show it — goes after the never-offered ones, so the
# same theme doesn't come straight back. And Claude sees the titles of the recent scenes.
POOL_PROMPT_LIMIT = 60
RECENT_TITLES = 15
MIN_POOL = 6
SCENE_COUNT = 3
MIN_SCENE_WORDS = 3
MAX_SCENE_WORDS = 4

IMAGE_MODEL = os.environ.get("SCENE_IMAGE_MODEL", "gpt-image-2")
IMAGE_QUALITY = os.environ.get("SCENE_IMAGE_QUALITY", "medium")


def build_pool(user_id: int) -> list[dict]:
    """[{es, ru, last_scene, last_offered}] — cards she has met in review plus catalog words
    marked «знаю». last_scene: when the word was last on a picture she got (ISO string), None
    if never. Only words that survived the vision check are stored with a scene, so a word
    that wasn't recognisable on its picture still counts as never described — but it does
    have last_offered (when it was last in any offered scene)."""
    last_scene = {}
    for words, created_at in db.get_scene_words_history(user_id):
        for w in words:
            key = word_catalog.normalize(w.get("es", ""))
            if created_at > last_scene.get(key, ""):
                last_scene[key] = created_at
    last_offered = {}
    for _title, words, created_at in db.get_scene_proposals(user_id):
        for es in words:
            key = word_catalog.normalize(es)
            if created_at > last_offered.get(key, ""):
                last_offered[key] = created_at
    pool = {}
    for row in db.get_known_words(user_id, limit=1000):
        pool[word_catalog.normalize(row["phrase"])] = {"es": row["phrase"], "ru": row["meaning"]}
    catalog_ru = {word_catalog.normalize(w["es"]): w["ru"] for w in word_catalog.load_words()}
    for entry in db.get_catalog_known_entries(user_id):
        ru = catalog_ru.get(entry["key"])
        if ru and entry["key"] not in pool:  # a card wins over a bare mark
            pool[entry["key"]] = {"es": entry["es"], "ru": ru}
    for key, word in pool.items():
        word["last_scene"] = last_scene.get(key)
        word["last_offered"] = last_offered.get(key)
    return list(pool.values())


def _rotation_key(w: dict) -> tuple:
    if w.get("last_scene"):
        return (2, w["last_scene"])            # described — longest ago first
    if w.get("last_offered"):
        return (1, w["last_offered"])          # offered, never drawn — offered longest ago first
    return (0, "")                             # never offered


def rotation_slice(pool: list[dict]) -> list[dict]:
    """Never-offered words first (shuffled, so «Другие сцены» gives a different set), then
    offered-but-never-drawn ones, then the ones described longest ago; the first
    POOL_PROMPT_LIMIT go to Claude."""
    shuffled = random.sample(pool, len(pool))
    shuffled.sort(key=_rotation_key)  # stable: ties keep the shuffle
    return shuffled[:POOL_PROMPT_LIMIT]


def recent_titles(user_id: int) -> list[str]:
    """Titles of the last scenes offered to her, newest first, without repeats."""
    titles = []
    for title, _words, _at in db.get_scene_proposals(user_id):
        if title not in titles:
            titles.append(title)
        if len(titles) >= RECENT_TITLES:
            break
    return titles


SCENES_SYSTEM = (
    "You design picture-description exercises for a Russian-speaking learner of Spanish (A1-A2). "
    "Reply with valid JSON only, no markdown."
)


def _mark(w: dict) -> str:
    if w.get("last_scene"):
        return " (already described)"
    if w.get("last_offered"):
        return " (offered before)"
    return ""


def _scenes_prompt(words: list[dict], recent=()) -> str:
    listing = "\n".join(f"- {w['es']} — {w['ru']}{_mark(w)}" for w in words)
    avoid = ""
    if recent:
        avoid = ("\nScenes she was offered recently — do NOT propose these situations again, not even "
                 "with other words or a reworded title (another trip-packing scene is still a repeat):\n"
                 + "\n".join(f"- {t}" for t in recent) + "\n")
    return f"""Here are Spanish words she already knows (passively) but rarely uses when speaking:
{listing}

Words marked "(already described)" were in one of her earlier pictures, words marked "(offered before)"
were in a scene she was already offered — use marked words only when a scene really needs them;
build the scenes from the unmarked words first.
{avoid}
Propose {SCENE_COUNT} scenes. Each scene is ONE everyday situation that will be drawn as a single
picture, and she will describe the picture aloud in Spanish. The goal: describing the picture
naturally requires the scene's target words.

Rules:
- Each scene has {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} target words taken EXACTLY as written in the list above.
- The words of one scene must belong together in one real situation (a kitchen in the morning,
  a rainy bus stop, a doctor's waiting room). Never a collage of unrelated objects.
- Don't play it safe with objects only. Actions (cocinar, esperar, correr) and feelings or states
  (cansada, preocupado, aburrido) can be drawn too — through a pose, a face, what is happening around
  the person. When the list has such words: try to give every scene at least one action or feeling,
  and build at least one scene where they are the core (someone waiting nervously, someone laughing
  while cooking). Objects are fine, but a scene of nothing but objects is the easy way out.
- Skip only what can't be drawn at all: connectors, grammar words, purely abstract ideas.
- The {SCENE_COUNT} scenes must be different situations and use different words.
- Adult everyday life, not children's-book scenes.

For each scene give:
- "title": a short theme name in Russian (2-4 words), e.g. "Утро перед работой";
- "words": the target words, exactly as in the list;
- "picture": a description for an illustrator in English, 2-4 sentences: the setting and what is
  happening, one person at most two;
- "must_show": for each target word, in the same order, a short English phrase saying how it is
  visible in the picture (e.g. "a folded umbrella dripping by the door"). For an action or a feeling,
  make it unmistakable: the body and face, plus the situation that explains it ("she frowns at her
  phone, hand on her forehead, a missed bus driving away" for preocupada).

JSON: {{"scenes": [{{"title": "...", "words": ["..."], "picture": "...", "must_show": ["..."]}}]}}"""


def propose_scenes(pool: list[dict], recent=()) -> list[dict]:
    """Claude groups the rotation slice of the pool into themed scenes. Every word it returns
    is checked against the pool (models do invent or re-spell words); a scene left with
    fewer than MIN_SCENE_WORDS real words is dropped. recent: titles of the last offered
    scenes, which Claude must not repeat."""
    sample = rotation_slice(pool)
    data = ai_helper._ask_claude(_scenes_prompt(sample, recent), max_tokens=3000, system=SCENES_SYSTEM)
    by_es = {w["es"].lower(): w for w in sample}
    scenes = []
    for raw in data.get("scenes") or []:
        words = raw.get("words") or []
        shows = raw.get("must_show") or []
        picked, visible, seen = [], [], set()
        for i, es in enumerate(words):
            word = by_es.get(str(es).strip().lower())
            if word is None or word["es"] in seen:
                continue
            seen.add(word["es"])
            picked.append(word)
            if i < len(shows) and shows[i]:
                visible.append(str(shows[i]))
        title, picture = str(raw.get("title") or "").strip(), str(raw.get("picture") or "").strip()
        if len(picked) >= MIN_SCENE_WORDS and title and picture:
            scenes.append({
                "title": title,
                "words": picked[:MAX_SCENE_WORDS],
                "picture": picture,
                "must_show": visible,
            })
    return scenes[:SCENE_COUNT]


def image_prompt(scene: dict) -> str:
    items = "\n".join(f"- {s}" for s in scene["must_show"])
    return (
        f"{scene['picture']}\n\n"
        f"These must be clearly visible and easy to recognise:\n{items}\n\n"
        "Style: clean, warm, slightly stylised illustration, uncluttered composition, "
        "everything important large enough to name at a glance on a phone screen. "
        # text in the picture would hand her the answer
        "Absolutely no text anywhere: no words, letters, labels, signs, captions or numbers."
    )


def generate_image(scene: dict) -> bytes:
    """JPEG bytes (a few hundred KB instead of a ~2 MB PNG — it's stored in the DB for the
    voice mode). Same OpenAI client (and key) as speech — no separate setup."""
    result = stt_helper._get_client().images.generate(
        model=IMAGE_MODEL,
        prompt=image_prompt(scene),
        size="1024x1024",
        quality=IMAGE_QUALITY,
        output_format="jpeg",
        output_compression=85,
    )
    return base64.b64decode(result.data[0].b64_json)


# ---------------------------------------------------------------------------
# Vision check — the picture is drawn from a description, and the generator doesn't
# always draw everything (first live test: 3 of 4 words guessable, the 4th "non-trivial").
# Claude looks at the actual picture once: what's really there becomes the conversation's
# ground truth, and a word that can't be seen is dropped instead of asked about.
# ---------------------------------------------------------------------------

MIN_VISIBLE_WORDS = 2

CHECK_SYSTEM = (
    "You check pictures for a Spanish picture-description exercise. A learner (A1-A2) will describe "
    "the picture aloud; a conversation partner who can't see it will ask her about it from your notes."
)

CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "seen": {"type": "string"},
        "words": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "es": {"type": "string"},
                    "visible": {"type": "boolean"},
                    "how": {"type": "string"},
                },
                "required": ["es", "visible", "how"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["seen", "words"],
    "additionalProperties": False,
}


def check_image(scene: dict, image: bytes) -> dict:
    """Sync (blocking) — call through asyncio.to_thread. Returns {"seen", "words"}: words
    are the scene's words that really are recognisable, each with "shown" — where/how."""
    listing = "\n".join(f"- {w['es']} — {w['ru']}" for w in scene["words"])
    response = ai_helper.client.messages.create(
        model=ai_helper.MODEL,
        max_tokens=1500,
        system=CHECK_SYSTEM,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(image).decode(),
            }},
            {"type": "text", "text": f"""Target words:
{listing}

1. "seen": describe what is actually in the picture, in English, 3-6 plain sentences: the setting, the people
   and what they are doing, the objects that stand out and where they are. Only what is really drawn.
2. "words": for each target word (copy "es" exactly), "visible" and "how" (where and how it appears,
   English, a few words — or why not).
   - Objects: "visible" is true only if a learner would naturally name the thing with this word — not if
     it's tiny, ambiguous or better named by a different word.
   - Actions, feelings and states: "visible" is true if the picture shows it so that this word is a
     natural way to name it, even if a synonym would fit too (a worried face may be "preocupada" or
     "nerviosa" — fine; the conversation accepts synonyms). False only if it isn't shown at all."""},
        ]}],
        output_config={"format": {"type": "json_schema", "schema": CHECK_SCHEMA}},
    )
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    data = json.loads(text)
    verdicts = {str(v.get("es", "")).strip().lower(): v for v in data.get("words") or []}
    words = []
    for w in scene["words"]:
        v = verdicts.get(w["es"].lower())
        if v and v.get("visible"):
            words.append({"es": w["es"], "ru": w["ru"], "shown": str(v.get("how") or "").strip()})
    return {"seen": str(data.get("seen") or "").strip(), "words": words}
