import aiosqlite


async def ensure_user(conn: aiosqlite.Connection, tg_id: int, now: int) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO users (tg_id, first_seen_at) VALUES (?, ?)", (tg_id, now)
    )


async def touch_user(conn: aiosqlite.Connection, tg_id: int, now: int) -> None:
    """Пользователь написал боту: запоминаем его и снимаем отметку «заблокировал бота»."""
    await ensure_user(conn, tg_id, now)
    await conn.execute(
        "UPDATE users SET bot_blocked = 0 WHERE tg_id = ? AND bot_blocked = 1", (tg_id,)
    )


async def beta_used(conn: aiosqlite.Connection, tg_id: int) -> bool:
    """Бета выдаётся один раз на аккаунт, даже если хранилище потом удалили."""
    async with conn.execute("SELECT beta_used_at FROM users WHERE tg_id = ?", (tg_id,)) as cur:
        row = await cur.fetchone()
    return row is not None and row["beta_used_at"] is not None


async def mark_beta_used(conn: aiosqlite.Connection, tg_id: int, now: int) -> None:
    await conn.execute("UPDATE users SET beta_used_at = ? WHERE tg_id = ?", (now, tg_id))


async def clear_beta_used(conn: aiosqlite.Connection, tg_id: int) -> None:
    """Бета не была выдана на деле (клиент не прислал ключ): даём возможность получить её снова."""
    await conn.execute("UPDATE users SET beta_used_at = NULL WHERE tg_id = ?", (tg_id,))
