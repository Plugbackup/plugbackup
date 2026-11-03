"""Настройки алертов клиента и данные для алертов о входах."""

from dataclasses import dataclass

import aiosqlite

from plugbackup_bot.db.users import ensure_user
from plugbackup_bot.domain.alerts import (
    ALERTS,
    BY_KEY,
    HOUR,
    IP_HASH_LIMIT,
    IP_HASH_RETENTION_S,
)

_LOGIN_RETENTION_S = 30 * 24 * HOUR


@dataclass(frozen=True, slots=True)
class Setting:
    key: str
    enabled: bool
    threshold: int | None
    consent_at: int | None


@dataclass(frozen=True, slots=True)
class Prefs:
    tz: str
    quiet_from: int
    quiet_to: int
    cooldown_hours: int


# --- настройки ---


async def get_settings(conn: aiosqlite.Connection, tg_id: int) -> dict[str, Setting]:
    """Настройки клиента: сохранённые поверх значений по умолчанию."""
    result = {
        alert.key: Setting(alert.key, alert.default_enabled, alert.default_threshold, None)
        for alert in ALERTS
    }
    async with conn.execute(
        "SELECT alert_key, enabled, threshold, consent_at FROM alert_settings WHERE tg_id = ?",
        (tg_id,),
    ) as cur:
        for row in await cur.fetchall():
            key = row["alert_key"]
            if key in result:
                threshold = (
                    row["threshold"] if row["threshold"] is not None else (result[key].threshold)
                )
                result[key] = Setting(key, bool(row["enabled"]), threshold, row["consent_at"])
    return result


async def _save(conn: aiosqlite.Connection, tg_id: int, setting: Setting) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO alert_settings (tg_id, alert_key, enabled, threshold, consent_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (tg_id, setting.key, setting.enabled, setting.threshold, setting.consent_at),
    )


async def set_enabled(
    conn: aiosqlite.Connection,
    tg_id: int,
    key: str,
    enabled: bool,
    now: int,
    *,
    consent: bool = False,
) -> None:
    """Включает или выключает алерт. Алерт нового IP включается только с явным согласием."""
    if key not in BY_KEY:
        raise ValueError(f"неизвестный алерт: {key}")
    if key == "new_ip" and enabled and not consent:
        raise ValueError("алерт нового IP требует явного согласия")
    await ensure_user(conn, tg_id, now)
    current = (await get_settings(conn, tg_id))[key]
    consent_at = (now if consent else current.consent_at) if enabled else None
    await _save(conn, tg_id, Setting(key, enabled, current.threshold, consent_at))
    if key == "new_ip" and not enabled:
        # Отказ от алерта сразу стирает накопленные хеши.
        await conn.execute(
            "DELETE FROM ip_hashes WHERE client_id IN (SELECT id FROM clients WHERE tg_id = ?)",
            (tg_id,),
        )


async def set_threshold(
    conn: aiosqlite.Connection, tg_id: int, key: str, value: int, now: int
) -> None:
    alert = BY_KEY.get(key)
    if alert is None or value not in alert.options:
        raise ValueError("недопустимый порог")
    await ensure_user(conn, tg_id, now)
    current = (await get_settings(conn, tg_id))[key]
    await _save(conn, tg_id, Setting(key, current.enabled, value, current.consent_at))


# --- общие настройки ---


async def get_prefs(conn: aiosqlite.Connection, tg_id: int, default_tz: str) -> Prefs:
    async with conn.execute("SELECT * FROM alert_prefs WHERE tg_id = ?", (tg_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return Prefs(default_tz, 23, 8, 24)
    return Prefs(row["tz"] or default_tz, row["quiet_from"], row["quiet_to"], row["cooldown_hours"])


async def set_prefs(
    conn: aiosqlite.Connection,
    tg_id: int,
    now: int,
    *,
    default_tz: str,
    tz: str | None = None,
    quiet: tuple[int, int] | None = None,
    cooldown_hours: int | None = None,
) -> None:
    await ensure_user(conn, tg_id, now)
    current = await get_prefs(conn, tg_id, default_tz)
    quiet_from, quiet_to = quiet if quiet is not None else (current.quiet_from, current.quiet_to)
    await conn.execute(
        "INSERT OR REPLACE INTO alert_prefs (tg_id, tz, quiet_from, quiet_to, cooldown_hours)"
        " VALUES (?, ?, ?, ?, ?)",
        (tg_id, tz or current.tz, quiet_from, quiet_to, cooldown_hours or current.cooldown_hours),
    )


# --- входы ---


async def replace_fail_counts(
    conn: aiosqlite.Connection, client_id: int, counts: dict[int, int]
) -> None:
    """Записывает неудачные входы по часам. Повторная запись тех же часов безопасна."""
    for hour_ts, fails in counts.items():
        await conn.execute(
            "INSERT OR REPLACE INTO login_fail_hourly (client_id, hour_ts, fails) VALUES (?, ?, ?)",
            (client_id, hour_ts, fails),
        )


async def fail_counts(conn: aiosqlite.Connection, client_id: int, since: int) -> dict[int, int]:
    async with conn.execute(
        "SELECT hour_ts, fails FROM login_fail_hourly WHERE client_id = ? AND hour_ts >= ?",
        (client_id, since),
    ) as cur:
        return {row["hour_ts"]: row["fails"] for row in await cur.fetchall()}


async def known_hash_count(conn: aiosqlite.Connection, client_id: int) -> int:
    async with conn.execute(
        "SELECT COUNT(*) FROM ip_hashes WHERE client_id = ?", (client_id,)
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


async def record_ip_hashes(
    conn: aiosqlite.Connection,
    client_id: int,
    seen: dict[str, int],
    now: int,
    *,
    baseline: bool,
) -> None:
    """Запоминает хеши {хеш: время последнего входа}.

    baseline=True: первая выгрузка после включения алерта, всё считаем уже известным.
    Иначе новые хеши остаются с notified_at = NULL и попадают в алерт.
    """
    for ip_hash, last_seen in seen.items():
        await conn.execute(
            "INSERT INTO ip_hashes (client_id, ip_hash, first_seen, last_seen, notified_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (client_id, ip_hash) DO UPDATE SET"
            " last_seen = MAX(last_seen, excluded.last_seen)",
            (client_id, ip_hash, last_seen, last_seen, now if baseline else None),
        )
    await _trim_hashes(conn, client_id, now)


async def _trim_hashes(conn: aiosqlite.Connection, client_id: int, now: int) -> None:
    """Не больше 20 хешей и не старше 30 дней. Ещё не показанные клиенту хеши не трогаем."""
    await conn.execute(
        "DELETE FROM ip_hashes WHERE client_id = ? AND last_seen < ? AND notified_at IS NOT NULL",
        (client_id, now - IP_HASH_RETENTION_S),
    )
    await conn.execute(
        "DELETE FROM ip_hashes WHERE client_id = ? AND notified_at IS NOT NULL AND ip_hash NOT IN"
        " (SELECT ip_hash FROM ip_hashes WHERE client_id = ? ORDER BY last_seen DESC LIMIT ?)",
        (client_id, client_id, IP_HASH_LIMIT),
    )


async def new_ip_hashes(conn: aiosqlite.Connection, client_id: int) -> list[str]:
    async with conn.execute(
        "SELECT ip_hash FROM ip_hashes WHERE client_id = ? AND notified_at IS NULL"
        " ORDER BY first_seen",
        (client_id,),
    ) as cur:
        return [row[0] for row in await cur.fetchall()]


async def mark_ip_notified(
    conn: aiosqlite.Connection, client_id: int, hashes: list[str], now: int
) -> None:
    for ip_hash in hashes:
        await conn.execute(
            "UPDATE ip_hashes SET notified_at = ? WHERE client_id = ? AND ip_hash = ?",
            (now, client_id, ip_hash),
        )


async def prune(conn: aiosqlite.Connection, now: int) -> int:
    """Чистка по срокам хранения: почасовые счётчики входов и старые хеши IP."""
    cur = await conn.execute(
        "DELETE FROM login_fail_hourly WHERE hour_ts < ?", (now - _LOGIN_RETENTION_S,)
    )
    removed = cur.rowcount
    cur = await conn.execute(
        "DELETE FROM ip_hashes WHERE last_seen < ?", (now - IP_HASH_RETENTION_S,)
    )
    return removed + cur.rowcount
