"""Ноды в админке: ёмкость, режим обслуживания, добавление новой ноды.

Ноды из .env синхронизируются при старте. Нода, добавленная из бота, получает source = 'bot' и
стартует выключенной: сначала проверка связи, потом включение, чтобы на ноду без ключа хоста
не выдавались клиенты.
"""

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from plugbackup_bot.config import NodeConfig
from plugbackup_bot.db import Database, audit
from plugbackup_bot.db.capacity import sold_gb
from plugbackup_bot.db.nodes import Node, get_node
from plugbackup_bot.domain.roles import Actor
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.admin import AdminService
from plugbackup_bot.services.health import Probe

MAX_CAPACITY_GB = 10_000_000


class NodeResult(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    EXISTS = "exists"
    UNREACHABLE = "unreachable"
    WRONG_STATE = "wrong_state"


@dataclass(frozen=True, slots=True)
class NodeDetails:
    node: Node
    sold_gb: int
    clients: int  # живые клиенты на ноде

    @property
    def free_gb(self) -> int:
        return self.node.limit_gb - self.sold_gb


class NodeService:
    def __init__(self, db: Database, clock: Clock, admin: AdminService, probe: Probe) -> None:
        self._db = db
        self._clock = clock
        self._admin = admin
        self._probe = probe

    async def details(self, node_id: int) -> NodeDetails | None:
        async with self._db.read() as conn:
            node = await get_node(conn, node_id)
            if node is None:
                return None
            sold = (await sold_gb(conn)).get(node_id, 0)
            async with conn.execute(
                "SELECT COUNT(*) FROM clients WHERE node_id = ? AND state <> 'deleted'", (node_id,)
            ) as cur:
                clients = int((await cur.fetchone())[0])  # type: ignore[index]
        return NodeDetails(node, sold, clients)

    async def check(self, node_id: int) -> bool | None:
        """Отвечает ли нода по SSH-порту. None, если такой ноды нет."""
        async with self._db.read() as conn:
            node = await get_node(conn, node_id)
        return await self._probe(node) if node is not None else None

    async def add(self, actor: Actor, config: NodeConfig) -> tuple[NodeResult, int | None]:
        """Добавляет ноду выключенной. Возвращает результат и id ноды."""
        now = self._clock.now()
        try:
            async with self._db.transaction() as conn:
                cur = await conn.execute(
                    "INSERT INTO nodes (name, addr, public_host, capacity_gb, oversell, enabled,"
                    " source) VALUES (?, ?, ?, ?, ?, 0, 'bot')",
                    (config.name, config.addr, config.public_host, config.capacity_gb,
                     config.oversell),
                )  # fmt: skip
                await audit.record(
                    conn, actor, "node.add", now=now, target_type="node", target_id=config.name,
                    capacity_gb=config.capacity_gb,
                )  # fmt: skip
        except sqlite3.IntegrityError:
            return NodeResult.EXISTS, None
        return NodeResult.OK, cur.lastrowid

    async def set_capacity(self, node_id: int, actor: Actor, capacity_gb: int) -> NodeResult:
        if not 0 <= capacity_gb <= MAX_CAPACITY_GB:
            return NodeResult.INVALID
        return await self._set(node_id, actor, "node.capacity", "capacity_gb", capacity_gb)

    async def set_oversell(self, node_id: int, actor: Actor, oversell: float) -> NodeResult:
        if not 0 < oversell <= 10:
            return NodeResult.INVALID
        return await self._set(node_id, actor, "node.oversell", "oversell", oversell)

    async def _set(
        self, node_id: int, actor: Actor, action: str, column: str, value: float
    ) -> NodeResult:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            node = await get_node(conn, node_id)
            if node is None:
                return NodeResult.NOT_FOUND
            before = getattr(node, column)
            await conn.execute(
                f"UPDATE nodes SET {column} = ? WHERE id = ?",  # noqa: S608
                (value, node_id),
            )
            await audit.record(
                conn, actor, action, now=now, target_type="node", target_id=node.name,
                before=before, after=value,
            )  # fmt: skip
        return NodeResult.OK

    async def set_enabled(self, node_id: int, actor: Actor, enabled: bool) -> NodeResult:
        """Включение требует, чтобы нода сейчас отвечала по SSH-порту."""
        async with self._db.read() as conn:
            node = await get_node(conn, node_id)
        if node is None:
            return NodeResult.NOT_FOUND
        if enabled and not await self._probe(node):
            return NodeResult.UNREACHABLE
        now = self._clock.now()
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE nodes SET enabled = ? WHERE id = ?", (enabled, node_id))
            await audit.record(
                conn, actor, "node.enable" if enabled else "node.disable", now=now,
                target_type="node", target_id=node.name,
            )  # fmt: skip
        return NodeResult.OK

    async def start_maintenance(self, node_id: int, actor: Actor, text: str) -> NodeResult:
        """Обслуживание: новые клиенты на ноду не выдаются, её клиентам уходит уведомление."""
        async with self._db.read() as conn:
            node = await get_node(conn, node_id)
        if node is None:
            return NodeResult.NOT_FOUND
        if node.maintenance:
            return NodeResult.WRONG_STATE
        event = await self._admin.start_maintenance(node.name, text, actor)
        return NodeResult.OK if event is not None else NodeResult.NOT_FOUND

    async def end_maintenance(self, node_id: int, actor: Actor) -> NodeResult:
        async with self._db.read() as conn:
            node = await get_node(conn, node_id)
            if node is None:
                return NodeResult.NOT_FOUND
            async with conn.execute(
                "SELECT id FROM service_events WHERE kind = 'maintenance' AND node_id = ?"
                " AND ends_at IS NULL ORDER BY id DESC LIMIT 1",
                (node_id,),
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            if not node.maintenance:
                return NodeResult.WRONG_STATE
            # Флаг остался без события: просто снимаем его.
            now = self._clock.now()
            async with self._db.transaction() as conn:
                await conn.execute("UPDATE nodes SET maintenance = 0 WHERE id = ?", (node_id,))
                await audit.record(
                    conn, actor, "maintenance.end", now=now, target_type="node", target_id=node.name
                )
            return NodeResult.OK
        ended = await self._admin.end_maintenance(int(row["id"]), actor)
        return NodeResult.OK if ended else NodeResult.WRONG_STATE
