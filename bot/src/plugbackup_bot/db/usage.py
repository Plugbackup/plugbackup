"""История занятого места по часам."""

from dataclasses import dataclass

import aiosqlite

RETENTION_S = 30 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Sample:
    client_id: int
    hour_ts: int
    used_bytes: int
    quota_bytes: int


async def record_sample(
    conn: aiosqlite.Connection, client_id: int, hour_ts: int, used_bytes: int, quota_bytes: int
) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO usage_samples (client_id, hour_ts, used_bytes, quota_bytes)"
        " VALUES (?, ?, ?, ?)",
        (client_id, hour_ts, used_bytes, quota_bytes),
    )


async def latest_sample(conn: aiosqlite.Connection, client_id: int) -> Sample | None:
    async with conn.execute(
        "SELECT * FROM usage_samples WHERE client_id = ? ORDER BY hour_ts DESC LIMIT 1",
        (client_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return Sample(row["client_id"], row["hour_ts"], row["used_bytes"], row["quota_bytes"])


async def prune(conn: aiosqlite.Connection, now: int) -> int:
    """Удаляет отсчёты старше 30 дней, возвращает их число."""
    cur = await conn.execute("DELETE FROM usage_samples WHERE hour_ts < ?", (now - RETENTION_S,))
    return cur.rowcount


async def history(conn: aiosqlite.Connection, client_id: int, since: int) -> list[Sample]:
    """Отсчёты клиента начиная с `since`, от старых к новым."""
    async with conn.execute(
        "SELECT * FROM usage_samples WHERE client_id = ? AND hour_ts >= ? ORDER BY hour_ts",
        (client_id, since),
    ) as cur:
        return [
            Sample(row["client_id"], row["hour_ts"], row["used_bytes"], row["quota_bytes"])
            for row in await cur.fetchall()
        ]
