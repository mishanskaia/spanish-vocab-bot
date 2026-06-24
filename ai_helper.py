import os
import json
from anthropic import Anthropic

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = (
    "Ты — помощник по изучению испанского языка для начинающих (уровень A1-A2). "
    "Объясняй слова на русском языке. "
    "Отвечай ТОЛЬКО валидным JSON без markdown и без вступления."
)


def _ask_claude(prompt: str):
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def explain_word(word: str) -> dict:
    prompt = f"""Объясни испанское слово или фразу: "{word}"

Важные правила:
- Если это существительное — ОБЯЗАТЕЛЬНО включи артикль (el/la): например "la casa", "el coche"
- Если это глагол — ОБЯЗАТЕЛЬНО включи спряжение в настоящем времени (presente de indicativo)
- Уровень объяснений — для начинающего (A1-A2)
- Все объяснения и переводы — на русском языке

Верни JSON строго в таком формате:
{{
  "phrase": "слово с артиклем если существительное, или инфинитив если глагол",
  "meaning": "перевод на русском (1-2 слова или короткая фраза)",
  "part_of_speech": "существительное" | "глагол" | "прилагательное" | "наречие" | "предлог" | "местоимение" | "другое",
  "cefr_level": "A1" | "A2",
  "examples": ["пример предложения 1 (испанский — русский перевод)", "пример предложения 2 (испанский — русский перевод)"],
  "conjugation": null
}}

Если это глагол — поле conjugation должно быть строкой с таблицей спряжения:
"yo ..., tú ..., él/ella ..., nosotros ..., ellos/ellas ..."

Если не глагол — conjugation = null"""
    return _ask_claude(prompt)


def find_frequent_words(existing_words: list, count: int = 10) -> list:
    existing_str = ", ".join(existing_words) if existing_words else "нет"
    prompt = f"""Подбери {count} испанских слов уровня A1-A2, которых ещё нет у пользователя.

Уже добавленные слова (не повторяй): {existing_str}

Требования:
- Пользователь прошёл около 5 уроков испанского — базовые приветствия и числа уже знает
- Подбирай практичную повседневную лексику: еда, транспорт, время, эмоции, действия, описания
- Разнообразный микс: существительные, глаголы, прилагательные, наречия
- Для существительных ОБЯЗАТЕЛЬНО с артиклем (la casa, el coche)
- Для глаголов ОБЯЗАТЕЛЬНО спряжение в настоящем времени
- Все объяснения на русском языке

Верни JSON-массив:
[
  {{
    "phrase": "слово (с артиклем для существительных)",
    "meaning": "перевод на русском",
    "part_of_speech": "существительное" | "глагол" | "прилагательное" | "наречие" | "предлог" | "местоимение" | "другое",
    "cefr_level": "A1" | "A2",
    "examples": ["пример 1 (испанский — русский)", "пример 2 (испанский — русский)"],
    "conjugation": "yo ..., tú ..., él/ella ..., nosotros ..., ellos/ellas ..." или null
  }}
]"""
    result = _ask_claude(prompt)
    return result if isinstance(result, list) else []
