"""Ограничение частоты запросов и учёт пользователей."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db.users import touch_user
from plugbackup_bot.infra.clock import Clock

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
_PRUNE_AT = 10_000


class ThrottleMiddleware(BaseMiddleware):
    """Корзина токенов на пользователя: burst запросов сразу, затем per_second в секунду.

    Платежи не ограничиваются: сообщение об оплате нельзя терять.
    """

    def __init__(
        self,
        *,
        burst: int = 6,
        per_second: float = 0.5,
        notice_gap_s: float = 10.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst = burst
        self._per_second = per_second
        self._notice_gap_s = notice_gap_s
        self._now = now
        self._buckets: dict[int, tuple[float, float]] = {}
        self._last_notice: dict[int, float] = {}

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is None or (isinstance(event, Message) and event.successful_payment):
            return await handler(event, data)
        if self._take(user.id):
            return await handler(event, data)
        log.info("user %s throttled", user.id)
        await self._warn(event, user.id)
        return None

    def _take(self, user_id: int) -> bool:
        now = self._now()
        tokens, last = self._buckets.get(user_id, (float(self._burst), now))
        tokens = min(float(self._burst), tokens + (now - last) * self._per_second)
        allowed = tokens >= 1
        self._buckets[user_id] = (tokens - 1 if allowed else tokens, now)
        if len(self._buckets) > _PRUNE_AT:
            self._prune(now)
        return allowed

    def _prune(self, now: float) -> None:
        idle = self._burst / self._per_second
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < idle}
        self._last_notice = {k: v for k, v in self._last_notice.items() if now - v < idle}

    async def _warn(self, event: TelegramObject, user_id: int) -> None:
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(T.THROTTLED)
            elif isinstance(event, Message):
                now = self._now()
                if now - self._last_notice.get(user_id, -self._notice_gap_s) >= self._notice_gap_s:
                    self._last_notice[user_id] = now
                    await event.answer(T.THROTTLED)
        except TelegramAPIError:
            log.debug("could not send the throttle notice", exc_info=True)


class UserMiddleware(BaseMiddleware):
    """Запоминает пользователя и снимает отметку «заблокировал бота», когда он пишет снова."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user is not None and not user.is_bot:
            async with self._db.transaction() as conn:
                await touch_user(conn, user.id, self._clock.now())
        return await handler(event, data)


class AttemptLimiter:
    """Ограничивает неудачные попытки (например, ввод неверных инвайт-кодов)."""

    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_s: float = 600,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window_s = window_s
        self._now = now
        self._failures: dict[int, list[float]] = {}

    def _recent(self, key: int) -> list[float]:
        cutoff = self._now() - self._window_s
        recent = [ts for ts in self._failures.get(key, []) if ts > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: int) -> bool:
        return len(self._recent(key)) >= self._max_failures

    def fail(self, key: int) -> None:
        self._failures[key] = [*self._recent(key), self._now()]

    def reset(self, key: int) -> None:
        self._failures.pop(key, None)
