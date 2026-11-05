"""Запасной вид для эффектов сообщений: отказ Telegram не должен терять сообщение.

Если запрос с message_effect_id отклонён, он повторяется без эффекта. Когда повтор проходит,
эффекты отключаются до перезапуска. Админу это не алерт, а строка в журнале: сообщение
дошло, потеряна только красота.
"""

import logging
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType

from plugbackup_bot.tg.emoji_fallback import UNRELATED_REFUSALS
from plugbackup_bot.ui.effects import Effects

log = logging.getLogger(__name__)


class EffectFallback(BaseRequestMiddleware):
    def __init__(self, effects: Effects) -> None:
        self._effects = effects

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
            if getattr(method, "message_effect_id", None) is None or any(
                text in reason.lower() for text in UNRELATED_REFUSALS
            ):
                raise
        result = await make_request(bot, method.model_copy(update={"message_effect_id": None}))
        if self._effects.degrade(reason):
            log.warning("Telegram refused a message effect, effects are off: %s", reason)
        return result
