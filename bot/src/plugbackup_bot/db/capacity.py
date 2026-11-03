"""Проданное место на нодах и выбор ноды для нового клиента."""

import aiosqlite

from plugbackup_bot.db.nodes import Node, get_node, list_nodes


async def sold_gb(conn: aiosqlite.Connection) -> dict[int, int]:
    """Сумма квот не удалённых клиентов по нодам (suspended и pending тоже заняты)."""
    async with conn.execute(
        "SELECT node_id, SUM(quota_gb) FROM clients WHERE state <> 'deleted' GROUP BY node_id"
    ) as cur:
        return {int(row[0]): int(row[1]) for row in await cur.fetchall()}


async def free_gb(conn: aiosqlite.Connection, node_id: int) -> int:
    node = await get_node(conn, node_id)
    if node is None:
        raise LookupError(f"нода {node_id} не найдена")
    return node.limit_gb - (await sold_gb(conn)).get(node_id, 0)


async def pick_node(conn: aiosqlite.Connection, need_gb: int) -> Node | None:
    """Включённая нода с наибольшим запасом, куда помещается need_gb. None, если мест нет."""
    sold = await sold_gb(conn)
    best: tuple[int, Node] | None = None
    for node in await list_nodes(conn):
        if not node.enabled:
            continue
        free = node.limit_gb - sold.get(node.id, 0)
        if free >= need_gb and (best is None or free > best[0]):
            best = (free, node)
    return best[1] if best else None
