"""Отправка сообщений клиентам и алертов в админ-чат."""

import asyncio
import html
import logging
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from plugbackup_bot.db import Database
from plugbackup_bot.ui.effects import Effects

log = logging.getLogger(__name__)

_MAX_RETRY_WAIT_S = 30
_ALERT_LIMIT = 3900


class Messenger(Protocol):
    async def send(
        self,
        tg_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        effect: bool = False,
    ) -> bool:
        """Отправляет HTML-текст клиенту. effect: с эффектом сообщения (если он включён).
        False, если доставить не удалось."""
        ...


class AdminAlerter(Protocol):
    async def alert(self, text: str) -> None:
        """Отправляет обычный текст в админ-чат. Сбой отправки не бросает исключение."""
        ...


class TelegramNotifier:
    def __init__(
        self, bot: Bot, db: Database, admin_chat_id: int, effects: Effects | None = None
    ) -> None:
        self._bot = bot
        self._effects = effects
        self._db = db
        self._admin_chat_id = admin_chat_id

    async def send(
        self,
        tg_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        effect: bool = False,
    ) -> bool:
        effect_id = self._effects.pick(effect) if self._effects is not None else None
        for attempt in (1, 2):
            try:
                await self._bot.send_message(
                    tg_id, text, reply_markup=reply_markup, message_effect_id=effect_id
                )
            except TelegramForbiddenError:
                log.info("user %s blocked the bot", tg_id)
                await self._mark_blocked(tg_id)
                return False
            except TelegramRetryAfter as exc:
                if attempt == 2:
                    return False
                await asyncio.sleep(min(exc.retry_after, _MAX_RETRY_WAIT_S))
            except TelegramAPIError:
                log.warning("could not send a message to %s", tg_id, exc_info=True)
                return False
            else:
                return True
        return False

    async def alert(self, text: str) -> None:
        try:
            await self._bot.send_message(
                self._admin_chat_id, html.escape(text[:_ALERT_LIMIT]), disable_web_page_preview=True
            )
        except TelegramAPIError:
            log.error("could not deliver an admin alert", exc_info=True)

    async def _mark_blocked(self, tg_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE users SET bot_blocked = 1 WHERE tg_id = ?", (tg_id,))
