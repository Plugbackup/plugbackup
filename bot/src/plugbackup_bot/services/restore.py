"""Заявки на восстановление из снимка. Автоматического отката нет: решает администратор."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

import aiosqlite

from plugbackup_bot.db import Database, audit
from plugbackup_bot.db.clients import Client, get_client, get_live_client
from plugbackup_bot.domain.roles import Actor, Permission
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
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreView:
    """Заявка целиком: для очереди и карточки в админке."""

    request_id: int
    status: str  # new, done или rejected
    snapshot_date: str
    note: str | None
    created_at: int
    handled_at: int | None
    handled_by: int | None
    client: Client
    username: str | None


MAX_NOTE = 500
_VIEW_SQL = (
    "SELECT r.id AS request_id, r.status AS req_status, r.snapshot_date, r.note,"
    " r.created_at AS req_created_at, r.handled_at, r.handled_by, u.username AS username, c.*"
    " FROM restore_requests r JOIN clients c ON c.id = r.client_id"
    " LEFT JOIN users u ON u.tg_id = c.tg_id"
)


def _view(row: aiosqlite.Row) -> RestoreView:
    return RestoreView(
        row["request_id"], row["req_status"], row["snapshot_date"], row["note"],
        row["req_created_at"], row["handled_at"], row["handled_by"], Client.from_row(row),
        row["username"],
    )  # fmt: skip


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
        self,
        request_id: int,
        status: str,
        note: str | None = None,
        actor: Actor | None = None,
    ) -> ResolvedRequest | None:
        """Закрывает заявку (done или rejected). None, если она уже обработана, не найдена или
        у администратора нет права. Комментарий уйдёт клиенту вместе с решением."""
        if status not in ("done", "rejected"):
            raise ValueError("статус заявки: done или rejected")
        if actor is not None and not actor.can(Permission.RESTORE):
            return None
        note = (note or "").strip()[:MAX_NOTE] or None
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
                "UPDATE restore_requests SET status = ?, note = ?, handled_at = ?, handled_by = ?"
                " WHERE id = ?",
                (status, note, now, actor.tg_id if actor else None, request_id),
            )
            if actor is not None:
                await audit.record(
                    conn, actor, "restore.resolve", now=now, target_type="restore",
                    target_id=request_id, status=status, comment=note is not None,
                )  # fmt: skip
            client = await get_client(conn, row["client_id"])
        if client is None:
            return None
        return ResolvedRequest(request_id, status, row["snapshot_date"], client, note)

    async def get(self, request_id: int) -> RestoreView | None:
        async with (
            self._db.read() as conn,
            conn.execute(f"{_VIEW_SQL} WHERE r.id = ?", (request_id,)) as cur,
        ):
            row = await cur.fetchone()
        return _view(row) if row else None

    async def page(
        self, *, handled: bool, limit: int, offset: int
    ) -> tuple[list[RestoreView], int]:
        """Очередь: новые заявки, самые старые первыми. Обработанные: свежие сверху."""
        if handled:
            where, order = "r.status <> 'new'", "r.handled_at DESC, r.id DESC"
        else:
            where, order = "r.status = 'new'", "r.created_at ASC, r.id ASC"
        async with self._db.read() as conn:
            async with conn.execute(
                f"SELECT COUNT(*) FROM restore_requests r WHERE {where}"  # noqa: S608
            ) as cur:
                total = int((await cur.fetchone())[0])  # type: ignore[index]
            async with conn.execute(
                f"{_VIEW_SQL} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cur:
                rows = [_view(row) for row in await cur.fetchall()]
        return rows, total

    async def open_count(self) -> int:
        async with (
            self._db.read() as conn,
            conn.execute("SELECT COUNT(*) FROM restore_requests WHERE status = 'new'") as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0
