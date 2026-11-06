"""Настройки, которые меняются на ходу (таблица app_settings)."""

import aiosqlite


async def get(conn: aiosqlite.Connection, key: str) -> str | None:
    async with conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row["value"] if row else None


async def put(conn: aiosqlite.Connection, key: str, value: str, now: int) -> None:
    await conn.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now),
    )


async def delete(conn: aiosqlite.Connection, key: str) -> None:
    await conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
