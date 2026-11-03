"""Почасовой опрос занятого места: storectl usage по каждому клиенту."""

import asyncio
import logging
from dataclasses import dataclass, field

from plugbackup_bot.db import Database, notifications
from plugbackup_bot.db import usage as usage_repo
from plugbackup_bot.db.clients import Client, list_by_state
from plugbackup_bot.db.nodes import Node, list_nodes
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.infra.storectl import ErrorKind, Storectl, StorectlError, Usage
from plugbackup_bot.services.notify import AdminAlerter

log = logging.getLogger(__name__)

HOUR = 3600
_DAY = 24 * HOUR


@dataclass
class PollReport:
    hour_ts: int = 0
    # Клиенты, по которым получен свежий отсчёт.
    polled: list[int] = field(default_factory=list)
    # Сколько запросов к каждой ноде и сколько из них не удались из-за недоступности.
    attempts: dict[int, int] = field(default_factory=dict)
    unreachable: dict[int, int] = field(default_factory=dict)


class UsagePoller:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        storectl: Storectl,
        alerter: AdminAlerter,
        *,
        concurrency: int = 4,
    ) -> None:
        self._db = db
        self._clock = clock
        self._storectl = storectl
        self._alerter = alerter
        self._limit = asyncio.Semaphore(concurrency)

    async def poll(self) -> PollReport:
        """Опрашивает активных клиентов (заблокированные не меняются) и пишет отсчёты."""
        now = self._clock.now()
        report = PollReport(hour_ts=now // HOUR * HOUR)
        async with self._db.read() as conn:
            clients = await list_by_state(conn, State.ACTIVE, State.EXPIRING)
            nodes = {node.id: node for node in await list_nodes(conn)}

        results = await asyncio.gather(
            *(self._fetch(client, nodes.get(client.node_id), report) for client in clients)
        )

        async with self._db.transaction() as conn:
            for client, usage in zip(clients, results, strict=True):
                if usage is None:
                    continue
                await usage_repo.record_sample(
                    conn, client.id, report.hour_ts, usage.used_bytes, usage.quota_bytes
                )
                report.polled.append(client.id)
        return report

    async def _fetch(self, client: Client, node: Node | None, report: PollReport) -> Usage | None:
        if node is None:
            return None
        report.attempts[node.id] = report.attempts.get(node.id, 0) + 1
        async with self._limit:
            try:
                return await self._storectl.usage(node.addr, client.login)
            except StorectlError as error:
                await self._on_error(client, node, error, report)
            except Exception:
                log.exception("usage poll failed for %s", client.login)
        return None

    async def _on_error(
        self, client: Client, node: Node, error: StorectlError, report: PollReport
    ) -> None:
        if error.kind in (ErrorKind.UNREACHABLE, ErrorKind.TIMEOUT):
            report.unreachable[node.id] = report.unreachable.get(node.id, 0) + 1
            return
        log.warning("usage of %s: %s", client.login, error.kind.value)
        if error.kind is ErrorKind.NOT_FOUND:
            # Клиент есть в базе, а на ноде его нет. Сообщаем админу не чаще раза в сутки.
            day = str(self._clock.now() // _DAY)
            async with self._db.transaction() as conn:
                fresh = await notifications.claim(
                    conn, client.id, "usage_missing", day, self._clock.now()
                )
            if fresh:
                await self._alerter.alert(
                    f"Клиент {client.login} есть в базе, но не найден на ноде {node.name}."
                )
