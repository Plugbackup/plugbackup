"""Согласие клиента с рисками беты: версия показанного текста и время."""

import aiosqlite

from plugbackup_bot.db.users import ensure_user


async def ack_risk(conn: aiosqlite.Connection, tg_id: int, version: int, now: int) -> None:
    await ensure_user(conn, tg_id, now)
    await conn.execute(
        "INSERT INTO risk_acks (tg_id, text_version, acked_at) VALUES (?, ?, ?)",
        (tg_id, version, now),
    )


async def has_acked(conn: aiosqlite.Connection, tg_id: int, version: int) -> bool:
    """Согласие именно с этой версией текста. После правки текста спрашиваем заново."""
    async with conn.execute(
        "SELECT 1 FROM risk_acks WHERE tg_id = ? AND text_version = ? LIMIT 1", (tg_id, version)
    ) as cur:
        return await cur.fetchone() is not None
