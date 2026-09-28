"""/scene — describe-a-picture prototype (owner only, 2026-09-28).

The problem it goes after: in /talk she steers the conversation to comfortable topics, so
words she recognises but doesn't use never come up (avoidance). A picture takes the topic
out of her hands — what's drawn has to be described.

Prototype scope is deliberately narrow: check the two unknowns before building a voice mode
around it — do her words group into sensible scenes, and are they recognisable in the
generated picture. So: pool → Claude proposes 3 themed scenes → she picks → one picture.
No conversation, no scoring yet.

Pool = words she knows or almost knows: cards at stage 1+ (status learning and above) plus
catalog words marked «знаю». Scenes are built theme-first — 3-4 words that plausibly meet
in one everyday situation, not a collage of random objects.
"""

import base64
import os
import random

import ai_helper
import db
import stt_helper
import word_catalog

POOL_PROMPT_LIMIT = 150  # words sent to Claude per request; shuffled so each /scene differs
MIN_POOL = 6
SCENE_COUNT = 3
MIN_SCENE_WORDS = 3
MAX_SCENE_WORDS = 4

IMAGE_MODEL = os.environ.get("SCENE_IMAGE_MODEL", "gpt-image-2")
IMAGE_QUALITY = os.environ.get("SCENE_IMAGE_QUALITY", "medium")


def build_pool(user_id: int) -> list[dict]:
    """[{es, ru}] — cards she has met in review plus catalog words marked «знаю»."""
    pool = {}
    for row in db.get_known_words(user_id, limit=1000):
        pool[word_catalog.normalize(row["phrase"])] = {"es": row["phrase"], "ru": row["meaning"]}
    catalog_ru = {word_catalog.normalize(w["es"]): w["ru"] for w in word_catalog.load_words()}
    for entry in db.get_catalog_known_entries(user_id):
        ru = catalog_ru.get(entry["key"])
        if ru and entry["key"] not in pool:  # a card wins over a bare mark
            pool[entry["key"]] = {"es": entry["es"], "ru": ru}
    return list(pool.values())


SCENES_SYSTEM = (
    "You design picture-description exercises for a Russian-speaking learner of Spanish (A1-A2). "
    "Reply with valid JSON only, no markdown."
)


def _scenes_prompt(words: list[dict]) -> str:
    listing = "\n".join(f"- {w['es']} — {w['ru']}" for w in words)
    return f"""Here are Spanish words she already knows (passively) but rarely uses when speaking:
{listing}

Propose {SCENE_COUNT} scenes. Each scene is ONE everyday situation that will be drawn as a single
picture, and she will describe the picture aloud in Spanish. The goal: describing the picture
naturally requires the scene's target words.

Rules:
- Each scene has {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} target words taken EXACTLY as written in the list above.
- The words of one scene must belong together in one real situation (a kitchen in the morning,
  a rainy bus stop, a doctor's waiting room). Never a collage of unrelated objects.
- Only words that can be SHOWN unambiguously: objects, visible actions, clearly visible states
  (a tired face, a wet street). Skip abstract words, connectors, grammar words.
- The {SCENE_COUNT} scenes must be different situations and use different words.
- Adult everyday life, not children's-book scenes.

For each scene give:
- "title": a short theme name in Russian (2-4 words), e.g. "Утро перед работой";
- "words": the target words, exactly as in the list;
- "picture": a description for an illustrator in English, 2-4 sentences: the setting and what is
  happening, one person at most two;
- "must_show": for each target word, in the same order, a short English phrase saying how it is
  visible in the picture (e.g. "a folded umbrella dripping by the door").

JSON: {{"scenes": [{{"title": "...", "words": ["..."], "picture": "...", "must_show": ["..."]}}]}}"""


def propose_scenes(pool: list[dict]) -> list[dict]:
    """Claude groups a shuffled slice of the pool into themed scenes. Every word it returns
    is checked against the pool (models do invent or re-spell words); a scene left with
    fewer than MIN_SCENE_WORDS real words is dropped."""
    sample = random.sample(pool, min(len(pool), POOL_PROMPT_LIMIT))
    data = ai_helper._ask_claude(_scenes_prompt(sample), max_tokens=3000, system=SCENES_SYSTEM)
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
    """PNG bytes. Same OpenAI client (and key) as speech — no separate setup."""
    result = stt_helper._get_client().images.generate(
        model=IMAGE_MODEL,
        prompt=image_prompt(scene),
        size="1024x1024",
        quality=IMAGE_QUALITY,
    )
    return base64.b64decode(result.data[0].b64_json)
