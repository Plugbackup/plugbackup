"""Ноды хранения. Список задаётся в .env и синхронизируется в таблицу при старте."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

import aiosqlite

from plugbackup_bot.config import NodeConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Node:
    id: int
    name: str
    addr: str
    public_host: str
    capacity_gb: int
    oversell: float
    enabled: bool
    down_since: int | None
    # Обслуживание: новые клиенты на ноду не выдаются, действующие работают.
    maintenance: bool = False
    # env: запись из .env, bot: добавлена из бота (при старте не отключается).
    source: str = "env"

    @property
    def limit_gb(self) -> int:
        """Сколько ГБ квот можно продать на ноде с учётом перепродажи."""
        return int(self.capacity_gb * self.oversell)

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Node":
        return cls(
            id=row["id"],
            name=row["name"],
            addr=row["addr"],
            public_host=row["public_host"],
            capacity_gb=row["capacity_gb"],
            oversell=row["oversell"],
            enabled=bool(row["enabled"]),
            down_since=row["down_since"],
            maintenance=bool(row["maintenance"]),
            source=row["source"],
        )


async def get_node(conn: aiosqlite.Connection, node_id: int) -> Node | None:
    async with conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)) as cur:
        row = await cur.fetchone()
    return Node.from_row(row) if row else None


async def list_nodes(conn: aiosqlite.Connection) -> list[Node]:
    async with conn.execute("SELECT * FROM nodes ORDER BY id") as cur:
        return [Node.from_row(row) for row in await cur.fetchall()]


async def sync_nodes(conn: aiosqlite.Connection, configs: Sequence[NodeConfig]) -> None:
    """Приводит таблицу в соответствие с .env. Убранные из конфига ноды отключаются."""
    for node in configs:
        await conn.execute(
            "INSERT INTO nodes (name, addr, public_host, capacity_gb, oversell, enabled)"
            " VALUES (?, ?, ?, ?, ?, 1)"
            " ON CONFLICT (name) DO UPDATE SET addr = excluded.addr,"
            " public_host = excluded.public_host, capacity_gb = excluded.capacity_gb,"
            " oversell = excluded.oversell, enabled = 1, source = 'env'",
            (node.name, node.addr, node.public_host, node.capacity_gb, node.oversell),
        )
    names = [node.name for node in configs]
    marks = ", ".join("?" for _ in names)
    sql = (
        "UPDATE nodes SET enabled = 0 WHERE enabled = 1 AND source = 'env'"  # noqa: S608
        f" AND name NOT IN ({marks})"
    )
    cur = await conn.execute(sql, names)
    if cur.rowcount:
        log.warning("nodes missing from config were disabled: %s", cur.rowcount)
