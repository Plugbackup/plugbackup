"""Журнал действий администраторов. Запись идёт в той же транзакции, что и само действие."""

import json
from dataclasses import dataclass

import aiosqlite

from plugbackup_bot.domain.roles import Actor


@dataclass(frozen=True, slots=True)
class AuditEntry:
    id: int
    at: int
    actor_id: int
    actor_role: str
    action: str
    target_type: str | None
    target_id: str | None
    details: dict[str, object]


async def record(
    conn: aiosqlite.Connection,
    actor: Actor,
    action: str,
    *,
    now: int,
    target_type: str | None = None,
    target_id: object = None,
    **details: object,
) -> None:
    """Пишет действие в журнал. В details кладём числа и короткие метки, не тексты сообщений."""
    await conn.execute(
        "INSERT INTO audit_log (at, actor_id, actor_role, action, target_type, target_id, details)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            actor.tg_id,
            actor.role.value,
            action,
            target_type,
            None if target_id is None else str(target_id),
            json.dumps(details, ensure_ascii=False, sort_keys=True) if details else "",
        ),
    )


def _entry(row: aiosqlite.Row) -> AuditEntry:
    raw = row["details"]
    try:
        details = json.loads(raw) if raw else {}
    except ValueError:
        details = {}
    return AuditEntry(
        row["id"], row["at"], row["actor_id"], row["actor_role"], row["action"],
        row["target_type"], row["target_id"], details,
    )  # fmt: skip


async def count(conn: aiosqlite.Connection, actor_id: int | None = None) -> int:
    where = " WHERE actor_id = ?" if actor_id else ""
    async with conn.execute(
        f"SELECT COUNT(*) FROM audit_log{where}",  # noqa: S608
        (actor_id,) if actor_id else (),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def page(
    conn: aiosqlite.Connection, *, limit: int, offset: int, actor_id: int | None = None
) -> list[AuditEntry]:
    """Записи от новых к старым."""
    where = " WHERE actor_id = ?" if actor_id else ""
    params = (actor_id, limit, offset) if actor_id else (limit, offset)
    async with conn.execute(
        f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",  # noqa: S608
        params,
    ) as cur:
        rows = await cur.fetchall()
    return [_entry(row) for row in rows]
