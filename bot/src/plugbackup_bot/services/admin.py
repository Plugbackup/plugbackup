"""Действия администратора над клиентами, инвайтами, нодами и плановыми работами.

Каждое изменение пишется в журнал действий (db.audit) в той же транзакции. Права на действие
проверяет слой Telegram (tg/admin/core.py, декоратор requires); сервис получает Actor, чтобы
записать в журнал, кто это сделал.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from plugbackup_bot.db import Database, audit
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.capacity import free_gb, sold_gb
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
from plugbackup_bot.db.users import find_by_username, get_username
from plugbackup_bot.domain.logins import make_login, parse_login
from plugbackup_bot.domain.plans import GIB, PlanCatalog
from plugbackup_bot.domain.roles import Actor
from plugbackup_bot.domain.states import LIVE, State
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.billing import BillingService

log = logging.getLogger(__name__)

MAX_INVITES_AT_ONCE = 50
MAX_MAINTENANCE_TEXT = 500
MAX_NOTE_LEN = 500
MAX_EXTEND_DAYS = 365
MAX_BONUS_GB = 1000
DAY = 24 * 3600
# Сколько уведомлений клиенту показываем в его карточке.
HISTORY_LIMIT = 10


class ActionKind(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    WRONG_STATE = "wrong_state"
    INVALID = "invalid"
    SAME_PLAN = "same_plan"
    NO_CAPACITY = "no_capacity"
    TOO_MUCH_DATA = "too_much_data"


@dataclass(frozen=True, slots=True)
class ActionResult:
    kind: ActionKind
    client: Client | None = None


@dataclass(frozen=True, slots=True)
class ClientRow:
    id: int
    tg_id: int
    username: str | None
    plan_id: str
    state: State
    quota_gb: int

    @property
    def login(self) -> str:
        return make_login(self.id)


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
    username: str | None = None
    # Последние уведомления, которые бот отправил клиенту: (вид, время).
    notices: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class NodeLoad:
    node: Node
    sold_gb: int

    @property
    def free_gb(self) -> int:
        return self.node.limit_gb - self.sold_gb


class AdminService:
    def __init__(
        self, db: Database, clock: Clock, billing: BillingService, catalog: PlanCatalog
    ) -> None:
        self._db = db
        self._clock = clock
        self._billing = billing
        self._catalog = catalog

    # --- поиск, список и карточка ---

    async def find_client(self, query: str) -> Client | None:
        """По логину (u1001), Telegram id или @username. Для аккаунта берём живого клиента,
        иначе последнего."""
        query = query.strip()
        if not query:
            return None
        async with self._db.read() as conn:
            if parse_login(query) is not None:
                return await get_client_by_login(conn, query)
            tg_id = int(query) if query.isdigit() else await find_by_username(conn, query)
            if tg_id is None:
                return None
            live = await get_live_client(conn, tg_id)
            if live is not None:
                return live
            async with conn.execute(
                "SELECT id FROM clients WHERE tg_id = ? ORDER BY id DESC LIMIT 1", (tg_id,)
            ) as cur:
                row = await cur.fetchone()
            return await get_client(conn, row["id"]) if row else None

    async def client(self, client_id: int) -> Client | None:
        async with self._db.read() as conn:
            return await get_client(conn, client_id)

    async def list_clients(self, *, limit: int, offset: int) -> tuple[list[ClientRow], int]:
        """Клиенты от новых к старым, удалённые в конце. Возвращает страницу и общее число."""
        async with self._db.read() as conn:
            async with conn.execute("SELECT COUNT(*) FROM clients") as cur:
                total = int((await cur.fetchone())[0])  # type: ignore[index]
            async with conn.execute(
                "SELECT c.id, c.tg_id, c.plan_id, c.state, c.quota_gb, u.username"
                " FROM clients c LEFT JOIN users u ON u.tg_id = c.tg_id"
                " ORDER BY (c.state = 'deleted'), c.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cur:
                rows = [
                    ClientRow(
                        r["id"], r["tg_id"], r["username"], r["plan_id"], State(r["state"]),
                        r["quota_gb"],
                    )
                    for r in await cur.fetchall()
                ]  # fmt: skip
        return rows, total

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
            async with conn.execute(
                "SELECT kind, sent_at FROM notifications WHERE client_id = ?"
                " ORDER BY sent_at DESC, id DESC LIMIT ?",
                (client_id, HISTORY_LIMIT),
            ) as cur:
                notices = tuple((r["kind"], r["sent_at"]) for r in await cur.fetchall())
            username = await get_username(conn, client.tg_id)
        beta_used = user is not None and user["beta_used_at"] is not None
        return ClientCard(
            client, nodes.get(client.node_id), sample, payments, jobs, beta_used, username, notices
        )

    # --- блокировка, удаление ---

    async def suspend(self, client_id: int, actor: Actor) -> ActionResult:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            if client.state not in (State.ACTIVE, State.EXPIRING):
                return ActionResult(ActionKind.WRONG_STATE, client)
            await transition(
                conn, client_id, State.SUSPENDED, reason="admin_suspend",
                actor=actor.tag, now=now, expected_from=client.state,
            )  # fmt: skip
            # Ручная блокировка: автоматика её не снимает и не удаляет данные.
            await set_fields(conn, client_id, admin_hold=True)
            await jobs_repo.enqueue(conn, client_id, Op.SUSPEND, now)
            await audit.record(
                conn, actor, "client.block", now=now, target_type="client", target_id=client_id
            )
            fresh = await get_client(conn, client_id)
        return ActionResult(ActionKind.OK, fresh)

    async def resume(self, client_id: int, actor: Actor) -> ActionResult:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            if client.state is not State.SUSPENDED:
                return ActionResult(ActionKind.WRONG_STATE, client)
            await transition(
                conn, client_id, State.ACTIVE, reason="admin_resume",
                actor=actor.tag, now=now, expected_from=State.SUSPENDED,
            )  # fmt: skip
            await set_fields(conn, client_id, admin_hold=False)
            await jobs_repo.enqueue(conn, client_id, Op.RESUME, now)
            await audit.record(
                conn, actor, "client.unblock", now=now, target_type="client", target_id=client_id
            )
            fresh = await get_client(conn, client_id)
        return ActionResult(ActionKind.OK, fresh)

    async def delete(self, client_id: int, actor: Actor) -> ActionResult:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            if client.state is State.DELETED:
                return ActionResult(ActionKind.WRONG_STATE, client)
            await transition(
                conn, client_id, State.DELETED, reason="admin_delete",
                actor=actor.tag, now=now, expected_from=client.state,
            )  # fmt: skip
            # Клиента без ключа на ноде не было, удалять там нечего.
            if not (client.state is State.PENDING and client.ssh_key_body is None):
                await jobs_repo.enqueue(conn, client_id, Op.DELETE, now)
            await audit.record(
                conn, actor, "client.delete", now=now, target_type="client", target_id=client_id
            )
            fresh = await get_client(conn, client_id)
        if client.sub_charge_id and client.renewal_on:
            await self._billing.disable_subscription(client.tg_id, client.sub_charge_id)
        return ActionResult(ActionKind.OK, fresh)

    async def requeue_job(self, job_id: int, actor: Actor) -> bool:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            job = await jobs_repo.get_job(conn, job_id)
            if job is None or job.status.value != "failed":
                return False
            await jobs_repo.requeue(conn, job_id, now)
            await audit.record(
                conn, actor, "job.requeue", now=now, target_type="job", target_id=job_id
            )
        return True

    # --- заметка, срок, тариф, бонусные ГБ ---

    async def set_note(self, client_id: int, actor: Actor, text: str) -> ActionResult:
        text = text.strip()
        if len(text) > MAX_NOTE_LEN:
            return ActionResult(ActionKind.INVALID)
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            await set_fields(conn, client_id, admin_note=text or None)
            # В журнал идёт только факт правки: заметка внутренняя, её текст там не нужен.
            await audit.record(
                conn, actor, "client.note", now=now, target_type="client", target_id=client_id,
                cleared=not text,
            )  # fmt: skip
            fresh = await get_client(conn, client_id)
        return ActionResult(ActionKind.OK, fresh)

    async def extend(self, client_id: int, actor: Actor, days: int) -> ActionResult:
        """Продлевает период на N дней: срок оплаты (платные) или срок вопроса «Я ещё пользуюсь»
        (бета). Просроченного клиента возвращает в работу."""
        if not 1 <= days <= MAX_EXTEND_DAYS:
            return ActionResult(ActionKind.INVALID)
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            blocked = client.state is State.SUSPENDED and client.admin_hold
            if client.state not in (State.ACTIVE, State.EXPIRING, State.SUSPENDED) or blocked:
                return ActionResult(ActionKind.WRONG_STATE, client)
            if client.plan_id == self._catalog.beta.id:
                until = max(client.confirm_due_at or now, now) + days * DAY
                await set_fields(conn, client_id, confirm_due_at=until)
            else:
                until = max(client.expires_at or now, now) + days * DAY
                await set_fields(conn, client_id, expires_at=until)
            if client.state is State.EXPIRING:
                await transition(
                    conn, client_id, State.ACTIVE, reason="admin_extend", actor=actor.tag,
                    now=now, expected_from=State.EXPIRING,
                )  # fmt: skip
            elif client.state is State.SUSPENDED:
                await transition(
                    conn, client_id, State.ACTIVE, reason="admin_extend", actor=actor.tag,
                    now=now, expected_from=State.SUSPENDED,
                )  # fmt: skip
                await jobs_repo.enqueue(conn, client_id, Op.RESUME, now)
            await audit.record(
                conn, actor, "client.extend", now=now, target_type="client", target_id=client_id,
                days=days, until=until,
            )  # fmt: skip
            fresh = await get_client(conn, client_id)
        return ActionResult(ActionKind.OK, fresh)

    async def change_plan(self, client_id: int, actor: Actor, plan_id: str) -> ActionResult:
        """Переводит платного клиента на другой платный тариф без оплаты. Бонусные ГБ остаются.
        Подписка Stars клиента при этом не меняется."""
        plan = self._catalog.find(plan_id)
        if plan is None or plan.is_beta:
            return ActionResult(ActionKind.INVALID)
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            if client.plan_id == plan.id:
                return ActionResult(ActionKind.SAME_PLAN, client)
            if client.state not in LIVE or client.plan_id == self._catalog.beta.id:
                return ActionResult(ActionKind.WRONG_STATE, client)
            verdict = await self._resize(conn, client, plan.gb + client.bonus_gb, now)
            if verdict is not None:
                return ActionResult(verdict, client)
            await set_fields(conn, client_id, plan_id=plan.id)
            await audit.record(
                conn, actor, "client.plan", now=now, target_type="client", target_id=client_id,
                before=client.plan_id, after=plan.id,
            )  # fmt: skip
            fresh = await get_client(conn, client_id)
        return ActionResult(ActionKind.OK, fresh)

    async def grant_bonus(self, client_id: int, actor: Actor, gb: int) -> ActionResult:
        """Добавляет (или отнимает) бонусные ГБ. Итоговый бонус не бывает отрицательным."""
        if gb == 0 or abs(gb) > MAX_BONUS_GB:
            return ActionResult(ActionKind.INVALID)
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_client(conn, client_id)
            if client is None:
                return ActionResult(ActionKind.NOT_FOUND)
            if client.state not in LIVE:
                return ActionResult(ActionKind.WRONG_STATE, client)
            bonus = client.bonus_gb + gb
            if bonus < 0:
                return ActionResult(ActionKind.INVALID, client)
            verdict = await self._resize(conn, client, client.quota_gb + gb, now)
            if verdict is not None:
                return ActionResult(verdict, client)
            await set_fields(conn, client_id, bonus_gb=bonus)
            await audit.record(
                conn, actor, "client.bonus", now=now, target_type="client", target_id=client_id,
                delta=gb, total=bonus,
            )  # fmt: skip
            fresh = await get_client(conn, client_id)
        return ActionResult(ActionKind.OK, fresh)

    async def _resize(
        self,
        conn,
        client: Client,
        new_quota_gb: int,
        now: int,  # type: ignore[no-untyped-def]
    ) -> ActionKind | None:
        """Меняет квоту клиента и ставит resize в очередь. Возвращает причину отказа или None."""
        delta = new_quota_gb - client.quota_gb
        if delta > 0 and await free_gb(conn, client.node_id) < delta:
            return ActionKind.NO_CAPACITY
        if delta < 0:
            sample = await latest_sample(conn, client.id)
            if sample is not None and sample.used_bytes > new_quota_gb * GIB:
                return ActionKind.TOO_MUCH_DATA
        if delta != 0:
            await set_fields(conn, client.id, quota_gb=new_quota_gb)
            await jobs_repo.enqueue(conn, client.id, Op.RESIZE, now)
        return None

    async def log(
        self, actor: Actor, action: str, *, target_type: str | None = None,
        target_id: object = None, **details: object,
    ) -> None:  # fmt: skip
        """Запись в журнал для действий, которые делает не сам сервис (например, сообщение
        клиенту от имени бота)."""
        async with self._db.transaction() as conn:
            await audit.record(
                conn, actor, action, now=self._clock.now(), target_type=target_type,
                target_id=target_id, **details,
            )  # fmt: skip

    # --- инвайты ---

    async def create_invites(self, count: int, actor: Actor) -> list[str]:
        if not 1 <= count <= MAX_INVITES_AT_ONCE:
            raise ValueError(f"от 1 до {MAX_INVITES_AT_ONCE} кодов за раз")
        now = self._clock.now()
        async with self._db.transaction() as conn:
            codes = await invites_repo.create_invites(conn, count, actor.tg_id, now)
            await audit.record(
                conn, actor, "invite.create", now=now, target_type="invite", count=count
            )
        return codes

    # --- ноды и работы ---

    async def nodes_overview(self) -> list[NodeLoad]:
        async with self._db.read() as conn:
            sold = await sold_gb(conn)
            return [NodeLoad(node, sold.get(node.id, 0)) for node in await list_nodes(conn)]

    async def start_maintenance(self, target: str, text: str, actor: Actor) -> int | None:
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
            if node_id is not None:
                # Нода на обслуживании: новые клиенты на неё не выдаются.
                await conn.execute("UPDATE nodes SET maintenance = 1 WHERE id = ?", (node_id,))
            await audit.record(
                conn, actor, "maintenance.start", now=now, target_type="node",
                target_id=target, event=cur.lastrowid,
            )  # fmt: skip
        return cur.lastrowid

    async def end_maintenance(self, event_id: int, actor: Actor) -> bool:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE service_events SET ends_at = ? WHERE id = ? AND kind = 'maintenance'"
                " AND ends_at IS NULL",
                (now, event_id),
            )
            if cur.rowcount == 1:
                await conn.execute(
                    "UPDATE nodes SET maintenance = 0 WHERE id ="
                    " (SELECT node_id FROM service_events WHERE id = ?)",
                    (event_id,),
                )
                await audit.record(
                    conn, actor, "maintenance.end", now=now, target_type="event",
                    target_id=event_id,
                )  # fmt: skip
        return cur.rowcount == 1
