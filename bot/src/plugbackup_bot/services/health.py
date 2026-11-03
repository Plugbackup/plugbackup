"""Проверка доступности нод: алерт админу и событие для клиентов."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from plugbackup_bot.db import Database
from plugbackup_bot.db.nodes import Node, list_nodes
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.notify import AdminAlerter

log = logging.getLogger(__name__)

Probe = Callable[[Node], Awaitable[bool]]
# Админу сообщаем через 5 минут простоя, клиентам только после 15.
ADMIN_ALERT_AFTER_S = 5 * 60
CLIENT_NOTICE_AFTER_S = 15 * 60


def tcp_probe(port: int, timeout_s: float = 5) -> Probe:
    """Нода жива, если принимает TCP-соединение на порт SSH по адресу из WireGuard-сети."""

    async def probe(node: Node) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(node.addr, port), timeout_s)
        except (OSError, TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            log.debug("probe connection closed with an error", exc_info=True)
        return True

    return probe


class HealthMonitor:
    def __init__(self, db: Database, clock: Clock, alerter: AdminAlerter, probe: Probe) -> None:
        self._db = db
        self._clock = clock
        self._alerter = alerter
        self._probe = probe
        # Ноды, о которых админ уже знает. После перезапуска бота алерт может повториться.
        self._alerted: set[int] = set()

    async def check(self) -> None:
        now = self._clock.now()
        async with self._db.read() as conn:
            nodes = [node for node in await list_nodes(conn) if node.enabled]
        results = await asyncio.gather(*(self._safe_probe(node) for node in nodes))
        for node, up in zip(nodes, results, strict=True):
            if up:
                await self._on_up(node, now)
            else:
                await self._on_down(node, now)

    async def _safe_probe(self, node: Node) -> bool:
        try:
            return await self._probe(node)
        except Exception:
            log.exception("probe of %s crashed", node.name)
            return False

    async def _on_down(self, node: Node, now: int) -> None:
        down_since = node.down_since
        if down_since is None:
            down_since = now
            async with self._db.transaction() as conn:
                await conn.execute("UPDATE nodes SET down_since = ? WHERE id = ?", (now, node.id))
        down_for = now - down_since

        if down_for >= ADMIN_ALERT_AFTER_S and node.id not in self._alerted:
            self._alerted.add(node.id)
            minutes = max(1, down_for // 60)
            await self._alerter.alert(f"Нода {node.name} недоступна уже {minutes} мин.")

        if down_for >= CLIENT_NOTICE_AFTER_S:
            async with self._db.transaction() as conn:
                async with conn.execute(
                    "SELECT 1 FROM service_events WHERE node_id = ? AND kind = 'node_down'"
                    " AND ends_at IS NULL",
                    (node.id,),
                ) as cur:
                    open_event = await cur.fetchone()
                if open_event is None:
                    await conn.execute(
                        "INSERT INTO service_events (kind, node_id, text, starts_at)"
                        " VALUES ('node_down', ?, '', ?)",
                        (node.id, now),
                    )

    async def _on_up(self, node: Node, now: int) -> None:
        if node.down_since is None:
            return
        async with self._db.transaction() as conn:
            await conn.execute("UPDATE nodes SET down_since = NULL WHERE id = ?", (node.id,))
            await conn.execute(
                "UPDATE service_events SET ends_at = ? WHERE node_id = ? AND kind = 'node_down'"
                " AND ends_at IS NULL",
                (now, node.id),
            )
        if node.id in self._alerted:
            self._alerted.discard(node.id)
            minutes = max(1, (now - node.down_since) // 60)
            await self._alerter.alert(f"Нода {node.name} снова доступна, простой {minutes} мин.")
