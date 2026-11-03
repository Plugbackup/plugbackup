"""Рассылка администратора всем пользователям бота."""

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable

from plugbackup_bot.db import Database
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.notify import Messenger

log = logging.getLogger(__name__)

MAX_TEXT = 3500


class BroadcastService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        messenger: Messenger,
        *,
        rate_per_s: float = 20,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._db = db
        self._clock = clock
        self._messenger = messenger
        self._delay = 1 / rate_per_s
        self._sleep = sleep

    async def recipients(self) -> list[int]:
        """Все, кто писал боту и не заблокировал его."""
        async with (
            self._db.read() as conn,
            conn.execute("SELECT tg_id FROM users WHERE bot_blocked = 0 ORDER BY tg_id") as cur,
        ):
            return [row[0] for row in await cur.fetchall()]

    async def create(self, admin_id: int, text: str) -> int:
        text = text.strip()
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f"текст рассылки: от 1 до {MAX_TEXT} символов")
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO broadcasts (text, created_by, created_at) VALUES (?, ?, ?)",
                (text, admin_id, self._clock.now()),
            )
        if cur.lastrowid is None:
            raise RuntimeError("INSERT не вернул id рассылки")
        return cur.lastrowid

    async def run(self, broadcast_id: int) -> tuple[int, int]:
        """Отправляет рассылку не быстрее rate_per_s сообщений в секунду. Возвращает (ушло, нет)."""
        async with (
            self._db.read() as conn,
            conn.execute("SELECT text FROM broadcasts WHERE id = ?", (broadcast_id,)) as cur,
        ):
            row = await cur.fetchone()
        if row is None:
            raise LookupError(f"рассылка {broadcast_id} не найдена")
        body = html.escape(row["text"])
        sent = failed = 0
        for tg_id in await self.recipients():
            try:
                ok = await self._messenger.send(tg_id, body)
            except Exception:
                log.exception("broadcast to %s failed", tg_id)
                ok = False
            sent, failed = (sent + 1, failed) if ok else (sent, failed + 1)
            await self._sleep(self._delay)
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE broadcasts SET sent = ?, failed = ? WHERE id = ?",
                (sent, failed, broadcast_id),
            )
        return sent, failed
