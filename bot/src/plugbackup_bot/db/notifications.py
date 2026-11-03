"""Журнал отправленных уведомлений: защита от повторов и кулдаун алертов."""

import aiosqlite


async def claim(conn: aiosqlite.Connection, client_id: int, kind: str, key: str, now: int) -> bool:
    """Занимает пару (клиент, вид, ключ). True, если уведомление ещё не отправлялось."""
    cur = await conn.execute(
        "INSERT OR IGNORE INTO notifications (client_id, kind, dedupe_key, sent_at)"
        " VALUES (?, ?, ?, ?)",
        (client_id, kind, key, now),
    )
    return cur.rowcount == 1


async def last_sent(conn: aiosqlite.Connection, client_id: int, kind: str) -> int | None:
    async with conn.execute(
        "SELECT MAX(sent_at) FROM notifications WHERE client_id = ? AND kind = ?",
        (client_id, kind),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row and row[0] is not None else None


async def prune(conn: aiosqlite.Connection, older_than: int) -> int:
    cur = await conn.execute("DELETE FROM notifications WHERE sent_at < ?", (older_than,))
    return cur.rowcount
