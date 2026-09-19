import os

from openai import OpenAI

# Speech-to-text for the evening practice (OpenAI — the Claude API doesn't take audio).
# Model and prompt picked by tests/stt_check.py on real voice notes (2026-09-15):
# gpt-4o-mini-transcribe kept deliberate learner errors ("yo fue", "el casa"), whisper-1
# silently fixed them. Without the verbatim prompt the gpt-4o models also *translated*
# a Russian word dropped mid-phrase into Spanish, hiding the vocabulary gap — so the
# prompt is required, not optional.
STT_MODEL = "gpt-4o-mini-transcribe"
VERBATIM_PROMPT = (
    "Transcripción literal de un estudiante de español de nivel A1. "
    "Escribe exactamente lo que dice, con sus errores gramaticales, "
    "sin corregir nada. Puede mezclar palabras en ruso."
)

_client = None


def _get_client() -> OpenAI:
    # Lazy: the bot must still start without OPENAI_API_KEY (the feature is just off then)
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


# Dictating a new word: Russian or Spanish, usually one word or a short phrase. No `language`
# parameter (it would force one of the two) — the model detects it, and the prompt says which
# two languages to expect so a short word isn't guessed as some third language.
WORD_PROMPT = (
    "Одно слово или короткая фраза на русском или на испанском языке. "
    "Una palabra o frase corta en ruso o en español."
)


def transcribe_word(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    result = _get_client().audio.transcriptions.create(
        model=STT_MODEL,
        file=(filename, audio_bytes),
        prompt=WORD_PROMPT,
    )
    # models tend to add sentence punctuation ("Счёт.") — dedup matches the phrase exactly
    return result.text.strip().strip(".,!?¿¡…\"«»").strip()


def transcribe(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    result = _get_client().audio.transcriptions.create(
        model=STT_MODEL,
        file=(filename, audio_bytes),
        language="es",
        prompt=VERBATIM_PROMPT,
    )
    return result.text.strip()


# Text-to-speech for the hands-free conversation (/talk). gpt-4o-mini-tts takes free-form
# delivery instructions; "normal pace" is deliberate — the first live test at a slowed-down
# speed was "painfully slow", and the page shows subtitles for anything too fast.
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "marin"
TTS_INSTRUCTIONS = (
    "Speak natural, clear Spanish at a normal conversational pace, "
    "warm and friendly, like chatting with a friend."
)


def synthesize(text: str) -> bytes:
    response = _get_client().audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        instructions=TTS_INSTRUCTIONS,
        response_format="mp3",
    )
    return response.content
