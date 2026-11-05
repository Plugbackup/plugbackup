"""Тарифы в базе."""

from collections.abc import Sequence

import aiosqlite

from plugbackup_bot.domain.plans import Plan


def _plan(row: aiosqlite.Row) -> Plan:
    return Plan(
        id=row["id"],
        name=row["name"],
        gb=row["gb"],
        price_rub=row["price_rub"],
        stars=row["stars"],
        visible=bool(row["visible"]),
    )


async def list_plans(conn: aiosqlite.Connection) -> list[Plan]:
    async with conn.execute("SELECT * FROM plans ORDER BY sort, gb, id") as cur:
        return [_plan(row) for row in await cur.fetchall()]


async def get_plan(conn: aiosqlite.Connection, plan_id: str) -> Plan | None:
    async with conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)) as cur:
        row = await cur.fetchone()
    return _plan(row) if row else None


async def insert_missing(conn: aiosqlite.Connection, plans: Sequence[Plan], now: int) -> int:
    """Добавляет тарифы, которых ещё нет в базе. Существующие не трогает: правки админа важнее
    файла. Возвращает, сколько добавлено."""
    added = 0
    for order, plan in enumerate(plans):
        cur = await conn.execute(
            "INSERT OR IGNORE INTO plans"
            " (id, name, gb, price_rub, stars, visible, sort, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (plan.id, plan.name, plan.gb, plan.price_rub, plan.stars, plan.visible, order, now),
        )
        added += cur.rowcount
    return added


async def update_fields(
    conn: aiosqlite.Connection, plan_id: str, now: int, **fields: object
) -> None:
    allowed = {"name", "gb", "price_rub", "stars", "visible"}
    unknown = fields.keys() - allowed
    if unknown:
        raise ValueError(f"поля тарифа нельзя менять: {', '.join(sorted(unknown))}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    await conn.execute(
        f"UPDATE plans SET {assignments}, updated_at = ? WHERE id = ?",  # noqa: S608
        (*fields.values(), now, plan_id),
    )
