"""Одноразовые инвайт-коды беты. В базе лежит только хеш."""

import hashlib
import secrets
from enum import StrEnum

import aiosqlite

# Без похожих символов: нет I, L, O, 0, 1.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 10


class RedeemStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    USED = "used"


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
    conn: aiosqlite.Connection, count: int, created_by: int, now: int
) -> list[str]:
    codes = []
    for _ in range(count):
        code = new_code()
        await conn.execute(
            "INSERT INTO invites (code_hash, created_by, created_at) VALUES (?, ?, ?)",
            (code_hash(normalize(code) or ""), created_by, now),
        )
        codes.append(code)
    return codes


async def redeem(conn: aiosqlite.Connection, text: str, tg_id: int, now: int) -> RedeemStatus:
    """Погашает код. Вызывать в той же транзакции, где создаётся клиент."""
    normalized = normalize(text)
    if normalized is None:
        return RedeemStatus.INVALID
    digest = code_hash(normalized)
    async with conn.execute("SELECT used_by FROM invites WHERE code_hash = ?", (digest,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return RedeemStatus.INVALID
    if row["used_by"] is not None:
        return RedeemStatus.USED
    await conn.execute(
        "UPDATE invites SET used_by = ?, used_at = ? WHERE code_hash = ? AND used_by IS NULL",
        (tg_id, now, digest),
    )
    return RedeemStatus.OK


async def count_unused(conn: aiosqlite.Connection) -> int:
    async with conn.execute("SELECT COUNT(*) FROM invites WHERE used_by IS NULL") as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0
