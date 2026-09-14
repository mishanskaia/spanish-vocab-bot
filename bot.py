import os
import re
import json
import time
import hmac
import asyncio
import logging
import tempfile
from datetime import date, datetime, time as dtime, timezone as dt_timezone

from dotenv import load_dotenv

load_dotenv()

from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import BadRequest, NetworkError
from telegram.helpers import escape_markdown

import db
import ai_helper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_KEY = os.environ.get("API_KEY")
API_WRITE_KEY = os.environ.get("API_WRITE_KEY")
OWNER_TELEGRAM_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "0") or "0")
WEEKLY_NEW_WORD_LIMIT = int(os.environ.get("WEEKLY_NEW_WORD_LIMIT", "35"))
AUTHOR_TELEGRAM_USERNAME = os.environ.get("AUTHOR_TELEGRAM_USERNAME", "")
INVITE_TTL_DAYS = 15
# Vocab reminders fire at these hours in each user's own local time (see
# get_user_utc_offset) rather than a fixed UTC time — checked hourly by
# hourly_reminder_job(). A user who hasn't answered the /start timezone
# question defaults to UTC+3 (Moscow), so this reduces to the old fixed
# schedule for anyone who never sets a timezone.
REMINDER_LOCAL_SLOTS = {
    10: "☀️ Доброе утро!",
    14: "🕑 Дневная сессия!",
    18: "🌙 Добрый вечер!",
}
DEFAULT_UTC_OFFSET = 3  # Moscow — used until a user answers the /start timezone question
# 7:00 Moscow = 04:00 UTC — Study Coach, separate from the vocab reminders above
STUDY_COACH_UTC = (4, 0)
# Sunday 06:00 Moscow = 03:00 UTC — weekly DB backup, quiet time before the day's reminders
BACKUP_WEEKLY_UTC = (3, 0)
BACKUP_WEEKDAY = 6  # Monday=0 .. Sunday=6, per job_queue's `days`

SESSION_WORD_LIMIT = 30
MNEMONIC_RETRY_LIMIT = 3

ACCESS_TEST_MESSAGE = (
    "Бот сейчас тестируется. Если тебе нужен доступ — спроси автора"
    + (f" (@{AUTHOR_TELEGRAM_USERNAME})" if AUTHOR_TELEGRAM_USERNAME else "")
    + "."
)

RECALL_DISCLAIMER = (
    "💡 Ответ в карточках повторения — в словарной форме "
    "(инфинитив для глаголов, с артиклем для существительных), "
    "даже если в примере слово стоит в другой форме."
)


_MD_LINK_RE = re.compile(r'\[([^\]]*)\]\([^)]*\)')


def _strip_markdown_links(text: str) -> str:
    """The mnemonic is the one place we deliberately render Claude's raw
    Markdown as-is (bold/italic are part of the intended output — see
    MNEMONIC_SYSTEM_PROMPT), so it can't be run through _md() like
    explain_word() output. That leaves it as the one spot a crafted "word"
    input could still try to smuggle a clickable `[text](url)` phishing link
    into a message that looks like normal bot output — strip just that
    pattern (down to its label) while leaving real formatting untouched."""
    return _MD_LINK_RE.sub(r'\1', text) if text else text


def _md(text) -> str:
    """Escape Telegram Markdown (v1) special chars (`*_[`) before interpolating
    AI-generated text into a parse_mode="Markdown" message. Claude's output for
    explain_word() is meant to be plain text, not formatted — without this, a
    stray `*`/`_`/`[` in a word/example (accidental, or a deliberately crafted
    input trying to smuggle in a clickable markdown link) either breaks message
    rendering or renders as if it were trusted formatting."""
    return escape_markdown(str(text), version=1) if text else text


def _format_conj_gerund(conjugation, gerund) -> str:
    if not conjugation:
        return ""
    block = f"\n\n📝 Спряжение: {_md(conjugation)}"
    if gerund:
        block += f"\nГерундий: {_md(gerund)}"
    return block


def _format_collocations(collocations) -> str:
    if not collocations:
        return ""
    lines = "\n".join(f"• {_md(c)}" for c in collocations)
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
        # Escape before blanking, not after: _make_blank() inserts a literal
        # "_____" placeholder that must stay as-is, not become escaped underscores.
        example_es = _md(example_es)
        example_ru = _md(example_ru) if example_ru else example_ru
        blank = _make_blank(row["phrase"], example_es)
        if blank:
            suffix = f" — {example_ru}" if example_ru else ""
            return f'Вставь пропущенное слово:\n\n*{blank}{suffix}*'
        if example_ru:
            return f'Контекст: _{example_ru}_\n\nКак будет по-испански: *{_md(row["meaning"])}*'
    return f'Как будет по-испански:\n\n*{_md(row["meaning"])}*'


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not _is_authorized(user_id):
        code = context.args[0] if context.args else None
        if not (code and db.redeem_invite(code, user_id)):
            await update.message.reply_text(ACCESS_TEST_MESSAGE)
            return

    await _send_welcome(update)

    if db.get_user_utc_offset(user_id) is None:
        context.user_data["awaiting_tz_reply"] = True
        await update.message.reply_text(
            "Кстати — скажи, который у тебя сейчас час (например, 14:30)? "
            "Подстрою напоминания под твой часовой пояс."
        )


async def _send_welcome(update: Update):
    await update.message.reply_text(
        "Привет! Я помогу тебе учить испанские слова 🇪🇸\n\n"
        "Работаю по методу интервальных повторений: буду возвращать слова "
        "на повторение через подходящие интервалы, чтобы они лучше "
        "закреплялись в памяти.\n\n"
        "Как это работает:\n"
        "— Просто отправляй мне слово или фразу — по-испански или по-русски "
        "(сама переведу в нужную сторону), объясню, дам примеры и добавлю "
        "в обучение.\n"
        "— Повторения буду присылать сам 3 раза в день: в 10:00, 14:00 и "
        "18:00 по твоему времени.\n"
        "— За одно повторение — до 30 слов.\n"
        "— Если слово сложное, могу предложить мнемонику.\n"
        "— Ошибочно добавленное слово можно удалить через /delete.\n"
        "— Подробнее о повторениях и командах: /help\n\n"
        f"Это пока тестовая версия: до {WEEKLY_NEW_WORD_LIMIT} новых слов в "
        "неделю (лимит обновляется по понедельникам). Если что-то работает "
        "не так — пиши Оле лично, разберёмся"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Что я умею:\n\n"
        "📖 Добавить слово — просто напиши его по-испански или по-русски, "
        "сама переведу в нужную сторону, объясню и сохраню.\n\n"
        "🔁 Повторение\n"
        "/review — слова по расписанию (что пора повторить сейчас)\n"
        "/all — вообще всё из словаря, без расписания\n\n"
        "Принцип — интервальное повторение: каждый раз, когда правильно "
        "вспоминаешь слово, промежуток до следующего показа растёт — "
        "примерно так: завтра → через 3 дня → через неделю → через 2 "
        "недели → через месяц → через 3 месяца. После этого слово "
        "считается выученным и больше не показывается.\n\n"
        "Оцениваешь себя сам тремя кнопками:\n"
        "— Помню 🟢 — двигает по этой лестнице вперёд, увидишь нескоро\n"
        "— Почти помню 🟡 — лестница не двигается, промежуток чуть короче "
        "— увидишь пораньше\n"
        "— Сложно 🔴 — то же самое, но покороче — увидишь уже завтра\n\n"
        "Это не экзамен и не наказание — просто подстройка, когда слово "
        "вернётся. Если долго не открывал бота — сначала покажу то, что "
        "просрочено.\n\n"
        "За одну сессию — не больше 30 слов, чтобы не перегружаться. "
        "Обычно напоминаю сам 3 раза в день (10, 14, 18 по твоему времени) "
        "— получается около 90 слов в день. Если хочешь наверстать быстрее "
        "— можешь запускать /review вручную сколько угодно раз, каждый раз "
        "будет свежая порция.\n\n"
        "🧠 Мнемоника — на \"Почти помню\"/\"Сложно\" предложу ассоциацию для "
        "запоминания.\n\n"
        "🗑 /delete — удалить слово из словаря\n"
        "📊 /stats — сколько слов и как идёт прогресс\n\n"
        f"Пока тест: до {WEEKLY_NEW_WORD_LIMIT} новых слов в неделю "
        "(обновляется по понедельникам). Если что-то не работает — пиши Оле лично."
    )


# ---------------------------------------------------------------------------
# Ответ на вопрос про время из /start — перехватывается в handle_message()
# раньше добавления слова, пока не разберёт смещение (см. REMINDER_LOCAL_SLOTS
# выше и CLAUDE.md «Часовой пояс пользователя»).
# ---------------------------------------------------------------------------

_TZ_REPLY_RE = re.compile(r'^(\d{1,2})(?:[:.,](\d{2}))?\s*$')
# Escape hatch — without it, anyone who tries to add a word instead of
# answering (or just doesn't want to) gets stuck forever: every message they
# send afterward is intercepted here as a bad time reply, "не поняла" on
# repeat, and they can never add a word until they type digits. Any of these
# (case-insensitive) skips the question instead, defaulting to Moscow like
# an unanswered question already does.
_TZ_SKIP_WORDS = {"позже", "пропустить", "skip", "потом", "не знаю", "неважно"}


async def _handle_tz_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.lower() in _TZ_SKIP_WORDS:
        context.user_data.pop("awaiting_tz_reply", None)
        await update.message.reply_text(
            "Хорошо, пока буду ориентироваться на московское время — если что, "
            "можно уточнить позже через /start.\n\n"
            "Можешь присылать первое слово — например, conseguir или sin embargo 🙂"
        )
        return

    m = _TZ_REPLY_RE.match(text)
    local_hour = int(m.group(1)) if m else -1
    local_minute = int(m.group(2) or 0) if m else -1
    if not m or not (0 <= local_hour <= 23 and 0 <= local_minute <= 59):
        await update.message.reply_text(
            "Не поняла — напиши текущее время цифрами, например 14:30 или просто 14 "
            "(или «позже», если не хочешь отвечать сейчас)."
        )
        return

    now_utc = datetime.now(dt_timezone.utc)
    diff_minutes = (local_hour * 60 + local_minute) - (now_utc.hour * 60 + now_utc.minute)
    diff_minutes = ((diff_minutes + 720) % 1440) - 720  # normalize across midnight wraparound
    offset_hours = round(diff_minutes / 60)

    db.set_user_utc_offset(update.effective_user.id, offset_hours)
    context.user_data.pop("awaiting_tz_reply", None)
    await update.message.reply_text(
        "Записала! Можешь присылать первое слово — например, conseguir или "
        "sin embargo 🙂"
    )


# ---------------------------------------------------------------------------
# Недельный лимит на добавление новых слов — каждый вызов explain_word() стоит
# денег с личного ANTHROPIC_API_KEY владельца бота, поэтому у всех кроме
# OWNER_TELEGRAM_ID он ограничен. Сбрасывается по понедельникам (см.
# db._local_week_start()), а не "последние 7 дней" — сознательный выбор,
# см. CLAUDE.md.
# ---------------------------------------------------------------------------

def _is_owner(user_id: int) -> bool:
    return bool(OWNER_TELEGRAM_ID) and user_id == OWNER_TELEGRAM_ID


def _is_authorized(user_id: int) -> bool:
    return _is_owner(user_id) or db.is_user_authorized(user_id)


def _weekly_limit_reached(user_id: int, *, allow_owner_bypass: bool = True) -> bool:
    if allow_owner_bypass and _is_owner(user_id):
        return False
    return db.count_words_added_this_week(user_id) >= WEEKLY_NEW_WORD_LIMIT


def _mnemonic_retry_allowed(user_id: int, word_id: int) -> bool:
    if _is_owner(user_id):
        return True
    return db.get_mnemonic_retries(word_id) < MNEMONIC_RETRY_LIMIT


WEEKLY_LIMIT_MESSAGE = (
    f"На этой неделе лимит новых слов исчерпан ({WEEKLY_NEW_WORD_LIMIT}/неделю). "
    f"Лимит обновится в понедельник — то, что уже в словаре, никуда не денется 🙂"
)

# ---------------------------------------------------------------------------
# Per-user lock around "check weekly limit → call Claude → insert" — without
# it, two add-word requests for the same user_id that arrive close together
# can each see the limit as not-yet-reached (neither has committed its insert
# yet) and both proceed, bypassing WEEKLY_NEW_WORD_LIMIT. Telegram updates are
# already processed one at a time by PTB, so this mainly matters for the HTTP
# API below (aiohttp handles requests concurrently) — but it's shared so the
# Telegram and API entry points can't race against each other either.
# ---------------------------------------------------------------------------

_add_word_locks: dict[int, asyncio.Lock] = {}


def _get_add_word_lock(user_id: int) -> asyncio.Lock:
    lock = _add_word_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _add_word_locks[user_id] = lock
    return lock


# ---------------------------------------------------------------------------
# Гейт доступа — бот в закрытом тесте, пускаем только по инвайт-коду
# (см. CLAUDE.md «Доступ по инвайт-кодам»). Регистрируется в group=-1, раньше
# всех остальных хендлеров: неавторизованный пользователь получает
# ACCESS_TEST_MESSAGE и дальше update не идёт (ApplicationHandlerStop) — кроме
# /start, у него своя логика погашения кода в start() выше.
# ---------------------------------------------------------------------------

async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or _is_authorized(user.id):
        return

    message = update.effective_message
    if message is not None and message.text and message.text.startswith("/start"):
        return

    if update.callback_query:
        await update.callback_query.answer(ACCESS_TEST_MESSAGE, show_alert=True)
    elif message is not None:
        await message.reply_text(ACCESS_TEST_MESSAGE)
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Добавление слова через обычное сообщение
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_tz_reply"):
        await _handle_tz_reply(update, context)
        return

    word = update.message.text.strip()
    if not word or word.startswith("/"):
        return

    user_id = update.effective_user.id

    async with _get_add_word_lock(user_id):
        existing = db.find_word_by_phrase(user_id, word)
        if existing:
            await update.message.reply_text(
                f'📖 *{_md(existing["phrase"])}* уже есть в твоём словаре — не добавляю дубль.',
                parse_mode="Markdown",
            )
            return

        if _weekly_limit_reached(user_id):
            await update.message.reply_text(WEEKLY_LIMIT_MESSAGE)
            return

        await update.message.reply_text("Секунду, ищу...")

        try:
            info = await asyncio.to_thread(ai_helper.explain_word, word)
        except Exception:
            logger.exception("explain_word failed for %r", word)
            await update.message.reply_text(
                "Не получилось найти это слово — попробуй ещё раз через минуту."
            )
            return

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

        if not is_new:
            await update.message.reply_text(
                f'📖 *{_md(info.get("phrase", word))}* уже есть в твоём словаре — не добавляю дубль.',
                parse_mode="Markdown",
            )
            return

    examples_text = "\n".join(f"• {_md(e)}" for e in info.get("examples", []))
    conj_block = _format_conj_gerund(info.get("conjugation"), info.get("gerund"))
    collocations_block = _format_collocations(info.get("collocations"))

    user_offset = db.get_user_utc_offset(user_id)
    if user_offset is None:
        user_offset = DEFAULT_UTC_OFFSET
    window = db.get_current_window(user_offset)
    if window == 'morning':
        review_hint = "Первое повторение — сегодня вечером."
    else:
        review_hint = "Первое повторение — завтра утром."

    await update.message.reply_text(
        f'✅ *{_md(info.get("phrase", word))}*\n'
        f'{_md(info.get("meaning", ""))}\n'
        f'_{_md(info.get("part_of_speech", ""))}_\n\n'
        f'Примеры:\n{examples_text}'
        f'{conj_block}'
        f'{collocations_block}\n\n'
        f'{review_hint}',
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
            await update.message.reply_text(f'Слово *{_md(word)}* удалено из базы.', parse_mode="Markdown")
        else:
            await update.message.reply_text(
                f'Слово *{_md(word)}* не найдено. Напиши точно так, как оно сохранено.',
                parse_mode="Markdown",
            )
    else:
        words_list = db.get_user_words(update.effective_user.id)
        if not words_list:
            await update.message.reply_text("Твой словарь пуст.")
            return
        buttons = []
        for w in words_list[:20]:
            # callback_data must stay short (Telegram caps it at 64 bytes) —
            # use the word's id, never the phrase itself, which can be long
            # enough (multi-word collocations) to blow that limit and break
            # the whole menu.
            buttons.append([InlineKeyboardButton(w["phrase"], callback_data=f"del_word:{w['id']}")])
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
            f"Остальное подождёт следующей сессии (10:00 / 14:00 / 18:00 по твоему времени)."
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
    if not _is_owner(update.effective_user.id):
        return
    batches = db.reset_collected_review_dates(update.effective_user.id)
    if not batches:
        await update.message.reply_text("Нет слов в статусе «собрано» — распределять нечего.")
        return
    total = sum(c for _, c in batches)
    lines = "\n".join(f"{d}: {c} слов" for d, c in batches)
    await update.message.reply_text(
        f"Распределила {total} слов по датам:\n{lines}\n\nТеперь заходи в /review как обычно."
    )


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id):
        return
    code = db.create_invite(ttl_days=INVITE_TTL_DAYS)
    link = f"https://t.me/{context.bot.username}?start={code}"
    await update.message.reply_text(
        f"Приглашение (одноразовое, действует {INVITE_TTL_DAYS} дней, если не использовать):\n{link}"
    )


async def _send_backup(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    tmp_path = tempfile.mktemp(suffix=".db")
    try:
        await asyncio.to_thread(db.create_backup, tmp_path)
        with open(tmp_path, "rb") as f:
            await context.bot.send_document(
                chat_id,
                document=f,
                filename=f"spanish_vocab_bot_{date.today().isoformat()}.db",
                caption=f"Бэкап БД на {date.today().isoformat()}",
            )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id):
        return
    await _send_backup(update.effective_chat.id, context)


async def weekly_backup_job(context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_TELEGRAM_ID:
        return
    await _send_backup(OWNER_TELEGRAM_ID, context)


async def debug_due(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id):
        return
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
    if not _is_owner(user_id):
        return
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


def _mnemonic_keyboard(word_id: int, show_retry: bool = True) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton("Оставить ✅", callback_data=f"mnemo_keep:{word_id}")]
    if show_retry:
        buttons.append(InlineKeyboardButton("Другой вариант 🔄", callback_data=f"mnemo_retry:{word_id}"))
    return InlineKeyboardMarkup([buttons])


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
        if row is None or row["user_id"] != query.from_user.id:
            return
        examples = json.loads(row["examples"] or "[]")
        examples_text = "\n".join(f"• {_md(e)}" for e in examples)
        conj_block = _format_conj_gerund(row["conjugation"], row["gerund"])
        collocations_block = _format_collocations(json.loads(row["collocations"] or "[]"))

        try:
            await query.edit_message_text(
                f'*{_md(row["phrase"])}* — {_md(row["meaning"])}\n'
                f'_{_md(row["part_of_speech"])}_\n\n'
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
        except BadRequest as e:
            # A double-tap on "Показать ответ" sends two callback updates for
            # the same card in quick succession — the second edit targets
            # content Telegram considers identical/already-changed and raises
            # "message is not modified" (or similar). Harmless: the user
            # already sees the answer from the first tap, nothing to redo.
            if "not modified" not in str(e).lower():
                raise

    # --- self-assessment grade ---
    elif action == "grade":
        word_id = int(parts[1])
        grade = parts[2]
        row = db.get_word_by_id(word_id)
        if row is None or row["user_id"] != query.from_user.id:
            return
        db.mark_review_result(word_id, grade)
        row = db.get_word_by_id(word_id)
        marks = {"remember": "Помню 🟢", "almost": "Почти помню 🟡", "hard": "Сложно 🔴"}
        await query.edit_message_text(
            f'*{_md(row["phrase"])}* — {marks.get(grade, "")}',
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
        if row is None or row["user_id"] != query.from_user.id:
            return
        mnemonic = row["mnemonic"]
        if not mnemonic:
            try:
                mnemonic = await asyncio.to_thread(
                    ai_helper.get_mnemonic, row["phrase"], row["meaning"], row["part_of_speech"]
                )
            except Exception:
                logger.exception("mnemonic generation failed for word_id=%s", word_id)
                try:
                    await query.edit_message_text("Не получилось подобрать ассоциацию, попробуй позже.")
                except Exception:
                    pass
                return
            db.save_mnemonic(word_id, mnemonic)
        mnemonic = _strip_markdown_links(mnemonic)
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
        if row is None or row["user_id"] != query.from_user.id:
            return

        if not _mnemonic_retry_allowed(query.from_user.id, word_id):
            try:
                await query.edit_message_text(
                    _strip_markdown_links(row["mnemonic"]),
                    parse_mode="Markdown",
                    reply_markup=_mnemonic_keyboard(word_id, show_retry=False),
                )
            except Exception:
                pass
            await context.bot.send_message(
                query.message.chat_id,
                f"Лимит вариантов для этого слова исчерпан ({MNEMONIC_RETRY_LIMIT}) — оставляем этот 🙂",
            )
            return

        try:
            new_mnemonic = await asyncio.to_thread(
                ai_helper.get_mnemonic,
                row["phrase"], row["meaning"], row["part_of_speech"], avoid=row["mnemonic"],
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
        new_mnemonic = _strip_markdown_links(new_mnemonic)
        retries = db.increment_mnemonic_retries(word_id)
        show_retry = (
            _is_owner(query.from_user.id)
            or retries < MNEMONIC_RETRY_LIMIT
        )
        keyboard = _mnemonic_keyboard(word_id, show_retry=show_retry)
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
        word_id = int(parts[1])
        row = db.get_word_by_id(word_id)
        if row is None or row["user_id"] != query.from_user.id:
            return
        deleted = db.delete_word_by_id(word_id, query.from_user.id)
        if deleted:
            await query.edit_message_text(f'Слово *{_md(row["phrase"])}* удалено.', parse_mode="Markdown")
        else:
            await query.edit_message_text(f'Слово *{_md(row["phrase"])}* не найдено.', parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Daily reminders — checked hourly rather than three fixed UTC times, so each
# user gets their morning/midday/evening slot in their own local time (see
# REMINDER_LOCAL_SLOTS / get_user_utc_offset above). Runs every hour; a user's
# local hour only matches one slot at most once a day, so this doesn't double
# -send. See CLAUDE.md «Часовой пояс пользователя» for why hourly polling was
# chosen over scheduling a dynamic per-user job.
# ---------------------------------------------------------------------------

async def hourly_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    utc_hour = datetime.now(dt_timezone.utc).hour
    for user_id in db.get_all_due_users():
        # Defense in depth: due words for this user_id should be impossible
        # unless they're authorized (both add-word entry points check this
        # now), but if that ever changes or old data slips through, the bot
        # shouldn't proactively DM someone who was never let in.
        if not _is_authorized(user_id):
            continue
        offset = db.get_user_utc_offset(user_id)
        if offset is None:
            offset = DEFAULT_UTC_OFFSET
        local_hour = (utc_hour + offset) % 24
        greeting = REMINDER_LOCAL_SLOTS.get(local_hour)
        if greeting is None:
            continue

        count = db.count_due_not_reviewed_today(user_id)
        if count <= 0:
            continue
        session_count = min(count, SESSION_WORD_LIMIT)
        text = f"{greeting} Слов на эту сессию: {session_count}"
        if count > SESSION_WORD_LIMIT:
            text += f"\n(всего в очереди: {count})"
        await context.bot.send_message(user_id, text)
        user_data = context.application.user_data[user_id]
        user_data["review_shown"] = set()
        db.detect_and_mark_overdue(user_id)
        await _send_next_due(user_id, user_id, context, user_data)


# ---------------------------------------------------------------------------
# Study Coach — admin-only (OWNER_TELEGRAM_ID), hidden from regular users:
# not in set_my_commands, same as the /debug_* commands below. One message a
# day at 7:00 MSK with up to three independent blocks: the day's grammar
# slot (tutor-homework/listening/reading anchor, or a due grammar drill),
# 3 words to actively use in speech, and a due "use this topic in speech"
# nudge. See CLAUDE.md for the full weekly grid and why dates never get
# recalculated when a checkpoint has to wait for a free day.
# ---------------------------------------------------------------------------

STUDY_COACH_ANCHOR = {
    0: "📚 ДЗ репетитора — не забудь сделать.",
    1: "📚 Не забудь сделать ДЗ репетитора, если ещё не сделал(а).",
    2: "🎧 Аудирование — сегодня день послушать что-нибудь на испанском.",
    5: "📖 Чтение — сегодня день почитать что-нибудь на испанском.",
}

# Grammar due-topics only ever show on these days (Tue also gets the anchor
# above in the same slot); Mon/Wed/Sat are anchor-only and a due topic just
# waits for the next of these days.
STUDY_COACH_DUE_TOPIC_WEEKDAYS = (1, 3, 4, 6)

STUDY_COACH_STAGE_TEXT = {
    "x2": "✍️ Составь 10 предложений на тему «{topic}».",
    "x7": "🔁 Новый drills по «{topic}».",
    "x14": "🔁 Ещё раз drills по «{topic}» — третий подход.",
    "x30": "🔁 Финальные drills по «{topic}» — последний подход перед тем, как закрыть тему.",
}


def _build_study_coach_slot1(user_id: int, weekday: int) -> str:
    parts = []
    if weekday in STUDY_COACH_ANCHOR:
        parts.append(STUDY_COACH_ANCHOR[weekday])
    if weekday in STUDY_COACH_DUE_TOPIC_WEEKDAYS:
        due = db.get_due_grammar_item(user_id)
        if due:
            parts.append(STUDY_COACH_STAGE_TEXT[due["stage"]].format(topic=due["topic"]))
            db.mark_grammar_stage_sent(due["topic_id"], due["stage"])
    return "\n".join(parts)


async def study_coach_reminder(context: ContextTypes.DEFAULT_TYPE):
    if not OWNER_TELEGRAM_ID:
        return
    user_id = OWNER_TELEGRAM_ID
    weekday = date.today().weekday()
    blocks = []

    slot1 = _build_study_coach_slot1(user_id, weekday)
    if slot1:
        blocks.append(slot1)

    words = db.get_speech_activation_words(user_id, limit=3)
    if words:
        word_lines = "\n".join(f"• {w['phrase']} — {w['meaning']}" for w in words)
        blocks.append(f"🧠 Слова для использования в речи сегодня:\n{word_lines}")
        db.mark_speech_activation_shown([w["id"] for w in words])

    speaking_due = db.get_due_speaking_item(user_id)
    if speaking_due:
        blocks.append(f'🗣 Попробуй сегодня использовать в речи: «{speaking_due["topic"]}».')
        db.mark_speaking_sent(speaking_due["topic_id"])

    if not blocks:
        return
    await context.bot.send_message(user_id, "\n\n".join(blocks))


async def newtopic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /newtopic <тема>")
        return
    topic = " ".join(context.args).strip()
    topic_id = db.add_study_topic(update.effective_user.id, topic)
    row = db.get_study_topic(topic_id)
    await update.message.reply_text(
        f'✅ Тема добавлена: «{topic}» (id={topic_id})\n'
        f'X+2: {row["x2_date"]}\n'
        f'X+7: {row["x7_date"]}\n'
        f'X+10 (спикинг): {row["x10_date"]}\n'
        f'X+14: {row["x14_date"]}\n'
        f'X+30: {row["x30_date"]}'
    )


async def topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id):
        return
    rows = db.get_active_topics(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Очередь тем пуста.")
        return
    lines = []
    for r in rows:
        stage_bits = []
        for stage, label in (("x2", "X+2"), ("x7", "X+7"), ("x10", "X+10 🗣"), ("x14", "X+14"), ("x30", "X+30")):
            mark = "✅" if r[f"{stage}_sent"] else r[f"{stage}_date"]
            stage_bits.append(f"{label}: {mark}")
        lines.append(f'#{r["id"]} «{r["topic"]}» (добавлена {r["added_date"]})\n  ' + " | ".join(stage_bits))
    await update.message.reply_text("\n\n".join(lines))


async def canceltopic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /canceltopic <id>")
        return
    topic_id = int(context.args[0])
    ok = db.cancel_topic(update.effective_user.id, topic_id)
    await update.message.reply_text("Тема отменена." if ok else "Тема не найдена в активной очереди.")


# ---------------------------------------------------------------------------
# API — lets another site of yours read your word list, and (with a separate
# write key) add new words the same way typing to the bot directly would.
# Runs in the same process/container as the bot, reading/writing the same DB file.
# ---------------------------------------------------------------------------

_CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}


def _key_matches(candidate, expected: str | None) -> bool:
    """Constant-time key comparison — `candidate != expected` leaks timing
    info about how many leading characters matched (a real, if impractical
    over a real network, side channel). Also rejects up front when the
    expected key isn't configured, or candidate isn't a string (e.g. a
    malformed JSON body sending "key" as a number) — compare_digest requires
    matching str/bytes types on both sides and would raise otherwise."""
    if not expected or not isinstance(candidate, str) or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


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
    # Header preferred over the query param — a key in the URL ends up in
    # server/proxy access logs and (for any browser-facing caller) history and
    # Referer headers. Query param stays supported so existing callers aren't
    # broken; new/updated callers should send X-API-Key instead.
    key = request.headers.get("X-API-Key") or request.query.get("key")
    if not _key_matches(key, API_KEY):
        return web.json_response({"error": "unauthorized"}, status=401, headers=_CORS_HEADERS)

    user_id_param = request.query.get("user_id")
    if not user_id_param or not user_id_param.isdigit():
        return web.json_response(
            {"error": "user_id query param is required"}, status=400, headers=_CORS_HEADERS
        )

    user_id = int(user_id_param)
    # The invite gate is meant to cover the whole bot, not just Telegram
    # messages (see CLAUDE.md "Доступ по инвайт-кодам") — without this check
    # the API key alone would let a caller read any numeric user_id's words,
    # invited or not.
    if not _is_authorized(user_id):
        return web.json_response({"error": "user is not authorized for this bot"}, status=403, headers=_CORS_HEADERS)

    rows = db.get_words_for_export(user_id)
    words = [_word_row_to_dict(r) for r in rows]
    return web.json_response(words, headers=_CORS_HEADERS)


async def handle_api_add_word(request: web.Request) -> web.Response:
    if not API_WRITE_KEY:
        return web.json_response({"error": "unauthorized"}, status=401, headers=_CORS_HEADERS)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400, headers=_CORS_HEADERS)

    if not _key_matches(payload.get("key"), API_WRITE_KEY):
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

    # The invite gate is meant to cover the whole bot (see CLAUDE.md "Доступ
    # по инвайт-кодам"), but access_gate() only guards Telegram updates —
    # without this check, the write key alone would let a caller plant words
    # for ANY numeric user_id, invited or not. That matters beyond privacy:
    # hourly_reminder_job() below messages every user_id with due words with
    # no authorization check of its own, so an unauthorized id with planted
    # words would start getting proactively DMed by the bot.
    if not _is_authorized(user_id):
        return web.json_response(
            {"error": "user is not authorized for this bot"}, status=403, headers=_CORS_HEADERS
        )

    # Locked for the same reason as handle_message(): without it, concurrent
    # POST /words for the same user_id can all see the limit as not-yet-reached
    # and all proceed. Shared with the Telegram entry point so the two can't
    # race against each other either.
    async with _get_add_word_lock(user_id):
        existing = db.find_word_by_phrase(user_id, word)
        if existing:
            return web.json_response(
                {"is_new": False, "word": _word_row_to_dict(existing)},
                status=200,
                headers=_CORS_HEADERS,
            )

        # allow_owner_bypass=False: the API takes user_id from the request body, so
        # anyone holding API_WRITE_KEY could otherwise pass OWNER_TELEGRAM_ID and get
        # the owner's unlimited-words exemption for free. The bypass is only safe on
        # the Telegram side, where user_id comes from Telegram itself, not a client.
        if _weekly_limit_reached(user_id, allow_owner_bypass=False):
            return web.json_response(
                {"error": f"weekly limit of {WEEKLY_NEW_WORD_LIMIT} new words reached"},
                status=429,
                headers=_CORS_HEADERS,
            )

        try:
            info = await asyncio.to_thread(ai_helper.explain_word, word)
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
# Глобальный обработчик ошибок — иначе необработанное исключение в хендлере
# просто уходит в лог Railway, а пользователь молча не получает ответа.
# ---------------------------------------------------------------------------

ERROR_NOTIFY_THROTTLE_SECONDS = 60
_last_owner_error_notify_at = 0.0


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    global _last_owner_error_notify_at

    logger.error("Unhandled exception while processing update: %s", update, exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id, "Что-то пошло не так, попробуй ещё раз."
            )
        except Exception:
            logger.exception("Failed to notify user about the error")

    # Transient network blips between Railway and Telegram during long-polling
    # (getUpdates) surface here with update=None and a NetworkError — they
    # self-heal (PTB just retries the next poll) and happen to any polling
    # bot regardless of whether anyone's actually using it. Still logged
    # above, just not worth personally paging the owner for every one.
    if isinstance(context.error, NetworkError):
        return

    if not OWNER_TELEGRAM_ID:
        return

    now = time.monotonic()
    if now - _last_owner_error_notify_at < ERROR_NOTIFY_THROTTLE_SECONDS:
        return
    _last_owner_error_notify_at = now

    user_id = update.effective_user.id if isinstance(update, Update) and update.effective_user else "?"
    try:
        await context.bot.send_message(
            OWNER_TELEGRAM_ID,
            f"⚠️ Ошибка у пользователя {user_id}: {type(context.error).__name__}: {context.error}",
        )
    except Exception:
        logger.exception("Failed to notify owner about the error")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

PUBLIC_COMMANDS = [
    ("start", "Начать"),
    ("help", "Как всё устроено"),
    ("review", "Повторить слова по расписанию"),
    ("all", "Повторить все слова из базы"),
    ("delete", "Удалить слово из базы"),
    ("stats", "Статистика словаря"),
]

OWNER_ONLY_COMMANDS = [
    ("invite", "Сгенерировать инвайт-ссылку"),
    ("backup", "Прислать бэкап БД"),
    ("reset_collected", "Распределить collected-слова по датам"),
    ("debug_due", "История повторений"),
    ("debug_queue", "Текущая очередь /review"),
    ("newtopic", "Добавить тему для Study Coach"),
    ("topics", "Активные темы Study Coach"),
    ("canceltopic", "Отменить тему Study Coach"),
]


async def post_init(app: Application):
    await app.bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    if OWNER_TELEGRAM_ID:
        await app.bot.set_my_commands(
            PUBLIC_COMMANDS + OWNER_ONLY_COMMANDS,
            scope=BotCommandScopeChat(chat_id=OWNER_TELEGRAM_ID),
        )
    await start_api_server(app)


def main():
    db.init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler(filters.ALL, access_gate), group=-1)
    app.add_handler(CallbackQueryHandler(access_gate), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("review", review))
    app.add_handler(CommandHandler("all", review_all))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("invite", invite))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("reset_collected", reset_collected))
    app.add_handler(CommandHandler("debug_due", debug_due))
    app.add_handler(CommandHandler("debug_queue", debug_queue))
    app.add_handler(CommandHandler("newtopic", newtopic))
    app.add_handler(CommandHandler("topics", topics))
    app.add_handler(CommandHandler("canceltopic", canceltopic))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    app.job_queue.run_repeating(hourly_reminder_job, interval=3600, first=0)
    app.job_queue.run_daily(study_coach_reminder, time=dtime(hour=STUDY_COACH_UTC[0], minute=STUDY_COACH_UTC[1]))
    app.job_queue.run_daily(
        weekly_backup_job,
        time=dtime(hour=BACKUP_WEEKLY_UTC[0], minute=BACKUP_WEEKLY_UTC[1]),
        days=(BACKUP_WEEKDAY,),
    )

    print("Bot started. Stop with Ctrl+C.")
    app.run_polling()


if __name__ == "__main__":
    main()
