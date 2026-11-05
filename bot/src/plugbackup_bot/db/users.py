import aiosqlite


async def ensure_user(conn: aiosqlite.Connection, tg_id: int, now: int) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO users (tg_id, first_seen_at) VALUES (?, ?)", (tg_id, now)
    )


async def touch_user(
    conn: aiosqlite.Connection, tg_id: int, now: int, username: str | None = None
) -> None:
    """Пользователь написал боту: запоминаем его (и @username для поиска) и снимаем отметку
    «заблокировал бота»."""
    await ensure_user(conn, tg_id, now)
    await conn.execute(
        "UPDATE users SET bot_blocked = 0, last_seen_at = ?, username = ? WHERE tg_id = ?",
        (now, username.lower() if username else None, tg_id),
    )


async def find_by_username(conn: aiosqlite.Connection, username: str) -> int | None:
    """Telegram id по @username (без @, регистр не важен)."""
    async with conn.execute(
        "SELECT tg_id FROM users WHERE username = ?", (username.lstrip("@").lower(),)
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def get_username(conn: aiosqlite.Connection, tg_id: int) -> str | None:
    async with conn.execute("SELECT username FROM users WHERE tg_id = ?", (tg_id,)) as cur:
        row = await cur.fetchone()
    return row["username"] if row else None


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
