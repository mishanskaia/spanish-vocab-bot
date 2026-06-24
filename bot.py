import os
import re
import json
import random
import logging
from datetime import time as dtime

from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

import db
import ai_helper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
REMINDER_HOUR_UTC = int(os.environ.get("REMINDER_HOUR_UTC", "8"))


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу тебе учить испанские слова 🇪🇸\n\n"
        "Просто напиши любое испанское слово — я объясню и сохраню его.\n\n"
        "/words — добавить 10 частотных слов автоматически\n"
        "/review — повторить слова по расписанию\n"
        "/gender — угадать род существительных\n"
        "/delete — удалить слово из базы\n"
        "/stats — статистика словаря"
    )


# ---------------------------------------------------------------------------
# Добавление слова через обычное сообщение
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = update.message.text.strip()
    if not word or word.startswith("/"):
        return

    await update.message.reply_text("Секунду, ищу...")

    info = ai_helper.explain_word(word)
    db.add_word(
        user_id=update.effective_user.id,
        phrase=info.get("phrase", word),
        meaning=info.get("meaning", ""),
        part_of_speech=info.get("part_of_speech", ""),
        cefr_level=info.get("cefr_level", ""),
        examples=info.get("examples", []),
        conjugation=info.get("conjugation"),
    )

    examples_text = "\n".join(f"• {e}" for e in info.get("examples", []))
    conj = info.get("conjugation")
    conj_block = f"\n\n📝 Спряжение: {conj}" if conj else ""

    await update.message.reply_text(
        f'✅ *{info.get("phrase", word)}*\n'
        f'{info.get("meaning", "")}\n'
        f'_{info.get("part_of_speech", "")} · {info.get("cefr_level", "")}_\n\n'
        f'Примеры:\n{examples_text}'
        f'{conj_block}\n\n'
        f'Первое повторение — завтра.',
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /words — 10 частотных слов
# ---------------------------------------------------------------------------

async def words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Подбираю 10 частотных слов...")

    existing = db.get_user_words(update.effective_user.id)
    new_words = ai_helper.find_frequent_words(existing, count=10)

    if not new_words:
        await update.message.reply_text("Не удалось подобрать слова. Попробуй ещё раз.")
        return

    context.user_data["words_queue"] = new_words
    context.user_data["words_index"] = 0
    await _send_words_item(update.effective_chat.id, context)


async def _send_words_item(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    queue = context.user_data.get("words_queue", [])
    idx = context.user_data.get("words_index", 0)

    if idx >= len(queue):
        await context.bot.send_message(chat_id, "Все слова добавлены!")
        return

    item = queue[idx]
    examples_text = "\n".join(f"• {e}" for e in item.get("examples", []))
    conj = item.get("conjugation")
    conj_block = f"\n\n📝 Спряжение: {conj}" if conj else ""

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Сохранить ✅", callback_data=f"words_save:{idx}"),
        InlineKeyboardButton("Пропустить ➡️", callback_data=f"words_skip:{idx}"),
    ]])
    await context.bot.send_message(
        chat_id,
        f'*{item["phrase"]}* — {item.get("meaning", "")}\n'
        f'_{item.get("part_of_speech", "")} · {item.get("cefr_level", "")}_\n\n'
        f'Примеры:\n{examples_text}'
        f'{conj_block}',
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /delete
# ---------------------------------------------------------------------------

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        word = " ".join(context.args)
        deleted = db.delete_word(update.effective_user.id, word)
        if deleted:
            await update.message.reply_text(f'Слово *{word}* удалено из базы.', parse_mode="Markdown")
        else:
            await update.message.reply_text(
                f'Слово *{word}* не найдено. Напиши точно так, как оно сохранено.',
                parse_mode="Markdown",
            )
    else:
        words_list = db.get_user_words(update.effective_user.id)
        if not words_list:
            await update.message.reply_text("Твой словарь пуст.")
            return
        # Show first 20 words as buttons
        buttons = []
        for w in words_list[:20]:
            buttons.append([InlineKeyboardButton(w, callback_data=f"del_word:{w}")])
        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(
            "Выбери слово для удаления (показаны последние 20):",
            reply_markup=keyboard,
        )


# ---------------------------------------------------------------------------
# /review — recognition + fill-in-the-blank
# ---------------------------------------------------------------------------

async def review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_next_due(update.effective_chat.id, update.effective_user.id, context)


async def _send_next_due(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    due = db.get_due_words(user_id)
    if not due:
        await context.bot.send_message(chat_id, "Нет слов для повторения сегодня 🎉")
        return

    row = due[0]
    examples = json.loads(row["examples"] or "[]")
    distractors = db.get_distractors(user_id, exclude_id=row["id"], count=2)

    if examples and len(distractors) >= 2:
        sent = await _try_send_fill_blank(chat_id, row, examples, distractors, context)
        if sent:
            return

    await _send_recognition(chat_id, row, context)


async def _send_recognition(chat_id: int, row, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Показать ответ", callback_data=f"show:{row['id']}")]]
    )
    await context.bot.send_message(
        chat_id,
        f'Что значит:\n\n*{row["phrase"]}*',
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def _try_send_fill_blank(chat_id, row, examples, distractors, context) -> bool:
    phrase = row["phrase"]
    # Try to match the base word without article for fill-in-the-blank
    example = examples[0]

    # Extract the word part (after article for nouns)
    word_for_search = phrase
    if " " in phrase and phrase.split()[0].lower() in ("el", "la", "los", "las"):
        word_for_search = phrase.split(" ", 1)[1]

    blank_sentence = re.sub(re.escape(word_for_search), "_____", example, count=1, flags=re.IGNORECASE)
    if "_____" not in blank_sentence:
        blank_sentence = re.sub(re.escape(phrase), "_____", example, count=1, flags=re.IGNORECASE)
    if "_____" not in blank_sentence:
        return False

    options = [phrase] + [d["phrase"] for d in distractors]
    random.shuffle(options)

    buttons = [
        InlineKeyboardButton(
            opt,
            callback_data=f"fill:{row['id']}:{'correct' if opt == phrase else 'wrong'}",
        )
        for opt in options
    ]
    keyboard = InlineKeyboardMarkup([buttons])

    await context.bot.send_message(
        chat_id,
        f'Вставь пропущенное слово:\n\n*{blank_sentence}*',
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return True


# ---------------------------------------------------------------------------
# /gender — угадай род существительного
# ---------------------------------------------------------------------------

async def gender_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_gender_question(update.effective_chat.id, update.effective_user.id, context)


async def _send_gender_question(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    nouns = db.get_nouns_for_gender_quiz(user_id)
    if not nouns:
        await context.bot.send_message(
            chat_id,
            "Нет существительных для повторения сегодня. Добавь больше слов! 📚"
        )
        return

    row = nouns[0]
    phrase = row["phrase"]
    # Strip article to show bare noun
    word_without_article = phrase
    if " " in phrase and phrase.split()[0].lower() in ("el", "la", "los", "las"):
        word_without_article = phrase.split(" ", 1)[1]

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("el", callback_data=f"gender:{row['id']}:el"),
        InlineKeyboardButton("la", callback_data=f"gender:{row['id']}:la"),
    ]])
    await context.bot.send_message(
        chat_id,
        f'Какой артикль?\n\n*{word_without_article}*',
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db.get_stats(update.effective_user.id)
    cefr = s.get("cefr", {})
    cefr_text = ""
    if cefr:
        parts = [f"{lvl}: {cnt}" for lvl, cnt in sorted(cefr.items())]
        cefr_text = f"\nПо уровням: {', '.join(parts)}"

    await update.message.reply_text(
        f"Твой словарь испанского:\n\n"
        f"📥 Собрано (ещё не учим): {s.get('collected', 0)}\n"
        f"📖 Учим: {s.get('learning', 0)}\n"
        f"🔄 Знакомо: {s.get('familiar', 0)}\n"
        f"✅ Активно: {s.get('active', 0)}\n"
        f"🏆 Выучено: {s.get('mastered', 0)}\n"
        f"\nВсего: {s.get('total', 0)}"
        f"{cefr_text}"
    )


# ---------------------------------------------------------------------------
# Button callback handler
# ---------------------------------------------------------------------------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    parts = data.split(":")
    action = parts[0]

    # --- recognition: show answer ---
    if action == "show":
        word_id = int(parts[1])
        row = db.get_word_by_id(word_id)
        if row is None:
            return
        examples = json.loads(row["examples"] or "[]")
        examples_text = "\n".join(f"• {e}" for e in examples)
        conj = row["conjugation"]
        conj_block = f"\n\n📝 Спряжение: {conj}" if conj else ""

        await query.edit_message_text(
            f'*{row["phrase"]}* — {row["meaning"]}\n'
            f'_{row["part_of_speech"]} · {row["cefr_level"]}_\n\n'
            f'Примеры:\n{examples_text}'
            f'{conj_block}',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Знаю ✅", callback_data=f"good:{word_id}"),
                InlineKeyboardButton("Не знаю ❌", callback_data=f"bad:{word_id}"),
            ]]),
            parse_mode="Markdown",
        )

    # --- recognition: grade ---
    elif action in ("good", "bad"):
        word_id = int(parts[1])
        remembered = (action == "good")
        db.mark_review_result(word_id, remembered)
        row = db.get_word_by_id(word_id)
        mark = "✅" if remembered else "↩️ повторим позже"
        await query.edit_message_text(f'*{row["phrase"]}* — {mark}', parse_mode="Markdown")
        await _send_next_due(query.message.chat_id, query.from_user.id, context)

    # --- fill-in-the-blank ---
    elif action == "fill":
        word_id = int(parts[1])
        result = parts[2]
        remembered = (result == "correct")
        db.mark_review_result(word_id, remembered)
        row = db.get_word_by_id(word_id)
        if remembered:
            await query.edit_message_text(
                f'Правильно! ✅\n\n*{row["phrase"]}* — {row["meaning"]}',
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f'Неверно ❌\n\nПравильный ответ: *{row["phrase"]}* — {row["meaning"]}',
                parse_mode="Markdown",
            )
        await _send_next_due(query.message.chat_id, query.from_user.id, context)

    # --- gender quiz ---
    elif action == "gender":
        word_id = int(parts[1])
        chosen = parts[2]
        row = db.get_word_by_id(word_id)
        if row is None:
            return
        phrase = row["phrase"]
        correct_article = phrase.split()[0].lower() if " " in phrase else None

        if correct_article == chosen:
            db.mark_review_result(word_id, True)
            await query.edit_message_text(
                f'Правильно! ✅\n\n*{phrase}* — {row["meaning"]}',
                parse_mode="Markdown",
            )
        else:
            db.mark_review_result(word_id, False)
            await query.edit_message_text(
                f'Неверно ❌\n\nПравильно: *{phrase}* — {row["meaning"]}',
                parse_mode="Markdown",
            )
        await _send_gender_question(query.message.chat_id, query.from_user.id, context)

    # --- delete word by button ---
    elif action == "del_word":
        word = ":".join(parts[1:])
        deleted = db.delete_word(query.from_user.id, word)
        if deleted:
            await query.edit_message_text(f'Слово *{word}* удалено.', parse_mode="Markdown")
        else:
            await query.edit_message_text(f'Слово *{word}* не найдено.', parse_mode="Markdown")

    # --- /words queue ---
    elif action in ("words_save", "words_skip"):
        op = action.split("_")[1]
        idx = int(parts[1])
        queue = context.user_data.get("words_queue", [])

        if op == "save" and idx < len(queue):
            item = queue[idx]
            db.add_word(
                user_id=query.from_user.id,
                phrase=item["phrase"],
                meaning=item.get("meaning", ""),
                part_of_speech=item.get("part_of_speech", ""),
                cefr_level=item.get("cefr_level", ""),
                examples=item.get("examples", []),
                conjugation=item.get("conjugation"),
            )
            await query.edit_message_text(
                f'Сохранено: *{item["phrase"]}* ✅',
                parse_mode="Markdown",
            )
        else:
            if idx < len(queue):
                item = queue[idx]
                db.add_skipped_word(
                    user_id=query.from_user.id,
                    phrase=item["phrase"],
                    meaning=item.get("meaning", ""),
                    part_of_speech=item.get("part_of_speech", ""),
                    cefr_level=item.get("cefr_level", ""),
                    examples=item.get("examples", []),
                    conjugation=item.get("conjugation"),
                )
                await query.edit_message_text(
                    f'Пропущено: *{item["phrase"]}*',
                    parse_mode="Markdown",
                )

        context.user_data["words_index"] = idx + 1
        await _send_words_item(query.message.chat_id, context)


# ---------------------------------------------------------------------------
# Daily reminder
# ---------------------------------------------------------------------------

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    for user_id in db.get_all_due_users():
        await context.bot.send_message(user_id, "Время повторить испанские слова! 🇪🇸")
        await _send_next_due(user_id, user_id, context)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def post_init(app: Application):
    await app.bot.set_my_commands([
        ("words", "Добавить 10 частотных слов"),
        ("review", "Повторить слова по расписанию"),
        ("gender", "Угадать род существительных"),
        ("delete", "Удалить слово из базы"),
        ("stats", "Статистика словаря"),
    ])


def main():
    db.init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("words", words))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("review", review))
    app.add_handler(CommandHandler("gender", gender_quiz))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(daily_reminder, time=dtime(hour=REMINDER_HOUR_UTC, minute=0))

    print("Bot started. Stop with Ctrl+C.")
    app.run_polling()


if __name__ == "__main__":
    main()
