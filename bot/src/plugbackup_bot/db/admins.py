"""Администраторы и их роли."""

from collections.abc import Collection
from dataclasses import dataclass

import aiosqlite

from plugbackup_bot.domain.roles import Role


@dataclass(frozen=True, slots=True)
class AdminRow:
    tg_id: int
    role: Role
    source: str  # env: из .env, bot: добавлен владельцем из бота
    added_by: int | None
    added_at: int


async def get_role(conn: aiosqlite.Connection, tg_id: int) -> Role | None:
    async with conn.execute("SELECT role FROM admins WHERE tg_id = ?", (tg_id,)) as cur:
        row = await cur.fetchone()
    return Role(row["role"]) if row else None


async def list_admins(conn: aiosqlite.Connection) -> list[AdminRow]:
    async with conn.execute("SELECT * FROM admins ORDER BY role, tg_id") as cur:
        rows = await cur.fetchall()
    return [
        AdminRow(r["tg_id"], Role(r["role"]), r["source"], r["added_by"], r["added_at"])
        for r in rows
    ]


async def add_support(conn: aiosqlite.Connection, tg_id: int, added_by: int, now: int) -> bool:
    """Добавляет помощника. False, если этот человек уже администратор."""
    cur = await conn.execute(
        "INSERT OR IGNORE INTO admins (tg_id, role, source, added_by, added_at)"
        " VALUES (?, 'support', 'bot', ?, ?)",
        (tg_id, added_by, now),
    )
    return cur.rowcount == 1


async def remove_support(conn: aiosqlite.Connection, tg_id: int) -> bool:
    """Убирает помощника. Владельцев и записи из .env отсюда удалить нельзя."""
    cur = await conn.execute(
        "DELETE FROM admins WHERE tg_id = ? AND role = 'support' AND source = 'bot'", (tg_id,)
    )
    return cur.rowcount == 1


async def sync_env_owners(conn: aiosqlite.Connection, owner_ids: Collection[int], now: int) -> None:
    """Приводит записи из .env в соответствие с ADMIN_IDS: новые владельцы добавляются, убранные
    из .env теряют права. Помощники, добавленные из бота, не трогаются."""
    for tg_id in owner_ids:
        await conn.execute(
            "INSERT INTO admins (tg_id, role, source, added_by, added_at)"
            " VALUES (?, 'owner', 'env', NULL, ?)"
            " ON CONFLICT (tg_id) DO UPDATE SET role = 'owner', source = 'env', added_by = NULL",
            (tg_id, now),
        )
    marks = ", ".join("?" for _ in owner_ids) or "NULL"
    await conn.execute(
        f"DELETE FROM admins WHERE source = 'env' AND tg_id NOT IN ({marks})",  # noqa: S608
        tuple(owner_ids),
    )
