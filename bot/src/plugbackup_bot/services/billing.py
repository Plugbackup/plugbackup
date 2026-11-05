"""Платежи Telegram Stars: подписки, продления, смена тарифа.

Сервис не знает про aiogram: handlers превращают апдейты в `PaymentEvent`, а результат
(`PaymentOutcome`) показывают клиенту. Каждый платёж обрабатывается в одной транзакции,
а повторная доставка того же платежа ничего не меняет.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import aiosqlite

from plugbackup_bot.db import Database
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.capacity import free_gb, pick_node
from plugbackup_bot.db.clients import (
    Client,
    create_client,
    get_client,
    get_live_client,
    set_fields,
    transition,
)
from plugbackup_bot.db.jobs import Op
from plugbackup_bot.db.payments import (
    Payment,
    get_by_charge,
    insert_payment,
    set_sub_expires,
)
from plugbackup_bot.db.risk import has_acked
from plugbackup_bot.db.usage import latest_sample
from plugbackup_bot.db.users import ensure_user
from plugbackup_bot.domain.payload import parse_payload
from plugbackup_bot.domain.plans import GIB, SUBSCRIPTION_PERIOD_S, Plan, PlanCatalog
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.clock import Clock

log = logging.getLogger(__name__)

STARS_CURRENCY = "XTR"
_TITLE_LIMIT = 32
_LIVE_ON_NODE = (State.ACTIVE, State.EXPIRING, State.SUSPENDED)


class Reject(StrEnum):
    """Почему pre_checkout_query отклонён. Тексты для клиента лежат в tg/texts.py."""

    BAD_PLAN = "bad_plan"
    PRICE_CHANGED = "price_changed"
    NO_RISK_ACK = "no_risk_ack"
    HAS_CLIENT = "has_client"
    NO_CLIENT = "no_client"
    ALREADY_SUBSCRIBED = "already_subscribed"
    NO_CAPACITY = "no_capacity"
    TOO_MUCH_DATA = "too_much_data"
    ADMIN_HOLD = "admin_hold"


class PaymentKind(StrEnum):
    NEW_CLIENT = "new_client"
    PLAN_CHANGED = "plan_changed"
    RENEWED = "renewed"
    DUPLICATE = "duplicate"
    ANOMALY = "anomaly"


@dataclass(frozen=True, slots=True)
class PaymentEvent:
    tg_id: int
    currency: str
    total_amount: int
    payload: str
    charge_id: str
    provider_charge_id: str | None
    is_recurring: bool
    is_first: bool
    sub_expires_at: int | None


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    kind: PaymentKind
    client: Client | None = None
    plan: Plan | None = None
    previous_plan_id: str | None = None
    ask_key: bool = False
    resumed: bool = False
    # Прежняя подписка, которую нужно отключить после смены тарифа.
    cancel_charge_id: str | None = None
    admin_alert: str | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionOutcome:
    state: str
    client: Client


class StarsGateway(Protocol):
    async def set_canceled(self, tg_id: int, charge_id: str, canceled: bool) -> None:
        """Отключает (True) или возвращает (False) автопродление. При сбое бросает исключение."""
        ...


def invoice_params(plan: Plan, payload: str) -> dict[str, object]:
    """Аргументы для createInvoiceLink. Подписка: XTR, период 30 дней, одна позиция в счёте."""
    title = f"PlugBackup {plan.name}"
    if len(title) > _TITLE_LIMIT:
        raise ValueError("название тарифа слишком длинное для счёта")
    return {
        "title": title,
        "description": (
            f"Хранилище {plan.name} для резервных копий. Подписка на 30 дней с автопродлением, "
            "оплата в Telegram Stars. Бета: один диск, без резервирования."
        ),
        "payload": payload,
        "provider_token": "",
        "currency": STARS_CURRENCY,
        "prices": [{"label": plan.name, "amount": plan.stars}],
        "subscription_period": SUBSCRIPTION_PERIOD_S,
    }


class BillingService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        catalog: PlanCatalog,
        stars: StarsGateway,
        risk_version: int,
    ) -> None:
        self._db = db
        self._clock = clock
        self._catalog = catalog
        self._stars = stars
        self._risk_version = risk_version

    # --- перед оплатой ---

    async def check_pre_checkout(
        self, *, tg_id: int, currency: str, total_amount: int, payload: str
    ) -> Reject | None:
        """None, если оплату можно принять. Только база, без сетевых вызовов: ответ за 10 с."""
        parsed = parse_payload(payload)
        plan = self._catalog.find(parsed.plan_id) if parsed else None
        if parsed is None or plan is None or plan.is_beta or currency != STARS_CURRENCY:
            return Reject.BAD_PLAN
        if total_amount != plan.stars:
            return Reject.PRICE_CHANGED
        now = self._clock.now()
        async with self._db.read() as conn:
            if not await has_acked(conn, tg_id, self._risk_version):
                return Reject.NO_RISK_ACK
            live = await get_live_client(conn, tg_id)
            if parsed.client_id == 0:
                if live is not None:
                    return Reject.HAS_CLIENT
                if await pick_node(conn, plan.gb) is None:
                    return Reject.NO_CAPACITY
                return None
            if live is None or live.id != parsed.client_id:
                return Reject.NO_CLIENT
            if live.admin_hold:
                return Reject.ADMIN_HOLD
            if _has_active_subscription(live, plan, now):
                return Reject.ALREADY_SUBSCRIBED
            new_quota_gb = plan.gb + live.bonus_gb  # бонусные ГБ остаются при смене тарифа
            delta = new_quota_gb - live.quota_gb
            if delta > 0 and await free_gb(conn, live.node_id) < delta:
                return Reject.NO_CAPACITY
            if delta < 0:
                sample = await latest_sample(conn, live.id)
                if sample is not None and sample.used_bytes > new_quota_gb * GIB:
                    return Reject.TOO_MUCH_DATA
            return None

    # --- после оплаты ---

    async def handle_payment(self, event: PaymentEvent) -> PaymentOutcome:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            outcome = await self._apply(conn, event, now)
        log.info("payment %s from %s: %s", event.charge_id, event.tg_id, outcome.kind.value)
        return outcome

    async def _apply(
        self, conn: aiosqlite.Connection, event: PaymentEvent, now: int
    ) -> PaymentOutcome:
        existing = await get_by_charge(conn, event.charge_id)
        if existing is not None:
            return await self._repeat(conn, event, existing, now)

        parsed = parse_payload(event.payload)
        plan = self._catalog.find(parsed.plan_id) if parsed else None
        if event.total_amount <= 0:
            return self._anomaly(event, "сумма платежа не положительная")
        await ensure_user(conn, event.tg_id, now)

        async def record(client_id: int | None) -> None:
            await insert_payment(
                conn,
                tg_id=event.tg_id,
                client_id=client_id,
                plan_id=parsed.plan_id if parsed else "?",
                stars=event.total_amount,
                charge_id=event.charge_id,
                provider_charge_id=event.provider_charge_id,
                invoice_payload=event.payload,
                is_recurring=event.is_recurring,
                is_first=event.is_first,
                sub_expires_at=event.sub_expires_at,
                now=now,
            )

        if parsed is None or plan is None or plan.is_beta or event.currency != STARS_CURRENCY:
            await record(None)
            return self._anomaly(event, "счёт не распознан")

        live = await get_live_client(conn, event.tg_id)
        initial = not event.is_recurring or event.is_first
        expires_at = event.sub_expires_at or now + SUBSCRIPTION_PERIOD_S

        if not initial:
            if live is None or live.sub_payload != event.payload:
                await record(live.id if live else None)
                return self._anomaly(event, "продление не относится к подписке клиента", live)
            await record(live.id)
            return await self._renew(conn, event, live, now)

        if parsed.client_id == 0:
            if live is not None:
                await record(live.id)
                return self._anomaly(event, "вторая оплата при действующем хранилище", live)
            node = await pick_node(conn, plan.gb)
            if node is None:
                await record(None)
                return self._anomaly(event, "на нодах нет места под тариф")
            client = await create_client(
                conn,
                tg_id=event.tg_id,
                node_id=node.id,
                plan=plan,
                now=now,
                reason="payment",
                actor="system",
                expires_at=expires_at,
            )
            await set_fields(
                conn, client.id, sub_charge_id=event.charge_id, sub_payload=event.payload
            )
            await record(client.id)
            fresh = await get_client(conn, client.id)
            return PaymentOutcome(PaymentKind.NEW_CLIENT, client=fresh, plan=plan, ask_key=True)

        if live is None or live.id != parsed.client_id:
            await record(None)
            return self._anomaly(event, "хранилище для подписки не найдено", live)
        await record(live.id)
        return await self._attach(conn, event, live, plan, expires_at, now)

    async def _repeat(
        self, conn: aiosqlite.Connection, event: PaymentEvent, existing: Payment, now: int
    ) -> PaymentOutcome:
        """Платёж уже записан. Обычно это повторная доставка, и менять ничего не нужно."""
        client = await get_client(conn, existing.client_id) if existing.client_id else None
        newer = event.sub_expires_at
        later = newer is not None and (existing.sub_expires_at or 0) < newer
        if newer is not None and later and event.is_recurring and not event.is_first and client:
            # Страховка: если Telegram повторит charge id у продления, период всё равно продлим.
            log.warning("charge id %s reused by a renewal", event.charge_id)
            await set_sub_expires(conn, existing.id, newer)
            if client.sub_payload == event.payload and client.state is not State.DELETED:
                return await self._renew(conn, event, client, now)
        ask_key = (
            client is not None and client.state is State.PENDING and client.ssh_key_body is None
        )
        return PaymentOutcome(PaymentKind.DUPLICATE, client=client, ask_key=ask_key)

    async def _renew(
        self, conn: aiosqlite.Connection, event: PaymentEvent, client: Client, now: int
    ) -> PaymentOutcome:
        expires_at = max(
            client.expires_at or 0, event.sub_expires_at or now + SUBSCRIPTION_PERIOD_S
        )
        await set_fields(conn, client.id, expires_at=expires_at, renewal_on=True)
        resumed = False
        alert = None
        if client.state is State.EXPIRING:
            await transition(
                conn, client.id, State.ACTIVE, reason="renewal", actor="system", now=now,
                expected_from=State.EXPIRING,
            )  # fmt: skip
        elif client.state is State.SUSPENDED:
            if client.admin_hold:
                alert = _hold_alert(event, client)
            else:
                await self._wake(conn, client, "renewal", now)
                resumed = True
        fresh = await get_client(conn, client.id)
        return PaymentOutcome(PaymentKind.RENEWED, client=fresh, resumed=resumed, admin_alert=alert)

    async def _attach(
        self,
        conn: aiosqlite.Connection,
        event: PaymentEvent,
        client: Client,
        plan: Plan,
        expires_at: int,
        now: int,
    ) -> PaymentOutcome:
        """Новая подписка для существующего хранилища: смена тарифа или повторная оплата."""
        await set_fields(
            conn,
            client.id,
            plan_id=plan.id,
            quota_gb=plan.gb + client.bonus_gb,
            expires_at=expires_at,
            confirm_due_at=None,
            sub_charge_id=event.charge_id,
            sub_payload=event.payload,
            renewal_on=True,
        )
        resumed = False
        alert = None
        if client.state is State.EXPIRING:
            await transition(
                conn, client.id, State.ACTIVE, reason="payment", actor="system", now=now,
                expected_from=State.EXPIRING,
            )  # fmt: skip
        elif client.state is State.SUSPENDED:
            if client.admin_hold:
                alert = _hold_alert(event, client)
            else:
                await self._wake(conn, client, "payment", now)
                resumed = True
        if client.state in _LIVE_ON_NODE and plan.gb + client.bonus_gb != client.quota_gb:
            await jobs_repo.enqueue(conn, client.id, Op.RESIZE, now)
        old_charge = client.sub_charge_id
        fresh = await get_client(conn, client.id)
        return PaymentOutcome(
            PaymentKind.PLAN_CHANGED,
            client=fresh,
            plan=plan,
            previous_plan_id=client.plan_id,
            ask_key=fresh is not None and fresh.ssh_key_body is None,
            resumed=resumed,
            cancel_charge_id=old_charge if old_charge and old_charge != event.charge_id else None,
            admin_alert=alert,
        )

    @staticmethod
    async def _wake(conn: aiosqlite.Connection, client: Client, reason: str, now: int) -> None:
        await transition(
            conn, client.id, State.ACTIVE, reason=reason, actor="system", now=now,
            expected_from=State.SUSPENDED,
        )  # fmt: skip
        await jobs_repo.enqueue(conn, client.id, Op.RESUME, now)

    @staticmethod
    def _anomaly(event: PaymentEvent, reason: str, client: Client | None = None) -> PaymentOutcome:
        alert = (
            f"Платёж требует ручного разбора: {reason}.\n"
            f"tg_id: {event.tg_id}\n"
            f"сумма: {event.total_amount} {event.currency}\n"
            f"charge_id: {event.charge_id}\n"
            f"payload: {event.payload}"
        )
        return PaymentOutcome(PaymentKind.ANOMALY, client=client, admin_alert=alert)

    # --- события подписки и управление продлением ---

    async def handle_subscription_update(
        self, tg_id: int, payload: str, state: str
    ) -> SubscriptionOutcome | None:
        """canceled / active / failed от Telegram. Чужие и устаревшие подписки игнорируем."""
        async with self._db.transaction() as conn:
            client = await get_live_client(conn, tg_id)
            if client is None or client.sub_payload != payload:
                log.info("subscription update %s for %s ignored: not the current one", state, tg_id)
                return None
            if state == "active":
                await set_fields(conn, client.id, renewal_on=True)
            elif state in ("canceled", "failed"):
                await set_fields(conn, client.id, renewal_on=False)
            else:
                log.warning("unknown subscription state %r", state)
                return None
            fresh = await get_client(conn, client.id)
        if fresh is None:
            raise RuntimeError("клиент пропал после обновления")
        return SubscriptionOutcome(state, fresh)

    async def disable_subscription(self, tg_id: int, charge_id: str) -> bool:
        """Отключает автопродление старой подписки. False, если Telegram не принял запрос."""
        try:
            await self._stars.set_canceled(tg_id, charge_id, True)
        except Exception:
            log.warning("could not cancel subscription of %s", tg_id, exc_info=True)
            return False
        return True

    async def set_renewal(self, client: Client, enabled: bool) -> bool:
        """Включает или отключает автопродление действующей подписки клиента."""
        if client.sub_charge_id is None:
            return False
        try:
            await self._stars.set_canceled(client.tg_id, client.sub_charge_id, not enabled)
        except Exception:
            log.warning("could not change renewal of %s", client.tg_id, exc_info=True)
            return False
        async with self._db.transaction() as conn:
            await set_fields(conn, client.id, renewal_on=enabled)
        return True


def _has_active_subscription(client: Client, plan: Plan, now: int) -> bool:
    return (
        client.plan_id == plan.id
        and client.sub_charge_id is not None
        and client.renewal_on
        and client.state in (State.ACTIVE, State.EXPIRING)
        and (client.expires_at or 0) > now
    )


def _hold_alert(event: PaymentEvent, client: Client) -> str:
    return (
        "Оплата от клиента на ручной блокировке, доступ не возвращён.\n"
        f"логин: {client.login}, tg_id: {event.tg_id}, charge_id: {event.charge_id}"
    )
