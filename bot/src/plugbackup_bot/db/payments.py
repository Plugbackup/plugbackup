"""Платежи в Stars. Уникальный telegram_payment_charge_id делает обработку идемпотентной."""

from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True, slots=True)
class Payment:
    id: int
    tg_id: int
    client_id: int | None
    plan_id: str
    stars: int
    charge_id: str
    invoice_payload: str
    is_recurring: bool
    is_first: bool
    sub_expires_at: int | None
    status: str
    at: int

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Payment":
        return cls(
            id=row["id"],
            tg_id=row["tg_id"],
            client_id=row["client_id"],
            plan_id=row["plan_id"],
            stars=row["stars"],
            charge_id=row["telegram_payment_charge_id"],
            invoice_payload=row["invoice_payload"],
            is_recurring=bool(row["is_recurring"]),
            is_first=bool(row["is_first"]),
            sub_expires_at=row["sub_expires_at"],
            status=row["status"],
            at=row["at"],
        )


async def get_by_charge(conn: aiosqlite.Connection, charge_id: str) -> Payment | None:
    async with conn.execute(
        "SELECT * FROM payments WHERE telegram_payment_charge_id = ?", (charge_id,)
    ) as cur:
        row = await cur.fetchone()
    return Payment.from_row(row) if row else None


async def insert_payment(
    conn: aiosqlite.Connection,
    *,
    tg_id: int,
    client_id: int | None,
    plan_id: str,
    stars: int,
    charge_id: str,
    provider_charge_id: str | None,
    invoice_payload: str,
    is_recurring: bool,
    is_first: bool,
    sub_expires_at: int | None,
    now: int,
) -> int:
    cur = await conn.execute(
        "INSERT INTO payments (tg_id, client_id, plan_id, stars, telegram_payment_charge_id,"
        " provider_payment_charge_id, invoice_payload, is_recurring, is_first, sub_expires_at, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tg_id,
            client_id,
            plan_id,
            stars,
            charge_id,
            provider_charge_id,
            invoice_payload,
            is_recurring,
            is_first,
            sub_expires_at,
            now,
        ),
    )
    if cur.lastrowid is None:
        raise RuntimeError("INSERT не вернул id платежа")
    return cur.lastrowid


async def set_sub_expires(conn: aiosqlite.Connection, payment_id: int, sub_expires_at: int) -> None:
    await conn.execute(
        "UPDATE payments SET sub_expires_at = ? WHERE id = ?", (sub_expires_at, payment_id)
    )


async def revenue_stars(conn: aiosqlite.Connection, start: int, end: int) -> int:
    """Оплаченные Stars за период [start, end). Возвращённые платежи не считаются."""
    async with conn.execute(
        "SELECT COALESCE(SUM(stars), 0) FROM payments WHERE status = 'paid' AND at >= ? AND at < ?",
        (start, end),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0
