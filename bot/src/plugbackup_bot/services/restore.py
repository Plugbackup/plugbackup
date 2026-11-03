"""Заявки на восстановление из снимка. Автоматического отката нет: решает администратор."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from plugbackup_bot.db import Database
from plugbackup_bot.db.clients import Client, get_client, get_live_client
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.clock import Clock

SNAPSHOT_DAYS = 7
MAX_REQUESTS_PER_DAY = 3
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DAY = 24 * 3600


class RestoreKind(StrEnum):
    OK = "ok"
    NO_CLIENT = "no_client"
    BLOCKED = "blocked"
    BAD_DATE = "bad_date"
    OPEN_EXISTS = "open_exists"
    TOO_MANY = "too_many"


@dataclass(frozen=True, slots=True)
class RestoreResult:
    kind: RestoreKind
    request_id: int | None = None
    client: Client | None = None


@dataclass(frozen=True, slots=True)
class ResolvedRequest:
    request_id: int
    status: str
    snapshot_date: str
    client: Client


class RestoreService:
    def __init__(self, db: Database, clock: Clock, default_tz: str) -> None:
        self._db = db
        self._clock = clock
        self._tz = ZoneInfo(default_tz)

    def available_dates(self) -> list[str]:
        """Даты снимков за последние 7 суток, от новых к старым. Наличие проверяет админ."""
        today = datetime.fromtimestamp(self._clock.now(), self._tz).date()
        return [(today - timedelta(days=i)).isoformat() for i in range(SNAPSHOT_DAYS)]

    async def eligible_client(self, tg_id: int) -> RestoreResult:
        async with self._db.read() as conn:
            client = await get_live_client(conn, tg_id)
        if client is None or client.state is State.PENDING:
            return RestoreResult(RestoreKind.NO_CLIENT)
        if client.state is State.SUSPENDED:
            return RestoreResult(RestoreKind.BLOCKED, client=client)
        return RestoreResult(RestoreKind.OK, client=client)

    async def create(self, tg_id: int, snapshot_date: str) -> RestoreResult:
        if not _DATE_RE.fullmatch(snapshot_date) or snapshot_date not in self.available_dates():
            return RestoreResult(RestoreKind.BAD_DATE)
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_live_client(conn, tg_id)
            if client is None or client.state is State.PENDING:
                return RestoreResult(RestoreKind.NO_CLIENT)
            if client.state is State.SUSPENDED:
                return RestoreResult(RestoreKind.BLOCKED, client=client)
            async with conn.execute(
                "SELECT status, created_at FROM restore_requests WHERE client_id = ?"
                " AND (status = 'new' OR created_at > ?)",
                (client.id, now - _DAY),
            ) as cur:
                recent = await cur.fetchall()
            if any(row["status"] == "new" for row in recent):
                return RestoreResult(RestoreKind.OPEN_EXISTS, client=client)
            if len(recent) >= MAX_REQUESTS_PER_DAY:
                return RestoreResult(RestoreKind.TOO_MANY, client=client)
            cur = await conn.execute(
                "INSERT INTO restore_requests (client_id, snapshot_date, created_at)"
                " VALUES (?, ?, ?)",
                (client.id, snapshot_date, now),
            )
        return RestoreResult(RestoreKind.OK, cur.lastrowid, client)

    async def resolve(
        self, request_id: int, status: str, note: str | None = None
    ) -> ResolvedRequest | None:
        """Закрывает заявку (done или rejected). None, если она уже обработана или не найдена."""
        if status not in ("done", "rejected"):
            raise ValueError("статус заявки: done или rejected")
        now = self._clock.now()
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT client_id, snapshot_date FROM restore_requests"
                " WHERE id = ? AND status = 'new'",
                (request_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE restore_requests SET status = ?, note = ?, handled_at = ? WHERE id = ?",
                (status, note, now, request_id),
            )
            client = await get_client(conn, row["client_id"])
        if client is None:
            return None
        return ResolvedRequest(request_id, status, row["snapshot_date"], client)

    async def open_count(self) -> int:
        async with (
            self._db.read() as conn,
            conn.execute("SELECT COUNT(*) FROM restore_requests WHERE status = 'new'") as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0
