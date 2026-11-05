"""Инвайт-коды беты. В базе лежит только хеш; по трём последним знакам админ узнаёт код в списке."""

import hashlib
import secrets
from enum import StrEnum

import aiosqlite

# Без похожих символов: нет I, L, O, 0, 1.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 10
TAIL_LEN = 3


class RedeemStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"


def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    return f"{raw[:5]}-{raw[5:]}"


def normalize(text: str) -> str | None:
    """Приводит введённый код к каноническому виду или возвращает None."""
    code = "".join(ch for ch in text.upper() if ch not in " -\t")
    if len(code) != _CODE_LEN or any(ch not in _ALPHABET for ch in code):
        return None
    return code


def code_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


async def create_invites(
    conn: aiosqlite.Connection,
    count: int,
    created_by: int,
    now: int,
    *,
    max_uses: int = 1,
    expires_at: int | None = None,
) -> list[str]:
    codes = []
    for _ in range(count):
        code = new_code()
        normalized = normalize(code) or ""
        await conn.execute(
            "INSERT INTO invites"
            " (code_hash, created_by, created_at, max_uses, expires_at, code_tail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (code_hash(normalized), created_by, now, max_uses, expires_at, normalized[-TAIL_LEN:]),
        )
        codes.append(code)
    return codes


async def redeem(conn: aiosqlite.Connection, text: str, tg_id: int, now: int) -> RedeemStatus:
    """Погашает код. Вызывать в той же транзакции, где создаётся клиент."""
    normalized = normalize(text)
    if normalized is None:
        return RedeemStatus.INVALID
    digest = code_hash(normalized)
    async with conn.execute(
        "SELECT uses, max_uses, expires_at, revoked_at FROM invites WHERE code_hash = ?", (digest,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return RedeemStatus.INVALID
    if row["revoked_at"] is not None:
        return RedeemStatus.REVOKED
    if row["expires_at"] is not None and row["expires_at"] <= now:
        return RedeemStatus.EXPIRED
    if row["uses"] >= row["max_uses"]:
        return RedeemStatus.USED
    cur = await conn.execute(
        "UPDATE invites SET uses = uses + 1, used_by = COALESCE(used_by, ?),"
        " used_at = COALESCE(used_at, ?)"
        " WHERE code_hash = ? AND uses < max_uses AND revoked_at IS NULL"
        " AND (expires_at IS NULL OR expires_at > ?)",
        (tg_id, now, digest, now),
    )
    if cur.rowcount != 1:
        return RedeemStatus.USED
    await conn.execute(
        "INSERT OR REPLACE INTO invite_uses (code_hash, tg_id, used_at) VALUES (?, ?, ?)",
        (digest, tg_id, now),
    )
    return RedeemStatus.OK


async def count_unused(conn: aiosqlite.Connection, now: int = 0) -> int:
    """Коды, которыми ещё можно воспользоваться: не отозваны, не исчерпаны, срок не вышел."""
    async with conn.execute(
        "SELECT COUNT(*) FROM invites WHERE revoked_at IS NULL AND uses < max_uses"
        " AND (expires_at IS NULL OR expires_at > ?)",
        (now,),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0
