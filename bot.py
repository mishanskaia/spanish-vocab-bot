import os
import re
import json
import logging
from datetime import date, time as dtime

from dotenv import load_dotenv

load_dotenv()

from aiohttp import web
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
API_KEY = os.environ.get("API_KEY")
API_WRITE_KEY = os.environ.get("API_WRITE_KEY")
# 10:00 Moscow = 07:00 UTC; 14:00 Moscow = 11:00 UTC; 18:00 Moscow = 15:00 UTC
REMINDER_MORNING_UTC = (7, 0)
REMINDER_MIDDAY_UTC = (11, 0)
REMINDER_EVENING_UTC = (15, 0)

SESSION_WORD_LIMIT = 30

RECALL_DISCLAIMER = (
    "💡 Ответ в карточках повторения — в словарной форме "
    "(инфинитив для глаголов, с артиклем для существительных), "
    "даже если в примере слово стоит в другой форме."
)


def _format_conj_gerund(conjugation, gerund) -> str:
    if not conjugation:
        return ""
    block = f"\n\n📝 Спряжение: {conjugation}"
    if gerund:
        block += f"\nГерундий: {gerund}"
    return block


def _format_collocations(collocations) -> str:
    if not collocations:
        return ""
    lines = "\n".join(f"• {c}" for c in collocations)
    return f"\n\n💬 Устойчивые выражения:\n{lines}"


def _split_example(example: str):
    """Examples are stored as "испанский — русский перевод" in one string."""
    if " — " in example:
        es, ru = example.split(" — ", 1)
        return es.strip(), ru.strip()
    return example.strip(), None


def _make_blank(phrase: str, example_es: str):
    word_for_search = phrase
    if " " in phrase and phrase.split()[0].lower() in ("el", "la", "los", "las"):
        word_for_search = phrase.split(" ", 1)[1]

    blank = re.sub(re.escape(word_for_search), "_____", example_es, count=1, flags=re.IGNORECASE)
    if "_____" not in blank:
        blank = re.sub(re.escape(phrase), "_____", example_es, count=1, flags=re.IGNORECASE)
    if "_____" not in blank:
        return None
    return blank


def _build_recall_prompt(row) -> str:
    """Active-recall prompt: cloze sentence when we can find the word in the
    example, otherwise the example's Russian translation as context, otherwise
    just the bare meaning."""
    examples = json.loads(row["examples"] or "[]")
    if examples:
        example_es, example_ru = _split_example(examples[0])
        blank = _make_blank(row["phrase"], example_es)
        if blank:
            suffix = f" — {example_ru}" if example_ru else ""
            return f'Вставь пропущенное слово:\n\n*{blank}{suffix}*'
        if example_ru:
            return f'Контекст: _{example_ru}_\n\nКак будет по-испански: *{row["meaning"]}*'
    return f'Как будет по-испански:\n\n*{row["meaning"]}*'


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

    try:
        info = ai_helper.explain_word(word)
    except Exception:
        logger.exception("explain_word failed for %r", word)
        await update.message.reply_text(
            "Не получилось найти это слово — попробуй ещё раз через минуту."
        )
        return

    word_id, is_new = db.add_word(
        user_id=update.effective_user.id,
        phrase=info.get("phrase", word),
        meaning=info.get("meaning", ""),
        part_of_speech=info.get("part_of_speech", ""),
        cefr_level=info.get("cefr_level", ""),
        examples=info.get("examples", []),
        conjugation=info.get("conjugation"),
        collocations=info.get("collocations", []),
        gerund=info.get("gerund"),
    )

    if not is_new:
        await update.message.reply_text(
            f'📖 *{info.get("phrase", word)}* уже есть в твоём словаре — не добавляю дубль.',
            parse_mode="Markdown",
        )
        return

    examples_text = "\n".join(f"• {e}" for e in info.get("examples", []))
    conj_block = _format_conj_gerund(info.get("conjugation"), info.get("gerund"))
    collocations_block = _format_collocations(info.get("collocations"))

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
        f'{conj_block}'
        f'{collocations_block}\n\n'
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
    try:
        new_words = ai_helper.find_frequent_words(existing, count=5)
    except Exception:
        logger.exception("find_frequent_words failed")
        new_words = []

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
    conj_block = _format_conj_gerund(item.get("conjugation"), item.get("gerund"))
    collocations_block = _format_collocations(item.get("collocations"))

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Сохранить ✅", callback_data=f"words_save:{idx}"),
        InlineKeyboardButton("Пропустить ➡️", callback_data=f"words_skip:{idx}"),
    ]])
    await context.bot.send_message(
        chat_id,
        f'*{item["phrase"]}* — {item.get("meaning", "")}\n'
        f'_{item.get("part_of_speech", "")} · {item.get("cefr_level", "")}_\n\n'
        f'Примеры:\n{examples_text}'
        f'{conj_block}'
        f'{collocations_block}',
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
# /review — active recall (cloze context, self-graded)
# ---------------------------------------------------------------------------

async def review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("all_queue", None)
    context.user_data["review_shown"] = set()
    db.detect_and_mark_overdue(update.effective_user.id)
    await update.message.reply_text(RECALL_DISCLAIMER)
    await _send_next_due(update.effective_chat.id, update.effective_user.id, context)


async def _send_next_due(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE, user_data=None):
    if user_data is None:
        user_data = context.user_data
    shown = user_data.get("review_shown", set())
    overdue, scheduled = db.get_due_words_split(user_id)

    # Filter out already shown in this session
    overdue = [r for r in overdue if r["id"] not in shown]
    scheduled = [r for r in scheduled if r["id"] not in shown]

    if not overdue and not scheduled:
        await context.bot.send_message(chat_id, "Нет слов для повторения сегодня 🎉")
        return

    if len(shown) >= SESSION_WORD_LIMIT:
        await context.bot.send_message(
            chat_id,
            f"На эту сессию хватит — {SESSION_WORD_LIMIT} слов сделано 👍\n"
            f"Остальное подождёт следующей сессии (10:00 / 14:00 / 18:00 МСК)."
        )
        return

    # Overdue first, then scheduled, both ordered by due date.
    row, is_overdue = (overdue + scheduled)[0], bool(overdue)

    shown.add(row["id"])
    user_data["review_shown"] = shown

    await _send_recall_card(chat_id, row, context, is_overdue)


async def _send_recall_card(chat_id: int, row, context: ContextTypes.DEFAULT_TYPE, is_overdue: bool = False):
    prefix = "⚠️ *Пропущено ранее*\n\n" if is_overdue else ""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Показать ответ", callback_data=f"show:{row['id']}")]]
    )
    await context.bot.send_message(
        chat_id,
        f'{prefix}{_build_recall_prompt(row)}',
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


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
    await update.message.reply_text(RECALL_DISCLAIMER)
    await _send_all_next(update.effective_chat.id, update.effective_user.id, context)


async def _send_all_next(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    queue = context.user_data.get("all_queue", [])
    idx = context.user_data.get("all_index", 0)
    if idx >= len(queue):
        await context.bot.send_message(chat_id, "Все слова пройдены! 🎉")
        return
    row = queue[idx]
    await _send_recall_card(chat_id, row, context)


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


async def debug_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.get_review_history_words(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Нет ни одного слова, которое уже проходило повторение хотя бы раз.")
        return
    today = date.today().isoformat()
    lines = []
    for r in rows:
        due_mark = "✅ due" if (r["next_review_date"] or "") <= today else "⏳ ждёт"
        lines.append(
            f"{due_mark} | {r['phrase']}\n"
            f"   status={r['status']} pool={r['pool']} stage={r['interval_stage']} "
            f"reviews={r['times_reviewed']} next={r['next_review_date']}"
        )
    text = f"Слова с историей повторений ({len(rows)}), сегодня={today}:\n\n" + "\n\n".join(lines)
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i:i + 3800])


async def debug_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db.detect_and_mark_overdue(user_id)
    overdue, scheduled = db.get_due_words_split(user_id)
    combined = overdue + scheduled
    today = date.today().isoformat()

    if not combined:
        await update.message.reply_text(f"Очередь /review пуста сегодня ({today}).")
        return

    new_count = sum(1 for r in combined if (r["times_reviewed"] or 0) == 0)
    review_count = len(combined) - new_count

    lines = [
        f"Очередь /review сейчас, сегодня={today}:",
        f"Всего due: {len(combined)} (🆕 новых: {new_count}, 🔁 на повторение: {review_count})",
        "",
    ]
    for r in combined:
        is_new = (r["times_reviewed"] or 0) == 0
        tag = "🆕" if is_new else "🔁"
        lines.append(
            f"{tag} {r['phrase']} | pool={r['pool']} next={r['next_review_date']} reviews={r['times_reviewed']}"
        )
    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await update.message.reply_text(text[i:i + 3800])


def _mnemonic_keyboard(word_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Оставить ✅", callback_data=f"mnemo_keep:{word_id}"),
        InlineKeyboardButton("Другой вариант 🔄", callback_data=f"mnemo_retry:{word_id}"),
    ]])


async def _maybe_ask_mnemonic(chat_id: int, row, context: ContextTypes.DEFAULT_TYPE, grade: str):
    if grade == "remember":
        return
    await context.bot.send_message(
        chat_id,
        "Показать ассоциацию для запоминания?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Да", callback_data=f"mnemo_ask:{row['id']}:yes"),
            InlineKeyboardButton("Нет", callback_data=f"mnemo_ask:{row['id']}:no"),
        ]]),
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
        conj_block = _format_conj_gerund(row["conjugation"], row["gerund"])
        collocations_block = _format_collocations(json.loads(row["collocations"] or "[]"))

        await query.edit_message_text(
            f'*{row["phrase"]}* — {row["meaning"]}\n'
            f'_{row["part_of_speech"]} · {row["cefr_level"]}_\n\n'
            f'Примеры:\n{examples_text}'
            f'{conj_block}'
            f'{collocations_block}\n\nТы вспомнил(а)?',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Помню 🟢", callback_data=f"grade:{word_id}:remember"),
                InlineKeyboardButton("Почти 🟡", callback_data=f"grade:{word_id}:almost"),
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
        marks = {"remember": "Помню 🟢", "almost": "Почти помню 🟡", "hard": "Сложно 🔴"}
        await query.edit_message_text(
            f'*{row["phrase"]}* — {marks.get(grade, "")}',
            parse_mode="Markdown",
        )
        await _maybe_ask_mnemonic(query.message.chat_id, row, context, grade)
        if "all_queue" in context.user_data:
            context.user_data["all_index"] = context.user_data.get("all_index", 0) + 1
            await _send_all_next(query.message.chat_id, query.from_user.id, context)
        else:
            await _send_next_due(query.message.chat_id, query.from_user.id, context)

    # --- mnemonic show prompt ---
    elif action == "mnemo_ask":
        word_id = int(parts[1])
        answer = parts[2]
        if answer == "no":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        row = db.get_word_by_id(word_id)
        if row is None:
            return
        mnemonic = row["mnemonic"]
        if not mnemonic:
            try:
                mnemonic = ai_helper.get_mnemonic(row["phrase"], row["meaning"], row["part_of_speech"])
            except Exception:
                logger.exception("mnemonic generation failed for word_id=%s", word_id)
                try:
                    await query.edit_message_text("Не получилось подобрать ассоциацию, попробуй позже.")
                except Exception:
                    pass
                return
            db.save_mnemonic(word_id, mnemonic)
        keyboard = _mnemonic_keyboard(word_id)
        try:
            await query.edit_message_text(mnemonic, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            logger.exception("mnemonic edit failed for word_id=%s", word_id)
            try:
                await query.edit_message_text(mnemonic, reply_markup=keyboard)
            except Exception:
                logger.exception("mnemonic plain-text edit also failed for word_id=%s", word_id)

    # --- mnemonic accept/retry ---
    elif action == "mnemo_keep":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif action == "mnemo_retry":
        word_id = int(parts[1])
        row = db.get_word_by_id(word_id)
        if row is None:
            return
        try:
            new_mnemonic = ai_helper.get_mnemonic(
                row["phrase"], row["meaning"], row["part_of_speech"], avoid=row["mnemonic"]
            )
        except Exception:
            logger.exception("mnemonic retry failed for word_id=%s", word_id)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await context.bot.send_message(
                query.message.chat_id, "Не получилось подобрать другой вариант, попробуй позже."
            )
            return
        db.save_mnemonic(word_id, new_mnemonic)
        keyboard = _mnemonic_keyboard(word_id)
        try:
            await query.edit_message_text(new_mnemonic, parse_mode="Markdown", reply_markup=keyboard)
        except Exception:
            logger.exception("mnemonic retry edit failed for word_id=%s", word_id)
            try:
                await query.edit_message_text(new_mnemonic, reply_markup=keyboard)
            except Exception:
                logger.exception("mnemonic retry plain-text edit also failed for word_id=%s", word_id)

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
            _word_id, is_new = db.add_word(
                user_id=query.from_user.id,
                phrase=item["phrase"],
                meaning=item.get("meaning", ""),
                part_of_speech=item.get("part_of_speech", ""),
                cefr_level=item.get("cefr_level", ""),
                examples=item.get("examples", []),
                conjugation=item.get("conjugation"),
                collocations=item.get("collocations", []),
                gerund=item.get("gerund"),
            )
            context.user_data["words_saved"] = context.user_data.get("words_saved", 0) + 1
            status_text = "Сохранено" if is_new else "Уже было в словаре"
            await query.edit_message_text(
                f'{status_text}: *{item["phrase"]}* ✅',
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

async def _run_reminder(context: ContextTypes.DEFAULT_TYPE, greeting: str):
    for user_id in db.get_all_due_users():
        count = db.count_due_not_reviewed_today(user_id)
        if count > 0:
            session_count = min(count, SESSION_WORD_LIMIT)
            text = f"{greeting} Слов на эту сессию: {session_count}"
            if count > SESSION_WORD_LIMIT:
                text += f"\n(всего в очереди: {count})"
            await context.bot.send_message(user_id, text)
            user_data = context.application.user_data[user_id]
            user_data["review_shown"] = set()
            db.detect_and_mark_overdue(user_id)
            await _send_next_due(user_id, user_id, context, user_data)


async def morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    await _run_reminder(context, "☀️ Доброе утро!")


async def midday_reminder(context: ContextTypes.DEFAULT_TYPE):
    await _run_reminder(context, "🕑 Дневная сессия!")


async def evening_reminder(context: ContextTypes.DEFAULT_TYPE):
    await _run_reminder(context, "🌙 Добрый вечер!")


# ---------------------------------------------------------------------------
# API — lets another site of yours read your word list, and (with a separate
# write key) add new words the same way typing to the bot directly would.
# Runs in the same process/container as the bot, reading/writing the same DB file.
# ---------------------------------------------------------------------------

_CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}


def _word_row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["examples"] = json.loads(d.get("examples") or "[]")
    except (TypeError, json.JSONDecodeError):
        d["examples"] = []
    try:
        d["collocations"] = json.loads(d.get("collocations") or "[]")
    except (TypeError, json.JSONDecodeError):
        d["collocations"] = []
    return d


async def handle_api_words(request: web.Request) -> web.Response:
    if not API_KEY or request.query.get("key") != API_KEY:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_CORS_HEADERS)

    user_id_param = request.query.get("user_id")
    if not user_id_param or not user_id_param.isdigit():
        return web.json_response(
            {"error": "user_id query param is required"}, status=400, headers=_CORS_HEADERS
        )

    rows = db.get_words_for_export(int(user_id_param))
    words = [_word_row_to_dict(r) for r in rows]
    return web.json_response(words, headers=_CORS_HEADERS)


async def handle_api_add_word(request: web.Request) -> web.Response:
    if not API_WRITE_KEY:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_CORS_HEADERS)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400, headers=_CORS_HEADERS)

    if payload.get("key") != API_WRITE_KEY:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_CORS_HEADERS)

    try:
        user_id = int(payload.get("user_id"))
    except (TypeError, ValueError):
        user_id = None
    word = (payload.get("word") or "").strip()
    if user_id is None or not word:
        return web.json_response(
            {"error": "user_id (number) and word are required"}, status=400, headers=_CORS_HEADERS
        )

    try:
        info = ai_helper.explain_word(word)
    except Exception:
        logger.exception("api add_word: explain_word failed for %r", word)
        return web.json_response({"error": "failed to look up word"}, status=502, headers=_CORS_HEADERS)

    word_id, is_new = db.add_word(
        user_id=user_id,
        phrase=info.get("phrase", word),
        meaning=info.get("meaning", ""),
        part_of_speech=info.get("part_of_speech", ""),
        cefr_level=info.get("cefr_level", ""),
        examples=info.get("examples", []),
        conjugation=info.get("conjugation"),
        collocations=info.get("collocations", []),
        gerund=info.get("gerund"),
    )
    result = _word_row_to_dict(db.get_word_by_id(word_id))
    return web.json_response(
        {"is_new": is_new, "word": result},
        status=201 if is_new else 200,
        headers=_CORS_HEADERS,
    )


async def handle_api_words_options(request: web.Request) -> web.Response:
    return web.Response(headers={
        **_CORS_HEADERS,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    })


async def handle_api_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def start_api_server(app: Application):
    if not API_KEY:
        logger.warning("API_KEY is not set — the read-only API will reject every request.")
    if not API_WRITE_KEY:
        logger.warning("API_WRITE_KEY is not set — the add-word API will reject every request.")
    api = web.Application()
    api.router.add_get("/words", handle_api_words)
    api.router.add_post("/words", handle_api_add_word)
    api.router.add_route("OPTIONS", "/words", handle_api_words_options)
    api.router.add_get("/health", handle_api_health)
    runner = web.AppRunner(api)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    app.bot_data["api_runner"] = runner  # keep a reference so it isn't garbage-collected
    logger.info(f"API listening on port {port}")


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
    await start_api_server(app)


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
    app.add_handler(CommandHandler("debug_due", debug_due))
    app.add_handler(CommandHandler("debug_queue", debug_queue))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(morning_reminder, time=dtime(hour=REMINDER_MORNING_UTC[0], minute=REMINDER_MORNING_UTC[1]))
    app.job_queue.run_daily(midday_reminder, time=dtime(hour=REMINDER_MIDDAY_UTC[0], minute=REMINDER_MIDDAY_UTC[1]))
    app.job_queue.run_daily(evening_reminder, time=dtime(hour=REMINDER_EVENING_UTC[0], minute=REMINDER_EVENING_UTC[1]))

    print("Bot started. Stop with Ctrl+C.")
    app.run_polling()


if __name__ == "__main__":
    main()
