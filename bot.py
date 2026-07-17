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
# 10:00 Moscow = 07:00 UTC; 18:00 Moscow = 15:00 UTC
REMINDER_MORNING_UTC = (7, 0)
REMINDER_EVENING_UTC = (15, 0)
NEW_WORDS_DAILY_LIMIT = 15


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу тебе учить испанские слова 🇪🇸\n\n"
        "Просто напиши любое испанское слово — я объясню и сохраню его.\n\n"
        "/words — добавить новые слова автоматически\n"
        "/review — повторить слова по расписанию\n"
        "/all — повторить все слова из базы\n"
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

    window = db.get_current_window()
    if window == 'morning':
        review_hint = "Первое повторение — сегодня вечером."
    else:
        review_hint = "Первое повторение — завтра утром."

    await update.message.reply_text(
        f'✅ *{info.get("phrase", word)}*\n'
        f'{info.get("meaning", "")}\n'
        f'_{info.get("part_of_speech", "")} · {info.get("cefr_level", "")}_\n\n'
        f'Примеры:\n{examples_text}'
        f'{conj_block}\n\n'
        f'{review_hint}',
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /words — новые слова
# ---------------------------------------------------------------------------

async def words(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("all_queue", None)
    context.user_data.pop("words_saved", None)
    context.user_data.pop("words_skipped", None)
    await update.message.reply_text("Подбираю слова, подожди немного...")

    existing = db.get_user_words(update.effective_user.id)
    new_words = ai_helper.find_frequent_words(existing, count=5)

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
        saved = context.user_data.get("words_saved", 0)
        skipped = context.user_data.get("words_skipped", 0)
        context.user_data.pop("words_saved", None)
        context.user_data.pop("words_skipped", None)
        await context.bot.send_message(
            chat_id,
            f"Готово! Сохранено: {saved}, пропущено: {skipped}."
        )
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
    context.user_data.pop("all_queue", None)
    context.user_data["review_shown"] = set()
    context.user_data["review_new_shown"] = 0
    db.detect_and_mark_overdue(update.effective_user.id)
    await _send_next_due(update.effective_chat.id, update.effective_user.id, context)


async def _send_next_due(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, user_data=None):
    if user_data is None:
        user_data = context.user_data
    shown = user_data.get("review_shown", set())
    new_shown = user_data.get("review_new_shown", 0)
    overdue, scheduled = db.get_due_words_split(user_id)

    # Filter out already shown in this session
    overdue = [r for r in overdue if r["id"] not in shown]
    scheduled = [r for r in scheduled if r["id"] not in shown]

    if not overdue and not scheduled:
        await context.bot.send_message(chat_id, "Нет слов для повторения сегодня 🎉")
        return

    # Overdue first, then scheduled, both ordered by due date. Only brand-new
    # (never reviewed) words count against the daily cap — words already in
    # progress are reviewed every time they're due, same as Anki review cards.
    row = None
    is_overdue = False
    for candidate, candidate_is_overdue in [(r, True) for r in overdue] + [(r, False) for r in scheduled]:
        is_new = (candidate["times_reviewed"] or 0) == 0
        if is_new and new_shown >= NEW_WORDS_DAILY_LIMIT:
            continue
        row, is_overdue = candidate, candidate_is_overdue
        break

    if row is None:
        await context.bot.send_message(
            chat_id,
            f"Новых слов на сегодня достаточно — изучили {NEW_WORDS_DAILY_LIMIT} 👍\n"
            f"Слова на повторение из старого расписания на сегодня закончились.",
        )
        return

    is_new = (row["times_reviewed"] or 0) == 0
    if is_new:
        user_data["review_new_shown"] = new_shown + 1
    shown.add(row["id"])
    user_data["review_shown"] = shown

    examples = json.loads(row["examples"] or "[]")
    distractors = db.get_distractors(user_id, exclude_id=row["id"], count=2)

    if examples and len(distractors) >= 2:
        sent = await _try_send_fill_blank(chat_id, row, examples, distractors, context, is_overdue)
        if sent:
            return

    await _send_recognition(chat_id, row, context, is_overdue)


async def _send_recognition(chat_id: int, row, context: ContextTypes.DEFAULT_TYPE, is_overdue: bool = False):
    prefix = "⚠️ *Пропущено ранее*\n\n" if is_overdue else ""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Показать ответ", callback_data=f"show:{row['id']}")]]
    )
    await context.bot.send_message(
        chat_id,
        f'{prefix}Как будет по-испански:\n\n*{row["meaning"]}*',
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def _try_send_fill_blank(chat_id, row, examples, distractors, context, is_overdue=False) -> bool:
    phrase = row["phrase"]
    example = examples[0]

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
    prefix = "⚠️ *Пропущено ранее*\n\n" if is_overdue else ""

    await context.bot.send_message(
        chat_id,
        f'{prefix}Вставь пропущенное слово:\n\n*{blank_sentence}*',
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return True


# ---------------------------------------------------------------------------
# /all — повторить все слова
# ---------------------------------------------------------------------------

async def review_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_words = db.get_all_words_for_review(update.effective_user.id)
    if not all_words:
        await update.message.reply_text("В базе нет слов для повторения.")
        return
    context.user_data["all_queue"] = [dict(r) for r in all_words]
    context.user_data["all_index"] = 0
    await _send_all_next(update.effective_chat.id, update.effective_user.id, context)


async def _send_all_next(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    queue = context.user_data.get("all_queue", [])
    idx = context.user_data.get("all_index", 0)
    if idx >= len(queue):
        await context.bot.send_message(chat_id, "Все слова пройдены! 🎉")
        return
    row = queue[idx]
    examples = json.loads(row.get("examples") or "[]")
    distractors = db.get_distractors(user_id, exclude_id=row["id"], count=2)
    if examples and len(distractors) >= 2:
        sent = await _try_send_fill_blank(chat_id, row, examples, distractors, context)
        if sent:
            return
    await _send_recognition(chat_id, row, context)


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
# /reset_collected — one-off: spread stuck "collected" words across days
# ---------------------------------------------------------------------------

async def reset_collected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    batches = db.reset_collected_review_dates(update.effective_user.id)
    if not batches:
        await update.message.reply_text("Нет слов в статусе «собрано» — распределять нечего.")
        return
    total = sum(c for _, c in batches)
    lines = "\n".join(f"{d}: {c} слов" for d, c in batches)
    await update.message.reply_text(
        f"Распределила {total} слов по датам:\n{lines}\n\nТеперь заходи в /review как обычно."
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
            f'{conj_block}\n\nТы вспомнил(а)?',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Легко 🟢", callback_data=f"grade:{word_id}:easy"),
                InlineKeyboardButton("Помню 🟡", callback_data=f"grade:{word_id}:remember"),
                InlineKeyboardButton("Сложно 🔴", callback_data=f"grade:{word_id}:hard"),
            ]]),
            parse_mode="Markdown",
        )

    # --- self-assessment grade ---
    elif action == "grade":
        word_id = int(parts[1])
        grade = parts[2]
        db.mark_review_result(word_id, grade)
        row = db.get_word_by_id(word_id)
        marks = {"easy": "Легко 🟢", "remember": "Помню 🟡", "hard": "Сложно 🔴"}
        await query.edit_message_text(
            f'*{row["phrase"]}* — {marks.get(grade, "")}',
            parse_mode="Markdown",
        )
        if "all_queue" in context.user_data:
            context.user_data["all_index"] = context.user_data.get("all_index", 0) + 1
            await _send_all_next(query.message.chat_id, query.from_user.id, context)
        else:
            await _send_next_due(query.message.chat_id, query.from_user.id, context)

    # --- fill-in-the-blank ---
    elif action == "fill":
        word_id = int(parts[1])
        result = parts[2]
        grade = "remember" if result == "correct" else "hard"
        db.mark_review_result(word_id, grade)
        row = db.get_word_by_id(word_id)
        if result == "correct":
            await query.edit_message_text(
                f'Правильно! ✅\n\n*{row["phrase"]}* — {row["meaning"]}',
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f'Неверно ❌\n\nПравильный ответ: *{row["phrase"]}* — {row["meaning"]}',
                parse_mode="Markdown",
            )
        if "all_queue" in context.user_data:
            context.user_data["all_index"] = context.user_data.get("all_index", 0) + 1
            await _send_all_next(query.message.chat_id, query.from_user.id, context)
        else:
            await _send_next_due(query.message.chat_id, query.from_user.id, context)

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
            context.user_data["words_saved"] = context.user_data.get("words_saved", 0) + 1
            await query.edit_message_text(
                f'Сохранено: *{item["phrase"]}* ✅',
                parse_mode="Markdown",
            )
        else:
            if idx < len(queue):
                item = queue[idx]
                context.user_data["words_skipped"] = context.user_data.get("words_skipped", 0) + 1
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
# Daily reminders
# ---------------------------------------------------------------------------

async def morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    for user_id in db.get_all_due_users():
        count = db.count_due_not_reviewed_today(user_id)
        if count > 0:
            await context.bot.send_message(
                user_id,
                f"☀️ Доброе утро! Слов на повторение: {count}"
            )
            user_data = context.application.user_data[user_id]
            user_data["review_shown"] = set()
            user_data["review_new_shown"] = 0
            db.detect_and_mark_overdue(user_id)
            await _send_next_due(user_id, user_id, context, user_data)


async def evening_reminder(context: ContextTypes.DEFAULT_TYPE):
    for user_id in db.get_all_due_users():
        count = db.count_due_not_reviewed_today(user_id)
        if count > 0:
            await context.bot.send_message(
                user_id,
                f"🌙 Добрый вечер! Слов на повторение: {count}"
            )
            user_data = context.application.user_data[user_id]
            await _send_next_due(user_id, user_id, context, user_data)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def post_init(app: Application):
    await app.bot.set_my_commands([
        ("words", "Добавить новые слова"),
        ("review", "Повторить слова по расписанию"),
        ("all", "Повторить все слова из базы"),
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
    app.add_handler(CommandHandler("all", review_all))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset_collected", reset_collected))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(morning_reminder, time=dtime(hour=REMINDER_MORNING_UTC[0], minute=REMINDER_MORNING_UTC[1]))
    app.job_queue.run_daily(evening_reminder, time=dtime(hour=REMINDER_EVENING_UTC[0], minute=REMINDER_EVENING_UTC[1]))

    print("Bot started. Stop with Ctrl+C.")
    app.run_polling()


if __name__ == "__main__":
    main()
