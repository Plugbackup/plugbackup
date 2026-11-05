"""/emoji_preview: весь пак и сопоставление имён, чтобы проверить их глазами.

Анимацию эмодзи не видно ни в тестах, ни в журнале, поэтому владелец смотрит её сам и правит
emoji_map.json. Команда всегда отправляет настоящие кастомные эмодзи, независимо от флага.
"""

import asyncio
import html
from collections.abc import Collection, Iterator
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from plugbackup_bot.emoji import GLYPHS, NAMES, E
from plugbackup_bot.emoji_tools import EmojiToolError, Pack, read_pack
from plugbackup_bot.tg.admin_filter import IsAdmin
from plugbackup_bot.tg.emoji_fallback import probing

# В одном сообщении Telegram принимает до 100 сущностей, а на каждый эмодзи их две.
CHUNK = 40
# Пауза между сообщениями: Telegram ограничивает частоту отправки в один чат.
PAUSE_S = 0.2

NOOP = "emoji:noop"
RETRY = "emoji:retry"

NO_PACK = "Файла пака нет. Запустите на сервере scripts/scan_emoji.py и повторите команду."
REJECTED = (
    "Telegram отклонил кастомные эмодзи: {reason}\n"
    "Обычно это значит, что у владельца бота нет Premium. Пока так, бот показывает обычные "
    "эмодзи, и в этом случае ничего ломаться не должно."
)
BUTTONS_TITLE = (
    "Так выглядят кнопки: зелёная, красная и синяя, с иконками из пака. Если иконок нет, а "
    "текст есть, Telegram их не принял."
)
NOOP_ANSWER = "Кнопка для проверки, ничего не делает."
RETRIED = "Кастомные эмодзи включены снова."


def tg_emoji(custom_id: str, glyph: str) -> str:
    return f'<tg-emoji emoji-id="{custom_id}">{glyph}</tg-emoji>'


def status_text(pack: Pack) -> str:
    flag = "включён" if E.enabled else "выключен"
    if not E.enabled:
        now = "обычные эмодзи, кастомные выключены флагом"
    elif E.degraded_reason is not None:
        now = f"обычные эмодзи, Telegram отклонил кастомные: {html.escape(E.degraded_reason)}"
    else:
        now = "кастомные эмодзи"
    return (
        "<b>Эмодзи</b>\n"
        f"Флаг <code>USE_CUSTOM_EMOJI</code>: {flag}\n"
        f"Сейчас в сообщениях: {now}\n"
        f"Пак: <code>{html.escape(pack.set_name)}</code>, {len(pack.items)} шт., "
        f"просмотрен {html.escape(pack.scanned_at)}\n\n"
        "Ниже сопоставление «имя · обычный эмодзи · id» и весь пак. Чтобы заменить эмодзи, "
        "возьмите id из пака и впишите в <code>emoji_map.json</code>, затем перезапустите бота."
    )


def mapping_text() -> str:
    lines = ["<b>Сопоставление</b>"]
    for name in NAMES:
        custom = E.mapping()[name]
        glyph = GLYPHS[name]
        if custom is None:
            lines.append(f"{glyph} <code>{name}</code> · не задан, будет обычный")
        else:
            lines.append(
                f"{tg_emoji(custom, glyph)} <code>{name}</code> · {glyph} · <code>{custom}</code>"
            )
    return "\n".join(lines)


def pack_texts(pack: Pack) -> Iterator[str]:
    total = len(pack.items)
    for start in range(0, total, CHUNK):
        rows = [
            f"{start + offset}. {tg_emoji(item.custom_emoji_id, item.emoji or '▫')}"
            f" <code>{item.custom_emoji_id}</code>"
            for offset, item in enumerate(pack.items[start : start + CHUNK], start=1)
        ]
        yield f"<b>Пак, {start + 1}–{start + len(rows)} из {total}</b>\n" + "\n".join(rows)


def sample_markup() -> InlineKeyboardMarkup:
    mapping = E.mapping()

    def button(text: str, name: str, style: str | None) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text, callback_data=NOOP, style=style, icon_custom_emoji_id=mapping[name]
        )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("Оплатить", "payment", "success"), button("Удалить", "error", "danger")],
            [button("Открыть", "star", "primary"), button("Назад", "back", None)],
        ]
    )


def retry_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Включить кастомные снова", callback_data=RETRY)]
        ]
    )


async def cmd_emoji_preview(message: Message, emoji_pack_file: Path) -> None:
    try:
        pack = read_pack(emoji_pack_file)
    except EmojiToolError as error:
        await message.answer(html.escape(str(error)))
        return
    markup = retry_markup() if E.enabled and E.degraded_reason is not None else None
    try:
        with probing():
            await message.answer(status_text(pack), reply_markup=markup)
            for text in (mapping_text(), *pack_texts(pack)):
                await asyncio.sleep(PAUSE_S)
                await message.answer(text)
            await asyncio.sleep(PAUSE_S)
            await message.answer(BUTTONS_TITLE, reply_markup=sample_markup())
    except TelegramBadRequest as error:
        await message.answer(html.escape(REJECTED.format(reason=error.message)))


async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer(NOOP_ANSWER)


async def cb_retry(callback: CallbackQuery) -> None:
    E.retry()
    await callback.answer(RETRIED)


def build_router(admin_ids: Collection[int]) -> Router:
    router = Router(name="emoji_preview")
    only_admins = IsAdmin(admin_ids)
    router.message.filter(only_admins)
    router.callback_query.filter(only_admins)
    router.message.register(cmd_emoji_preview, Command("emoji_preview"))
    router.callback_query.register(cb_noop, F.data == NOOP)
    router.callback_query.register(cb_retry, F.data == RETRY)
    return router
