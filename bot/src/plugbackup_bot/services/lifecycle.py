"""Жизненный цикл клиентов: переходы по времени и обязательные уведомления."""

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from aiogram.types import InlineKeyboardMarkup

from plugbackup_bot.db import Database, notifications
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.clients import (
    Client,
    get_client,
    get_live_client,
    list_by_state,
    set_fields,
    transition,
)
from plugbackup_bot.db.jobs import Op
from plugbackup_bot.db.users import clear_beta_used
from plugbackup_bot.domain.lifecycle import Notice, Step, evaluate
from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.domain.policy import BETA_CONFIRM_PERIOD_S
from plugbackup_bot.domain.states import InvalidTransition, State
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.billing import BillingService
from plugbackup_bot.services.notify import Messenger

log = logging.getLogger(__name__)

_ACTIVE_STATES = (State.PENDING, State.ACTIVE, State.EXPIRING, State.SUSPENDED)


class NoticeRenderer(Protocol):
    def render(self, notice: Notice, client: Client) -> tuple[str, InlineKeyboardMarkup | None]:
        """Текст и кнопки уведомления."""
        ...


@dataclass
class TickReport:
    transitions: dict[str, int] = field(default_factory=dict)
    notices: int = 0
    errors: int = 0


class AliveKind(StrEnum):
    OK = "ok"
    NO_CLIENT = "no_client"
    NOT_BETA = "not_beta"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class AliveResult:
    kind: AliveKind
    client: Client | None = None
    resumed: bool = False


class LifecycleService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        catalog: PlanCatalog,
        billing: BillingService,
        messenger: Messenger,
        renderer: NoticeRenderer,
    ) -> None:
        self._db = db
        self._clock = clock
        self._catalog = catalog
        self._billing = billing
        self._messenger = messenger
        self._renderer = renderer

    async def tick(self) -> TickReport:
        """Один проход по всем клиентам. Сбой на одном клиенте не мешает остальным."""
        now = self._clock.now()
        report = TickReport()
        async with self._db.read() as conn:
            clients = await list_by_state(conn, *_ACTIVE_STATES)
        for client in clients:
            step = evaluate(client, now, beta_plan_id=self._catalog.beta.id)
            if step is None:
                continue
            try:
                await self._apply(client, step, now, report)
            except Exception:
                report.errors += 1
                log.exception("lifecycle step failed for client %s", client.id)
        return report

    async def _apply(self, client: Client, step: Step, now: int, report: TickReport) -> None:
        to_send: list[Notice] = []
        async with self._db.transaction() as conn:
            if step.to is not None:
                try:
                    await transition(
                        conn,
                        client.id,
                        step.to,
                        reason=step.reason,
                        actor="system",
                        now=now,
                        expected_from=client.state,
                    )
                except InvalidTransition:
                    # Пока считали, клиент успел сменить состояние (например, пришло продление).
                    log.info("lifecycle step for client %s is stale", client.id)
                    return
                if step.job is not None:
                    await jobs_repo.enqueue(conn, client.id, Op(step.job), now)
                if step.reset_beta:
                    await clear_beta_used(conn, client.tg_id)
            for notice in step.notices:
                if await notifications.claim(conn, client.id, notice.kind.value, notice.key, now):
                    to_send.append(notice)
            fresh = await get_client(conn, client.id)
        if fresh is None:
            return

        if step.to is not None:
            report.transitions[step.to.value] = report.transitions.get(step.to.value, 0) + 1
        for notice in to_send:
            text, markup = self._renderer.render(notice, fresh)
            await self._messenger.send(client.tg_id, text, reply_markup=markup)
            report.notices += 1
        if step.to is State.DELETED and client.sub_charge_id and client.renewal_on:
            # Подписка не должна продолжать списывать деньги за удалённое хранилище.
            await self._billing.disable_subscription(client.tg_id, client.sub_charge_id)

    async def confirm_alive(self, tg_id: int, client_id: int) -> AliveResult:
        """Кнопка «Я ещё пользуюсь»: продлевает бета-цикл, при необходимости снимает блокировку."""
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_live_client(conn, tg_id)
            if client is None or client.id != client_id or client.state is State.PENDING:
                return AliveResult(AliveKind.NO_CLIENT)
            if client.plan_id != self._catalog.beta.id:
                return AliveResult(AliveKind.NOT_BETA, client)
            if client.admin_hold:
                return AliveResult(AliveKind.BLOCKED, client)
            await set_fields(conn, client.id, confirm_due_at=now + BETA_CONFIRM_PERIOD_S)
            resumed = False
            if client.state is State.EXPIRING:
                await transition(
                    conn, client.id, State.ACTIVE, reason="alive", actor="client", now=now,
                    expected_from=State.EXPIRING,
                )  # fmt: skip
            elif client.state is State.SUSPENDED:
                await transition(
                    conn, client.id, State.ACTIVE, reason="alive", actor="client", now=now,
                    expected_from=State.SUSPENDED,
                )  # fmt: skip
                await jobs_repo.enqueue(conn, client.id, Op.RESUME, now)
                resumed = True
            fresh = await get_client(conn, client.id)
        return AliveResult(AliveKind.OK, fresh, resumed)
