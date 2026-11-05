"""Инвайты в админке: создание с лимитом и сроком, список, отзыв, кто воспользовался."""

from dataclasses import dataclass
from enum import StrEnum

import aiosqlite

from plugbackup_bot.db import Database, audit
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.domain.roles import Actor
from plugbackup_bot.infra.clock import Clock

MAX_INVITES_AT_ONCE = 50
MAX_USES = 1000
MAX_EXPIRY_DAYS = 365
DAY = 24 * 3600


class InviteStatus(StrEnum):
    ACTIVE = "active"
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class InviteRow:
    id: int
    code_tail: str | None
    created_by: int
    created_at: int
    max_uses: int
    uses: int
    expires_at: int | None
    revoked_at: int | None

    def status(self, now: int) -> InviteStatus:
        if self.revoked_at is not None:
            return InviteStatus.REVOKED
        if self.expires_at is not None and self.expires_at <= now:
            return InviteStatus.EXPIRED
        if self.uses >= self.max_uses:
            return InviteStatus.USED
        return InviteStatus.ACTIVE

    @property
    def label(self) -> str:
        return f"…{self.code_tail}" if self.code_tail else f"#{self.id}"


@dataclass(frozen=True, slots=True)
class InviteUse:
    tg_id: int
    used_at: int
    username: str | None
    login: str | None


@dataclass(frozen=True, slots=True)
class InviteDetails:
    invite: InviteRow
    uses: tuple[InviteUse, ...]


class RevokeKind(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    ALREADY = "already"


_COLUMNS = "rowid AS id, code_tail, created_by, created_at, max_uses, uses, expires_at, revoked_at"


def _row(r: aiosqlite.Row) -> InviteRow:
    return InviteRow(
        r["id"], r["code_tail"], r["created_by"], r["created_at"], r["max_uses"], r["uses"],
        r["expires_at"], r["revoked_at"],
    )  # fmt: skip


class InviteService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def create(
        self, actor: Actor, count: int, *, max_uses: int = 1, expires_days: int | None = None
    ) -> list[str]:
        if not 1 <= count <= MAX_INVITES_AT_ONCE:
            raise ValueError(f"от 1 до {MAX_INVITES_AT_ONCE} кодов за раз")
        if not 1 <= max_uses <= MAX_USES:
            raise ValueError(f"от 1 до {MAX_USES} использований")
        if expires_days is not None and not 1 <= expires_days <= MAX_EXPIRY_DAYS:
            raise ValueError(f"срок от 1 до {MAX_EXPIRY_DAYS} дней")
        now = self._clock.now()
        expires_at = now + expires_days * DAY if expires_days else None
        async with self._db.transaction() as conn:
            codes = await invites_repo.create_invites(
                conn, count, actor.tg_id, now, max_uses=max_uses, expires_at=expires_at
            )
            await audit.record(
                conn, actor, "invite.create", now=now, target_type="invite", count=count,
                max_uses=max_uses, expires_days=expires_days or 0,
            )  # fmt: skip
        return codes

    async def page(self, *, limit: int, offset: int) -> tuple[list[InviteRow], int, int]:
        """Страница инвайтов (новые сверху), общее число и число действующих."""
        now = self._clock.now()
        async with self._db.read() as conn:
            async with conn.execute("SELECT COUNT(*) FROM invites") as cur:
                total = int((await cur.fetchone())[0])  # type: ignore[index]
            async with conn.execute(
                f"SELECT {_COLUMNS} FROM invites ORDER BY rowid DESC LIMIT ? OFFSET ?",  # noqa: S608
                (limit, offset),
            ) as cur:
                rows = [_row(r) for r in await cur.fetchall()]
            active = await invites_repo.count_unused(conn, now)
        return rows, total, active

    async def details(self, invite_id: int) -> InviteDetails | None:
        async with self._db.read() as conn:
            async with conn.execute(
                f"SELECT {_COLUMNS}, code_hash FROM invites WHERE rowid = ?",  # noqa: S608
                (invite_id,),
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            async with conn.execute(
                "SELECT u.tg_id, u.used_at, usr.username,"
                " (SELECT MAX(c.id) FROM clients c WHERE c.tg_id = u.tg_id) AS client_id"
                " FROM invite_uses u LEFT JOIN users usr ON usr.tg_id = u.tg_id"
                " WHERE u.code_hash = ? ORDER BY u.used_at, u.tg_id",
                (row["code_hash"],),
            ) as cur:
                uses = tuple(
                    InviteUse(
                        r["tg_id"], r["used_at"], r["username"],
                        f"u{1000 + r['client_id']}" if r["client_id"] else None,
                    )
                    for r in await cur.fetchall()
                )  # fmt: skip
        return InviteDetails(_row(row), uses)

    async def revoke(self, invite_id: int, actor: Actor) -> RevokeKind:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            async with conn.execute(
                "SELECT revoked_at FROM invites WHERE rowid = ?", (invite_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return RevokeKind.NOT_FOUND
            if row["revoked_at"] is not None:
                return RevokeKind.ALREADY
            await conn.execute(
                "UPDATE invites SET revoked_at = ? WHERE rowid = ?", (now, invite_id)
            )
            await audit.record(
                conn, actor, "invite.revoke", now=now, target_type="invite", target_id=invite_id
            )
        return RevokeKind.OK
