"""Одно соединение SQLite и замок, который сериализует доступ к нему.

Все обращения к базе идут через `transaction()` или `read()`. Внутри них нельзя
выполнять сетевые вызовы (ssh, Telegram): замок держится до выхода из блока.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Self

import aiosqlite

log = logging.getLogger(__name__)

MEMORY = ":memory:"


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path) -> Self:
        target = str(path)
        if target != MEMORY:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: транзакциями управляем сами (BEGIN IMMEDIATE).
        conn = await aiosqlite.connect(target, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        if target != MEMORY:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        """Доступ только на чтение, без блокировки записи для других процессов."""
        async with self._lock:
            yield self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """BEGIN IMMEDIATE; при исключении откат, иначе COMMIT."""
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                await self._rollback()
                raise
            await self._conn.execute("COMMIT")

    async def executescript(self, script: str) -> None:
        """Скрипт целиком, со своим BEGIN/COMMIT. Нужен для миграций."""
        async with self._lock:
            try:
                await self._conn.executescript(script)
            except BaseException:
                await self._rollback()
                raise

    async def user_version(self) -> int:
        async with self.read() as conn, conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        if row is None:
            raise RuntimeError("PRAGMA user_version ничего не вернул")
        return int(row[0])

    async def _rollback(self) -> None:
        try:
            await self._conn.execute("ROLLBACK")
        except Exception:
            # Транзакции уже нет (скрипт сам закончил её) или соединение закрыто.
            log.debug("rollback skipped", exc_info=True)
