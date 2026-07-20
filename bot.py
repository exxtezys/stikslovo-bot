"""
StickerBot v2 — Telegram inline bot for sticker-by-word search.

Features:
  1. Send a sticker → auto-detect its pack → offer one-tap import of all stickers.
  2. Favorites system: import all, mark favorites, only favorites appear in search.
  3. @botname word → search favorite stickers.

Commands:
  /start        — welcome & help
  /mysets       — list imported packs with favorite counts
  /pack Name    — browse stickers in a pack, tap to toggle favorite
  /import Name  — manual import of a sticker pack
  /remove       — remove last sticker
  /clear        — remove ALL stickers
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedSticker,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
    CallbackContext,
)

import config
from database import (
    add_sticker,
    add_stickers_bulk,
    get_pack_stickers,
    get_sticker,
    get_user_sets,
    get_user_sticker_count,
    init_db,
    remove_all_user_stickers,
    remove_sticker,
    search_stickers,
    set_favorite,
    toggle_favorite,
    user_has_stickers_from_set,
)
from emoji_map import WORD_INDEX, EMOJI_MAP

# ── AI Words ─────────────────────────────────────────────────────────
try:
    from ai_words import generate_words as _ai_generate_words, get_cached_words as _ai_cached
    _AI_AVAILABLE = bool(config.GEMINI_API_KEY or config.GROQ_API_KEY or config.HF_API_KEY)
except Exception:
    _AI_AVAILABLE = False
    _ai_generate_words = None  # type: ignore
    _ai_cached = None  # type: ignore

# Runtime AI word cache: emoji → [words]
_ai_word_cache: dict[str, list[str]] = {}

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
)
logger = logging.getLogger("stickerbot")

# ── Help Texts ───────────────────────────────────────────────────────

START_TEXT = """🎯 <b>Привет! Я — бот для подбора стикеров по словам.</b>

Выбери действие:"""

MENU_IMPORT_TEXT = """📦 <b>Как импортировать стикеры</b>

<b>Способ 1 (проще):</b>
Просто отправь мне <b>один стикер</b> из любого пака — я сам найду весь пак и предложу импортировать всё одной кнопкой.

<b>Способ 2:</b>
<code>/import ИмяПака</code> — например <code>/import FunnyCats</code>

⬇️ Пришли мне стикер прямо сейчас!"""

MENU_FAVS_TEXT = """⭐ <b>Как работает избранное</b>

1. Когда ты импортируешь пак, <b>все стикеры сохраняются</b>, но <b>не показываются</b> в поиске
2. Отправь мне нужный стикер из пака <b>ещё раз</b> — он станет ⭐ избранным
3. Отправь тот же стикер снова — уберётся из избранного

📌 <b>В поиске участвуют ТОЛЬКО избранные стикеры.</b>

Так ты можешь импортировать хоть 500 стикеров, а в поиске оставить только 20 нужных."""

MENU_SEARCH_TEXT = """🔍 <b>Как искать стикеры</b>

В <b>любом чате</b> (личка, группа) начни писать:

<code>@stikslovo_bot слово</code>

И бот покажет подходящие стикеры из твоего избранного.

Примеры слов:
• <code>@stikslovo_bot смех</code> → 😂
• <code>@stikslovo_bot любовь</code> → ❤️
• <code>@stikslovo_bot привет</code> → 👋
• <code>@stikslovo_bot огонь</code> → 🔥

💡 Работает и на русском, и на английском."""

MENU_ABOUT_TEXT = """ℹ️ <b>О боте</b>

StickerBot v2 — поиск стикеров по словам как в TikTok.

<b>Команды:</b>
/start — главное меню
/mysets — список паков и избранного
/pack ИмяПака — посмотреть стикеры в паке
/import ИмяПака — импорт вручную
/remove — удалить последний
/clear — удалить всё

Словарь: 767 эмодзи, 2317 слов (РУС + ENG).
Данные хранятся локально, только твои."""


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the main menu inline keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Импортировать стикеры", callback_data="menu:import")],
        [InlineKeyboardButton("📊 Мои стикеры и паки", callback_data="menu:mysets")],
        [InlineKeyboardButton("⭐ Как работает избранное", callback_data="menu:favs")],
        [InlineKeyboardButton("🔍 Как искать стикеры", callback_data="menu:search")],
        [InlineKeyboardButton("ℹ️ О боте и команды", callback_data="menu:about")],
    ])


def _back_button(text: str = "🔙 Назад в меню") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text, callback_data="menu:start")],
    ])


# ── Callback data prefixes ───────────────────────────────────────────
CB_IMPORT = "import:"
CB_FAV = "fav:"
CB_MENU = "menu:"
CB_PACK = "pack:"


# ── Handlers ─────────────────────────────────────────────────────────

async def start(update: Update, _context: CallbackContext) -> None:
    """Welcome message with interactive menu."""
    keyboard = _main_menu_keyboard()
    if update.message:
        await update.message.reply_html(START_TEXT, reply_markup=keyboard)
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            START_TEXT, parse_mode="HTML", reply_markup=keyboard
        )


async def handle_sticker(update: Update, _context: CallbackContext) -> None:
    """When user sends a sticker:
    - If new + has set_name → offer import whole pack
    - If new + no set_name → save as favorite
    - If already exists → toggle favorite
    """
    msg = update.message
    user_id = msg.from_user.id
    sticker = msg.sticker

    if not sticker:
        return

    file_unique_id = sticker.file_unique_id
    file_id = sticker.file_id
    emoji = sticker.emoji or ""
    set_name = sticker.set_name or ""

    if not emoji:
        await msg.reply_text("⚠️ У этого стикера нет эмодзи — пропускаю.")
        return

    # Check if already in DB
    existing = await get_sticker(user_id, file_unique_id)

    if existing:
        # ── Already saved → toggle favorite ──
        new_fav = await toggle_favorite(user_id, file_unique_id)
        emoji_text = existing["emoji"]
        word_preview = _word_preview(emoji_text)
        if new_fav:
            await msg.reply_text(
                f"⭐ <b>Избранное!</b>\n"
                f"Эмодзи: {emoji_text}\n"
                f"Слова: {word_preview}\n\n"
                f"Теперь этот стикер будет в поиске.",
                parse_mode="HTML",
            )
        else:
            await msg.reply_text(
                f"🚫 <b>Убрано из избранного.</b>\n"
                f"Стикер больше не показывается в поиске.\n"
                f"Отправь ещё раз — вернётся в избранное.",
                parse_mode="HTML",
            )
        return

    # ── New sticker ──
    # If it has a set_name and user hasn't imported this pack → offer import
    if set_name and await user_has_stickers_from_set(user_id, set_name) == 0:
        # Save this single sticker as not-favorite for now
        await add_sticker(user_id, file_unique_id, file_id, emoji, is_favorite=0, set_name=set_name)

        # Fetch pack info to show title & count
        try:
            pack = await _context.bot.get_sticker_set(set_name)
            pack_title = pack.title
            pack_size = len(pack.stickers)
        except Exception:
            pack_title = set_name
            pack_size = "?"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"📦 Импортировать «{pack_title}» ({pack_size} стикеров)",
                callback_data=f"{CB_IMPORT}{set_name}"
            )],
            [InlineKeyboardButton("❌ Нет, спасибо", callback_data=f"{CB_IMPORT}skip:{set_name}")],
        ])
        await msg.reply_text(
            f"🎯 Этот стикер из пака <b>«{pack_title}»</b> ({pack_size} стикеров).\n\n"
            f"Импортировать весь пак? Стикеры сохранятся, но в поиске будут только те, "
            f"которые ты отметишь ⭐ избранными.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        return

    # No set_name or pack already imported → save as favorite
    await add_sticker(user_id, file_unique_id, file_id, emoji, is_favorite=1, set_name=set_name)
    total = await get_user_sticker_count(user_id)
    word_preview = _word_preview(emoji)

    # Fire-and-forget AI word generation for this emoji
    _trigger_ai_words(emoji)

    await msg.reply_text(
        f"✅ <b>Стикер сохранён!</b> ⭐\n"
        f"Эмодзи: {emoji}\n"
        f"Слова: {word_preview}\n"
        f"Всего стикеров: {total}\n\n"
        f"Попробуй: <code>@{_context.bot.username} {emoji}</code>",
        parse_mode="HTML",
    )


async def handle_callback(update: Update, _context: CallbackContext) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = query.from_user.id

    # ── Menu callbacks ──
    if data.startswith(CB_MENU):
        _ = data[len(CB_MENU):]

        if _ == "start":
            await start(update, _context)
            return
        elif _ == "import":
            await query.edit_message_text(
                MENU_IMPORT_TEXT, parse_mode="HTML", reply_markup=_back_button()
            )
            return
        elif _ == "mysets":
            sets = await get_user_sets(user_id)
            total = await get_user_sticker_count(user_id)
            if not sets and total == 0:
                text = "📭 У тебя пока нет стикеров.\n\nОтправь мне любой стикер, чтобы начать!"
            else:
                lines = ["📊 <b>Твои стикеры:</b>\n"]
                if sets:
                    for s in sets:
                        lines.append(
                            f"• <b>{s['set_name']}</b>: {s['total']} стикеров, "
                            f"⭐{s['favorites'] or 0} избранных"
                        )
                lines.append(f"\nВсего: <b>{total}</b> стикеров")
            await query.edit_message_text(
                "\n".join(lines), parse_mode="HTML", reply_markup=_back_button()
            )
            return
        elif _ == "favs":
            await query.edit_message_text(
                MENU_FAVS_TEXT, parse_mode="HTML", reply_markup=_back_button()
            )
            return
        elif _ == "search":
            await query.edit_message_text(
                MENU_SEARCH_TEXT, parse_mode="HTML", reply_markup=_back_button()
            )
            return
        elif _ == "about":
            await query.edit_message_text(
                MENU_ABOUT_TEXT, parse_mode="HTML", reply_markup=_back_button()
            )
            return

    # ── Import callback ──
    if data.startswith(CB_IMPORT):
        payload = data[len(CB_IMPORT):]

        if payload.startswith("skip:"):
            set_name = payload[5:]
            await query.edit_message_text(
                f"👌 Ок, пак «{set_name}» не буду импортировать.\n"
                f"Если передумаешь — отправь <code>/import {set_name}</code>",
                parse_mode="HTML",
            )
            return

        # Import the pack
        set_name = payload
        await query.edit_message_text(f"⏳ Импортирую пак «{set_name}»...")

        try:
            sticker_set = await _context.bot.get_sticker_set(set_name)
        except Exception as e:
            await query.edit_message_text(
                f"❌ Ошибка при импорте пака: {e}", parse_mode="HTML"
            )
            return

        stickers = sticker_set.stickers
        entries: list[tuple[int, str, str, str, int, str]] = []
        for s in stickers:
            entries.append((
                user_id, s.file_unique_id, s.file_id,
                s.emoji or "", 0, set_name,  # is_favorite=0 by default
            ))

        added = await add_stickers_bulk(entries)
        total = await get_user_sticker_count(user_id)
        skipped = len(stickers) - added

        await query.edit_message_text(
            f"✅ Пак <b>{sticker_set.title}</b> импортирован!\n"
            f"Добавлено: <b>{added}</b> стикеров\n"
            f"Уже было: <b>{skipped}</b>\n"
            f"Всего у тебя: <b>{total}</b>\n\n"
            f"⭐ <b>Чтобы отметить избранные:</b>\n"
            f"1. Нажми кнопку ниже — покажу все стикеры пака\n"
            f"2. Или просто <b>отправь мне стикер</b> из клавиатуры — я переключу ⭐",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"📦 Смотреть стикеры «{sticker_set.title}»",
                    callback_data=f"pack:{set_name}"
                )],
                [InlineKeyboardButton("🔙 В меню", callback_data="menu:start")],
            ]),
        )

    elif data.startswith(CB_PACK):
        # Browse pack from callback
        set_name = data[len(CB_PACK):]
        stickers = await get_pack_stickers(user_id, set_name, limit=50)
        if not stickers:
            await query.edit_message_text(
                f"❌ Пак <code>{set_name}</code> не найден.\n"
                f"Импортируй: <code>/import {set_name}</code>",
                parse_mode="HTML",
                reply_markup=_back_button(),
            )
            return
        fav_count = sum(1 for s in stickers if s["is_favorite"])
        await query.edit_message_text(
            f"📦 <b>{set_name}</b> — {len(stickers)} стикеров, ⭐{fav_count} избранных\n\n"
            f"⬇️ <b>Отправляй стикеры из клавиатуры</b> чтобы отмечать ⭐ избранные\n"
            f"Или используй команду <code>/pack {set_name}</code> чтобы посмотреть каждый",
            parse_mode="HTML",
            reply_markup=_back_button(),
        )

    elif data.startswith(CB_FAV):
        # Toggle favorite from /pack browse
        file_unique_id = data[len(CB_FAV):]
        new_fav = await toggle_favorite(user_id, file_unique_id)
        if new_fav is None:
            await query.edit_message_text("⚠️ Стикер не найден в базе.")
            return

        status = "⭐" if new_fav else "🚫"
        # Don't re-edit the whole message — just answer the callback.
        # The user sees the sticker they tapped.
        await query.answer(f"{status} {'Избранное' if new_fav else 'Убрано'}")


async def mysets(update: Update, _context: CallbackContext) -> None:
    """Show all imported packs with stats."""
    user_id = update.message.from_user.id
    sets = await get_user_sets(user_id)
    total = await get_user_sticker_count(user_id)

    if not sets:
        if total == 0:
            await update.message.reply_text(
                "📭 У тебя пока нет стикеров.\n"
                "Отправь мне любой стикер, чтобы начать!"
            )
        else:
            await update.message.reply_text(
                f"📊 У тебя <b>{total}</b> стикеров (без пака).\n"
                f"Используй: <code>@{_context.bot.username} слово</code>",
                parse_mode="HTML",
            )
        return

    lines = [f"📊 <b>Твои стикер-паки:</b>\n"]
    for s in sets:
        fav = s["favorites"] or 0
        t = s["total"]
        lines.append(
            f"• <b>{s['set_name']}</b>: {t} стикеров, ⭐{fav} избранных  "
            f"<code>/pack {s['set_name']}</code>"
        )
    lines.append(f"\nВсего: <b>{total}</b> стикеров")
    lines.append(f"Поиск: <code>@{_context.bot.username} слово</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def browse_pack(update: Update, _context: CallbackContext) -> None:
    """Browse stickers in a pack with favorite toggles.
    Usage: /pack SetName"""
    msg = update.message
    user_id = msg.from_user.id
    args = msg.text.split(maxsplit=1)

    if len(args) < 2:
        await msg.reply_text(
            "📦 Укажи имя пака: <code>/pack ИмяПака</code>\n\n"
            "Список паков: <code>/mysets</code>",
            parse_mode="HTML",
        )
        return

    set_name = args[1].strip()
    stickers = await get_pack_stickers(user_id, set_name)

    if not stickers:
        await msg.reply_text(
            f"❌ Пак <code>{set_name}</code> не найден или пуст.\n"
            f"Импортируй его: <code>/import {set_name}</code>",
            parse_mode="HTML",
        )
        return

    fav_count = sum(1 for s in stickers if s["is_favorite"])
    total = len(stickers)
    header = (
        f"📦 <b>{set_name}</b> — {total} стикеров, ⭐{fav_count} избранных\n\n"
        f"Нажимай на стикер, чтобы добавить/убрать из избранного:\n"
    )

    # Send first batch (max 30 per message due to inline keyboard limits)
    batch = stickers[:30]
    for s in batch:
        emoji_display = s["emoji"] or "—"
        fav_mark = "⭐" if s["is_favorite"] else "○"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"{fav_mark} {emoji_display} {'Убрать' if s['is_favorite'] else 'В избранное'}",
                callback_data=f"{CB_FAV}{s['file_unique_id']}",
            )
        ]])
        await msg.reply_sticker(
            sticker=s["file_id"],
            reply_markup=keyboard,
        )

    if len(stickers) > 30:
        await msg.reply_text(
            f"Показано 30 из {total} стикеров. "
            f"Пришли ещё раз <code>/pack {set_name}</code> для остальных.",
            parse_mode="HTML",
        )
    else:
        await msg.reply_text(f"✅ Все {total} стикеров показаны. Нажимай ⭐ чтобы отметить избранные.")


async def import_set(update: Update, _context: CallbackContext) -> None:
    """Manual import: /import SetName"""
    msg = update.message
    user_id = msg.from_user.id
    args = msg.text.split(maxsplit=1)

    if len(args) < 2:
        await msg.reply_text(
            "📦 <b>Импорт стикер-пака</b>\n\n"
            "Имя пака:\n<code>/import ИмяПака</code>\n\n"
            "Или ссылка:\n<code>/import https://t.me/addstickers/SetName</code>\n\n"
            "💡 <b>Проще:</b> отправь мне ОДИН стикер из пака — я сам предложу импорт.",
            parse_mode="HTML",
        )
        return

    set_name = args[1].strip()
    if "/" in set_name:
        set_name = set_name.rstrip("/").split("/")[-1]
    if set_name.startswith("addstickers/"):
        set_name = set_name.split("/", 1)[1]

    try:
        sticker_set = await _context.bot.get_sticker_set(set_name)
    except Exception as e:
        await msg.reply_text(
            f"❌ Пак <code>{set_name}</code> не найден.\nОшибка: {e}",
            parse_mode="HTML",
        )
        return

    stickers = sticker_set.stickers
    existing_count = await user_has_stickers_from_set(user_id, set_name)

    if existing_count >= len(stickers):
        await msg.reply_text(
            f"ℹ️ Пак <b>{sticker_set.title}</b> уже полностью импортирован ({len(stickers)} стикеров).",
            parse_mode="HTML",
        )
        return

    status = await msg.reply_text(
        f"⏳ Импортирую <b>{sticker_set.title}</b> ({len(stickers)} стикеров)...",
        parse_mode="HTML",
    )

    entries: list[tuple[int, str, str, str, int, str]] = []
    for s in stickers:
        entries.append((
            user_id, s.file_unique_id, s.file_id,
            s.emoji or "", 0, set_name,
        ))

    added = await add_stickers_bulk(entries)
    total = await get_user_sticker_count(user_id)
    skipped = len(stickers) - added

    await status.edit_text(
        f"✅ Пак <b>{sticker_set.title}</b> импортирован!\n"
        f"Добавлено: <b>{added}</b> стикеров\n"
        f"Уже было: <b>{skipped}</b>\n"
        f"Всего у тебя: <b>{total}</b>\n\n"
        f"⭐ Отметь избранные: <code>/pack {set_name}</code>",
        parse_mode="HTML",
    )


# ── Remove / Clear ───────────────────────────────────────────────────

_last_sticker: dict[int, str] = {}


async def track_sticker(update: Update, _context: CallbackContext) -> None:
    """Track last sticker for /remove."""
    msg = update.message
    if msg.sticker:
        _last_sticker[msg.from_user.id] = msg.sticker.file_unique_id


async def remove_last(update: Update, _context: CallbackContext) -> None:
    """Remove last sticker."""
    user_id = update.message.from_user.id
    if user_id not in _last_sticker:
        await update.message.reply_text("⚠️ Нечего удалять. Сначала отправь стикер.")
        return
    file_unique_id = _last_sticker.pop(user_id)
    removed = await remove_sticker(user_id, file_unique_id)
    count = await get_user_sticker_count(user_id)
    if removed:
        await update.message.reply_text(f"🗑 Стикер удалён. Осталось: {count}")
    else:
        await update.message.reply_text("⚠️ Не найден.")


async def clear_all(update: Update, _context: CallbackContext) -> None:
    """Remove all stickers."""
    user_id = update.message.from_user.id
    count = await get_user_sticker_count(user_id)
    if count == 0:
        await update.message.reply_text("📭 Нечего удалять.")
        return
    deleted = await remove_all_user_stickers(user_id)
    await update.message.reply_text(f"🗑 Удалено <b>{deleted}</b> стикеров.", parse_mode="HTML")


# ── Inline Query ─────────────────────────────────────────────────────

async def inline_query(update: Update, _context: CallbackContext) -> None:
    """Handle @botname word — search only favorite stickers."""
    query = update.inline_query
    text = (query.query or "").strip().lower()
    user_id = query.from_user.id

    if not text:
        await query.answer([], cache_time=config.INLINE_CACHE_TIME)
        return

    # Map query → matching emojis (static + AI)
    query_words = text.split()
    matching_emojis: set[str] = set()
    for word in query_words:
        if word in WORD_INDEX:
            matching_emojis.update(WORD_INDEX[word])
        # Also check AI-generated words
        for emoji, ai_words in _ai_word_cache.items():
            if word in ai_words:
                matching_emojis.add(emoji)
    if text in EMOJI_MAP:
        matching_emojis.add(text)

    if not matching_emojis:
        await query.answer(
            [], cache_time=config.INLINE_CACHE_TIME,
            switch_pm_text="🔍 Словарь слов",
            switch_pm_parameter="help",
        )
        return

    # Search only favorites
    results_db = await search_stickers(user_id, list(matching_emojis), limit=50, favorites_only=True)

    if not results_db:
        await query.answer(
            [], cache_time=config.INLINE_CACHE_TIME,
            switch_pm_text="⭐ Нет избранных. Отправь мне стикер!",
            switch_pm_parameter="start",
        )
        return

    inline_results: list[InlineQueryResultCachedSticker] = []
    seen: set[str] = set()
    for row in results_db:
        fid = row["file_id"]
        if fid in seen:
            continue
        seen.add(fid)
        inline_results.append(InlineQueryResultCachedSticker(
            id=fid[:64], sticker_file_id=fid,
        ))

    await query.answer(inline_results, cache_time=config.INLINE_CACHE_TIME, is_personal=True)


# ── Helpers ──────────────────────────────────────────────────────────

def _word_preview(emoji: str, max_words: int = 5) -> str:
    words = EMOJI_MAP.get(emoji, [emoji])
    preview = ", ".join(words[:max_words])
    if len(words) > max_words:
        preview += "..."
    return preview


def _trigger_ai_words(emoji: str) -> None:
    """Generate AI words in background for an emoji (fire-and-forget)."""
    if not _AI_AVAILABLE or emoji in _ai_word_cache:
        return

    async def _run():
        try:
            words = await _ai_generate_words(emoji)  # type: ignore
            if words:
                _ai_word_cache[emoji] = words
                logger.debug("AI words cached for %s: %s", emoji, words[:3])
        except Exception as e:
            logger.debug("AI words background error for %s: %s", emoji, e)

    asyncio.create_task(_run())


# ── Error handler ────────────────────────────────────────────────────

async def error_handler(update: object, context: CallbackContext) -> None:
    logger.error("Update %s error: %s", update, context.error)


# ── Main ─────────────────────────────────────────────────────────────

def main() -> None:
    """Build and run the bot — auto-detects webhook (Render) vs polling (local)."""
    print("Starting StickerBot v2...", flush=True)

    try:
        asyncio.run(init_db())
        print("DB initialized", flush=True)
    except Exception as e:
        print(f"DB init failed: {e}", flush=True)
        raise

    # Load cached AI words on startup
    if _AI_AVAILABLE:
        try:
            import json
            cache_file = Path(__file__).parent / "ai_words_cache.json"
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                _ai_word_cache.update(cached)
                logger.info("Loaded %d cached AI word sets", len(cached))
        except Exception:
            pass

    print(f"Building app with token: {config.BOT_TOKEN[:8]}...", flush=True)
    app = Application.builder().token(config.BOT_TOKEN).build()

    # ── Handlers ──
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("mysets", mysets))
    app.add_handler(CommandHandler("pack", browse_pack))
    app.add_handler(CommandHandler("import", import_set))
    app.add_handler(CommandHandler("remove", remove_last))
    app.add_handler(CommandHandler("clear", clear_all))

    app.add_handler(
        MessageHandler(
            filters.Sticker.ALL & ~filters.VIA_BOT, track_sticker, block=False
        ),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.Sticker.ALL & ~filters.VIA_BOT, handle_sticker),
        group=1,
    )
    app.add_handler(InlineQueryHandler(inline_query))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    # ── Start ──
    if config.RENDER:
        webhook_url = config.WEBHOOK_URL
        if not webhook_url:
            print("ERROR: RENDER=true but WEBHOOK_URL is not set!", flush=True)
            print("Add WEBHOOK_URL env var in Render dashboard → Environment", flush=True)
            # Start anyway — Render needs port binding
            webhook_url = "https://placeholder.set.your.webhook.url"

        webhook_url = webhook_url.rstrip("/") + "/webhook"
        print(f"Webhook mode: {webhook_url} on port {config.PORT}", flush=True)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        print("Polling mode", flush=True)
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
