"""Действия администратора: карточка клиента, блокировка, удаление, инвайты, ноды."""

import logging
from dataclasses import dataclass
from enum import StrEnum

from plugbackup_bot.db import Database
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.capacity import sold_gb
from plugbackup_bot.db.clients import (
    Client,
    get_client,
    get_client_by_login,
    get_live_client,
    set_fields,
    transition,
)
from plugbackup_bot.db.jobs import Job, Op
from plugbackup_bot.db.nodes import Node, list_nodes
from plugbackup_bot.db.usage import Sample, latest_sample
from plugbackup_bot.domain.states import State, admin_actor
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.billing import BillingService

log = logging.getLogger(__name__)

MAX_INVITES_AT_ONCE = 50
MAX_MAINTENANCE_TEXT = 500


class ActionKind(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    WRONG_STATE = "wrong_state"


@dataclass(frozen=True, slots=True)
class ActionResult:
    kind: ActionKind
    client: Client | None = None


@dataclass(frozen=True, slots=True)
class PaymentRow:
    stars: int
    at: int
    plan_id: str
    recurring: bool
    status: str


@dataclass(frozen=True, slots=True)
class ClientCard:
    client: Client
    node: Node | None
    sample: Sample | None
    payments: list[PaymentRow]
    open_jobs: list[Job]
    beta_used: bool


@dataclass(frozen=True, slots=True)
class NodeLoad:
    node: Node
    sold_gb: int

    @property
    def free_gb(self) -> int:
        return self.node.limit_gb - self.sold_gb


class AdminService:
    def __init__(self, db: Database, clock: Clock, billing: BillingService) -> None:
        self._db = db
        self._clock = clock
        self._billing = billing

    # --- поиск и карточка ---

    async def find_client(self, query: str) -> Client | None:
        """По логину (u1001) или по Telegram id. Для id берём живого клиента, иначе последнего."""
        query = query.strip()
        async with self._db.read() as conn:
            if query.isdigit():
                tg_id = int(query)
                live = await get_live_client(conn, tg_id)
                if live is not None:
                    return live
                async with conn.execute(
                    "SELECT id FROM clients WHERE tg_id = ? ORDER BY id DESC LIMIT 1", (tg_id,)
                ) as cur:
                    row = await cur.fetchone()
                return await get_client(conn, row["id"]) if row else None
            return await get_client_by_login(conn, query)

    async def card(self, client_id: int) -> ClientCard | None:
        async with self._db.read() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return None
            nodes = {node.id: node for node in await list_nodes(conn)}
            sample = await latest_sample(conn, client_id)
            async with conn.execute(
                "SELECT stars, at, plan_id, is_recurring, status FROM payments"
                " WHERE tg_id = ? ORDER BY at DESC, id DESC LIMIT 5",
                (client.tg_id,),
            ) as cur:
                payments = [
                    PaymentRow(
                        r["stars"], r["at"], r["plan_id"], bool(r["is_recurring"]), r["status"]
                    )
                    for r in await cur.fetchall()
                ]
            async with conn.execute(
                "SELECT * FROM provision_jobs"
                " WHERE client_id = ? AND status IN ('pending', 'failed') ORDER BY id",
                (client_id,),
            ) as cur:
                jobs = [Job.from_row(row) for row in await cur.fetchall()]
            async with conn.execute(
                "SELECT beta_used_at FROM users WHERE tg_id = ?", (client.tg_id,)
            ) as cur:
                user = await cur.fetchone()
        beta_used = user is not None and user["beta_used_at"] is not None
        return ClientCard(client, nodes.get(client.node_id), sample, payments, jobs, beta_used)

    # --- блокировка, возврат, удаление ---

    async def suspend(self, client_id: int, admin_id: int) -> ActionResult:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            if client.state not in (State.ACTIVE, State.EXPIRING):
                return ActionResult(ActionKind.WRONG_STATE, client)
            await transition(
                conn, client_id, State.SUSPENDED, reason="admin_suspend",
                actor=admin_actor(admin_id), now=now, expected_from=client.state,
            )  # fmt: skip
            # Ручная блокировка: автоматика её не снимает и не удаляет данные.
            await set_fields(conn, client_id, admin_hold=True)
            await jobs_repo.enqueue(conn, client_id, Op.SUSPEND, now)
            fresh = await get_client(conn, client_id)
        return ActionResult(ActionKind.OK, fresh)

    async def resume(self, client_id: int, admin_id: int) -> ActionResult:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            if client.state is not State.SUSPENDED:
                return ActionResult(ActionKind.WRONG_STATE, client)
            await transition(
                conn, client_id, State.ACTIVE, reason="admin_resume",
                actor=admin_actor(admin_id), now=now, expected_from=State.SUSPENDED,
            )  # fmt: skip
            await set_fields(conn, client_id, admin_hold=False)
            await jobs_repo.enqueue(conn, client_id, Op.RESUME, now)
            fresh = await get_client(conn, client_id)
        return ActionResult(ActionKind.OK, fresh)

    async def delete(self, client_id: int, admin_id: int) -> ActionResult:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            if client.state is State.DELETED:
                return ActionResult(ActionKind.WRONG_STATE, client)
            await transition(
                conn, client_id, State.DELETED, reason="admin_delete",
                actor=admin_actor(admin_id), now=now, expected_from=client.state,
            )  # fmt: skip
            # Клиента без ключа на ноде не было, удалять там нечего.
            if not (client.state is State.PENDING and client.ssh_key_body is None):
                await jobs_repo.enqueue(conn, client_id, Op.DELETE, now)
            fresh = await get_client(conn, client_id)
        if client.sub_charge_id and client.renewal_on:
            await self._billing.disable_subscription(client.tg_id, client.sub_charge_id)
        return ActionResult(ActionKind.OK, fresh)

    async def requeue_job(self, job_id: int) -> bool:
        async with self._db.transaction() as conn:
            job = await jobs_repo.get_job(conn, job_id)
            if job is None or job.status.value != "failed":
                return False
            await jobs_repo.requeue(conn, job_id, self._clock.now())
        return True

    # --- инвайты ---

    async def create_invites(self, count: int, admin_id: int) -> list[str]:
        if not 1 <= count <= MAX_INVITES_AT_ONCE:
            raise ValueError(f"от 1 до {MAX_INVITES_AT_ONCE} кодов за раз")
        async with self._db.transaction() as conn:
            return await invites_repo.create_invites(conn, count, admin_id, self._clock.now())

    # --- ноды и работы ---

    async def nodes_overview(self) -> list[NodeLoad]:
        async with self._db.read() as conn:
            sold = await sold_gb(conn)
            return [NodeLoad(node, sold.get(node.id, 0)) for node in await list_nodes(conn)]

    async def start_maintenance(self, target: str, text: str) -> int | None:
        """Событие «плановые работы». target: имя ноды или all. None, если ноды нет."""
        text = text.strip()[:MAX_MAINTENANCE_TEXT]
        if not text:
            raise ValueError("пустой текст")
        async with self._db.transaction() as conn:
            node_id: int | None = None
            if target != "all":
                nodes = {node.name: node.id for node in await list_nodes(conn)}
                if target not in nodes:
                    return None
                node_id = nodes[target]
            now = self._clock.now()
            cur = await conn.execute(
                "INSERT INTO service_events (kind, node_id, text, starts_at)"
                " VALUES ('maintenance', ?, ?, ?)",
                (node_id, text, now),
            )
        return cur.lastrowid

    async def end_maintenance(self, event_id: int) -> bool:
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE service_events SET ends_at = ? WHERE id = ? AND kind = 'maintenance'"
                " AND ends_at IS NULL",
                (self._clock.now(), event_id),
            )
        return cur.rowcount == 1
