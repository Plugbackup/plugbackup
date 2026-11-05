"""Запасной вид для кастомных эмодзи.

Telegram принимает кастомные эмодзи в сообщениях бота и на его кнопках, только если у владельца
бота есть Premium. Если запрос с ними отклонён, он повторяется с обычными эмодзи; когда повтор
проходит, бот переходит на обычные эмодзи насовсем и один раз сообщает об этом админу.
Хуже всего, что мог бы сделать бот, это не доставить сообщение, поэтому ловится любой отказ.
"""

import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import InlineKeyboardMarkup

from plugbackup_bot.emoji import EmojiRegistry

log = logging.getLogger(__name__)

_TG_EMOJI = re.compile(r'<tg-emoji\s+emoji-id="[0-9]+">(.*?)</tg-emoji>', re.DOTALL)
_probe: ContextVar[bool] = ContextVar("emoji_probe", default=False)

# Отказы по другим причинам: повтор без эмодзи их не лечит, а если проходит (например, правка
# «без изменений»), это не повод отключать кастомные эмодзи.
_UNRELATED = (
    "message is not modified",
    "message to edit not found",
    "message to delete not found",
    "message can't be edited",
    "message is too long",
    "message_too_long",
    "message text is empty",
    "chat not found",
    "reply message not found",
    "wrong file identifier",
    "query is too old",
    "user not found",
)


@contextmanager
def probing() -> Iterator[None]:
    """Внутри блока отказы Telegram не маскируются: так /emoji_preview видит настоящий ответ."""
    token = _probe.set(True)
    try:
        yield
    finally:
        _probe.reset(token)


def strip_custom_emoji(html: str) -> str:
    """<tg-emoji emoji-id="…">🔒</tg-emoji> -> 🔒"""
    return _TG_EMOJI.sub(r"\1", html)


def _plain_markup(markup: InlineKeyboardMarkup, registry: EmojiRegistry) -> InlineKeyboardMarkup:
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for button in row:
            if button.icon_custom_emoji_id:
                glyph = registry.glyph_for_id(button.icon_custom_emoji_id)
                text = f"{glyph} {button.text}" if glyph else button.text
                button = button.model_copy(update={"icon_custom_emoji_id": None, "text": text})
            new_row.append(button)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plain_updates(method: TelegramMethod[Any], registry: EmojiRegistry) -> dict[str, Any]:
    """Поля метода, которые нужно заменить, чтобы убрать кастомные эмодзи. Пусто: их нет."""
    updates: dict[str, Any] = {}
    for field in ("text", "caption"):
        value = getattr(method, field, None)
        if isinstance(value, str) and "<tg-emoji" in value:
            updates[field] = strip_custom_emoji(value)
    markup = getattr(method, "reply_markup", None)
    if isinstance(markup, InlineKeyboardMarkup) and any(
        button.icon_custom_emoji_id for row in markup.inline_keyboard for button in row
    ):
        updates["reply_markup"] = _plain_markup(markup, registry)
    media = getattr(method, "media", None)
    caption = getattr(media, "caption", None)
    if isinstance(caption, str) and "<tg-emoji" in caption:
        updates["media"] = media.model_copy(update={"caption": strip_custom_emoji(caption)})
    return updates


class EmojiFallback(BaseRequestMiddleware):
    def __init__(
        self,
        registry: EmojiRegistry,
        on_degraded: Callable[[str], Awaitable[None]],
    ) -> None:
        self._registry = registry
        self._on_degraded = on_degraded

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Any:
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as error:
            reason = error.message
            if _probe.get() or any(text in reason.lower() for text in _UNRELATED):
                raise
            updates = plain_updates(method, self._registry)
            if not updates:
                raise
        # Повтор вне блока except: ошибка повтора не должна цепляться к первой.
        result = await make_request(bot, method.model_copy(update=updates))
        if self._registry.degrade(reason):
            log.warning("Telegram refused custom emoji, switched to plain ones: %s", reason)
            try:
                await self._on_degraded(reason)
            except Exception:
                log.exception("could not report the emoji fallback")
        return result
