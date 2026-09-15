"""
Проверка распознавания речи (STT) перед «Вечерней практикой» — см. TODO.md, P0.

Вопрос, на который отвечает скрипт: сохраняет ли OpenAI в транскрипте ошибки учащегося
(неверное спряжение, род, русское слово посреди фразы, невнятное слово) — или «причёсывает» их
до правильного испанского. Если причёсывает, разбор Claude ошибку не увидит.

Как пользоваться:
  1. pip install openai
  2. В .env добавить строку OPENAI_API_KEY=sk-...  (.env в .gitignore, в git не попадёт)
  3. Положить голосовые в tests/voice_samples/  (папка в .gitignore — голос в git не попадёт).
     Из Telegram Desktop: правый клик по голосовому → «Сохранить как» → .ogg
  4. Рядом с каждым файлом можно положить .txt с тем, что ты РЕАЛЬНО сказала, ошибками включительно:
     voice_1.ogg + voice_1.txt  → скрипт покажет их рядом для сравнения.
  5. python tests/stt_check.py

Каждый файл прогоняется через несколько моделей распознавания — они по-разному склонны
исправлять речь, и сравнение сразу показывает, какую брать.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = Path(__file__).resolve().parent / "voice_samples"
AUDIO_EXTENSIONS = {".ogg", ".oga", ".mp3", ".m4a", ".wav", ".webm"}

MODELS = ["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"]

# Подсказка модели: записывать дословно, не исправлять. Проверяем и с ней, и без неё —
# работает ли она вообще, заранее неизвестно.
VERBATIM_PROMPT = (
    "Transcripción literal de un estudiante de español de nivel A1. "
    "Escribe exactamente lo que dice, con sus errores gramaticales, "
    "sin corregir nada. Puede mezclar palabras en ruso."
)


def transcribe(client: OpenAI, path: Path, model: str, prompt: str | None) -> str:
    with path.open("rb") as f:
        kwargs = {"model": model, "file": f, "language": "es"}
        if prompt:
            kwargs["prompt"] = prompt
        try:
            result = client.audio.transcriptions.create(**kwargs)
        except Exception as e:  # отчёт, а не бот — любую ошибку просто показываем
            return f"[ошибка: {type(e).__name__}: {e}]"
    return result.text.strip()


def main():
    load_dotenv(ROOT / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("Нет OPENAI_API_KEY — добавь его в .env (см. инструкцию в начале файла).")

    files = sorted(p for p in SAMPLES_DIR.glob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)
    if not files:
        sys.exit(f"Нет аудиофайлов в {SAMPLES_DIR}")

    client = OpenAI()

    for path in files:
        print("=" * 70)
        print(f"🎙  {path.name}")
        expected = path.with_suffix(".txt")
        if expected.exists():
            print(f"   сказала на самом деле:  {expected.read_text(encoding='utf-8').strip()}")
        print()
        for model in MODELS:
            plain = transcribe(client, path, model, prompt=None)
            verbatim = transcribe(client, path, model, prompt=VERBATIM_PROMPT)
            print(f"   {model}")
            print(f"     без подсказки:    {plain}")
            print(f"     «дословно»:       {verbatim}")
        print()


if __name__ == "__main__":
    main()
