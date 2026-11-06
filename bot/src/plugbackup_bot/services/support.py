"""Обращения в поддержку: клиент пишет боту, администратор отвечает из админки.

У человека одно незакрытое обращение: новые сообщения дополняют его. Статусы: open (ждёт
ответа), answered (поддержка ответила), closed. Тексты переписки хранятся в ticket_messages, в
журнал действий администраторов они не попадают.
"""

from dataclasses import dataclass
from enum import StrEnum

import aiosqlite

from plugbackup_bot.db import Database, audit
from plugbackup_bot.domain.logins import make_login
from plugbackup_bot.domain.roles import Actor, Permission
from plugbackup_bot.infra.clock import Clock

MAX_TEXT = 2000
MAX_MESSAGES = 100


class TicketStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CLOSED = "closed"


class WriteKind(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    FULL = "full"


class ReplyKind(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    CLOSED = "closed"
    DENIED = "denied"


class ReopenKind(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    NOT_CLOSED = "not_closed"
    OTHER_OPEN = "other_open"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class Ticket:
    id: int
    tg_id: int
    status: TicketStatus
    created_at: int
    updated_at: int
    closed_at: int | None
    closed_by: int | None


@dataclass(frozen=True, slots=True)
class TicketMessage:
    author: str  # client или admin
    admin_id: int | None
    text: str
    at: int


@dataclass(frozen=True, slots=True)
class TicketRow:
    """Строка очереди: обращение и то, что нужно, чтобы понять, чьё оно."""

    ticket: Ticket
    login: str | None
    username: str | None
    client_id: int | None


@dataclass(frozen=True, slots=True)
class TicketView:
    row: TicketRow
    messages: tuple[TicketMessage, ...]


@dataclass(frozen=True, slots=True)
class WriteResult:
    kind: WriteKind
    ticket_id: int | None = None
    created: bool = False


@dataclass(frozen=True, slots=True)
class ReplyResult:
    kind: ReplyKind
    ticket: Ticket | None = None


def _ticket(row: aiosqlite.Row) -> Ticket:
    return Ticket(
        row["id"], row["tg_id"], TicketStatus(row["status"]), row["created_at"],
        row["updated_at"], row["closed_at"], row["closed_by"],
    )  # fmt: skip


_ROW_SQL = (
    "SELECT t.*, u.username AS username,"
    " (SELECT c.id FROM clients c WHERE c.tg_id = t.tg_id ORDER BY c.id DESC LIMIT 1)"
    "   AS client_id"
    " FROM tickets t LEFT JOIN users u ON u.tg_id = t.tg_id"
)


def _row(row: aiosqlite.Row) -> TicketRow:
    client_id = row["client_id"]
    login = make_login(client_id) if client_id is not None else None
    return TicketRow(_ticket(row), login, row["username"], client_id)


class SupportService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    # --- сторона клиента ---

    async def write(self, tg_id: int, text: str) -> WriteResult:
        """Сообщение клиента: открывает обращение или дополняет текущее."""
        text = text.strip()
        if not 1 <= len(text) <= MAX_TEXT:
            return WriteResult(WriteKind.INVALID)
        now = self._clock.now()
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT id FROM tickets WHERE tg_id = ? AND status <> 'closed'", (tg_id,)
            ) as cur:
                current = await cur.fetchone()
            created = current is None
            if current is None:
                cur2 = await conn.execute(
                    "INSERT INTO tickets (tg_id, created_at, updated_at) VALUES (?, ?, ?)",
                    (tg_id, now, now),
                )
                ticket_id = int(cur2.lastrowid or 0)
            else:
                ticket_id = int(current["id"])
                async with conn.execute(
                    "SELECT COUNT(*) FROM ticket_messages WHERE ticket_id = ?", (ticket_id,)
                ) as cur3:
                    count = int((await cur3.fetchone())[0])  # type: ignore[index]
                if count >= MAX_MESSAGES:
                    return WriteResult(WriteKind.FULL, ticket_id)
                await conn.execute(
                    "UPDATE tickets SET status = 'open', updated_at = ? WHERE id = ?",
                    (now, ticket_id),
                )
            await conn.execute(
                "INSERT INTO ticket_messages (ticket_id, author, text, at)"
                " VALUES (?, 'client', ?, ?)",
                (ticket_id, text, now),
            )
        return WriteResult(WriteKind.OK, ticket_id, created)

    async def current(self, tg_id: int) -> Ticket | None:
        """Незакрытое обращение человека."""
        async with (
            self._db.read() as conn,
            conn.execute(
                "SELECT * FROM tickets WHERE tg_id = ? AND status <> 'closed'", (tg_id,)
            ) as cur,
        ):
            row = await cur.fetchone()
        return _ticket(row) if row else None

    async def close_own(self, tg_id: int) -> Ticket | None:
        """Клиент закрывает своё обращение. None, если закрывать нечего."""
        now = self._clock.now()
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM tickets WHERE tg_id = ? AND status <> 'closed'", (tg_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = NULL,"
                " updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
        return _ticket(row)

    # --- сторона администратора ---

    async def reply(self, ticket_id: int, actor: Actor, text: str) -> ReplyResult:
        if not actor.can(Permission.TICKETS):
            return ReplyResult(ReplyKind.DENIED)
        text = text.strip()
        if not 1 <= len(text) <= MAX_TEXT:
            return ReplyResult(ReplyKind.INVALID)
        now = self._clock.now()
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return ReplyResult(ReplyKind.NOT_FOUND)
            if row["status"] == "closed":
                return ReplyResult(ReplyKind.CLOSED, _ticket(row))
            await conn.execute(
                "UPDATE tickets SET status = 'answered', updated_at = ? WHERE id = ?",
                (now, ticket_id),
            )
            await conn.execute(
                "INSERT INTO ticket_messages (ticket_id, author, admin_id, text, at)"
                " VALUES (?, 'admin', ?, ?, ?)",
                (ticket_id, actor.tg_id, text, now),
            )
            await audit.record(
                conn, actor, "ticket.reply", now=now, target_type="ticket", target_id=ticket_id,
                length=len(text),
            )  # fmt: skip
        return ReplyResult(ReplyKind.OK, _ticket(row))

    async def close(self, ticket_id: int, actor: Actor) -> Ticket | None:
        """Закрывает обращение. None: не найдено, уже закрыто или нет права."""
        if not actor.can(Permission.TICKETS):
            return None
        now = self._clock.now()
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT * FROM tickets WHERE id = ? AND status <> 'closed'", (ticket_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            await conn.execute(
                "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?,"
                " updated_at = ? WHERE id = ?",
                (now, actor.tg_id, now, ticket_id),
            )
            await audit.record(
                conn, actor, "ticket.close", now=now, target_type="ticket", target_id=ticket_id
            )
        return _ticket(row)

    async def reopen(self, ticket_id: int, actor: Actor) -> ReopenKind:
        if not actor.can(Permission.TICKETS):
            return ReopenKind.DENIED
        now = self._clock.now()
        async with self._db.transaction() as conn:
            async with conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return ReopenKind.NOT_FOUND
            if row["status"] != "closed":
                return ReopenKind.NOT_CLOSED
            async with conn.execute(
                "SELECT 1 FROM tickets WHERE tg_id = ? AND status <> 'closed'", (row["tg_id"],)
            ) as cur:
                if await cur.fetchone() is not None:
                    return ReopenKind.OTHER_OPEN
            await conn.execute(
                "UPDATE tickets SET status = 'open', closed_at = NULL, closed_by = NULL,"
                " updated_at = ? WHERE id = ?",
                (now, ticket_id),
            )
            await audit.record(
                conn, actor, "ticket.reopen", now=now, target_type="ticket", target_id=ticket_id
            )
        return ReopenKind.OK

    # --- чтение ---

    async def get(self, ticket_id: int) -> TicketView | None:
        async with self._db.read() as conn:
            async with conn.execute(f"{_ROW_SQL} WHERE t.id = ?", (ticket_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            async with conn.execute(
                "SELECT author, admin_id, text, at FROM ticket_messages WHERE ticket_id = ?"
                " ORDER BY id",
                (ticket_id,),
            ) as cur:
                messages = tuple(
                    TicketMessage(m["author"], m["admin_id"], m["text"], m["at"])
                    for m in await cur.fetchall()
                )
        return TicketView(_row(row), messages)

    async def page(self, *, closed: bool, limit: int, offset: int) -> tuple[list[TicketRow], int]:
        """Очередь: сначала ждущие ответа, самые старые первыми. Закрытые: свежие сверху."""
        if closed:
            where, order = "t.status = 'closed'", "t.updated_at DESC, t.id DESC"
        else:
            where = "t.status <> 'closed'"
            order = "(t.status = 'open') DESC, t.updated_at ASC, t.id ASC"
        async with self._db.read() as conn:
            async with conn.execute(
                f"SELECT COUNT(*) FROM tickets t WHERE {where}"  # noqa: S608
            ) as cur:
                total = int((await cur.fetchone())[0])  # type: ignore[index]
            async with conn.execute(
                f"{_ROW_SQL} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cur:
                rows = [_row(row) for row in await cur.fetchall()]
        return rows, total

    async def waiting_count(self) -> int:
        """Сколько обращений ждёт ответа поддержки."""
        async with (
            self._db.read() as conn,
            conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'open'") as cur,
        ):
            row = await cur.fetchone()
        return int(row[0]) if row else 0
