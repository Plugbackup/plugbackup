"""Выдача доступа: инвайты беты, ключи, предложение оплаты и статус клиента."""

import logging
from dataclasses import dataclass
from enum import StrEnum

from plugbackup_bot.db import Database
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.capacity import pick_node
from plugbackup_bot.db.clients import Client, create_client, get_client, get_live_client, set_fields
from plugbackup_bot.db.jobs import Op
from plugbackup_bot.db.nodes import Node, get_node
from plugbackup_bot.db.risk import ack_risk, has_acked
from plugbackup_bot.db.usage import Sample, latest_sample
from plugbackup_bot.db.users import beta_used, ensure_user, mark_beta_used
from plugbackup_bot.domain.payload import make_payload
from plugbackup_bot.domain.plans import Plan, PlanCatalog
from plugbackup_bot.domain.policy import BETA_CONFIRM_PERIOD_S
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.infra.storectl import Storectl, StorectlError
from plugbackup_bot.services.billing import STARS_CURRENCY, BillingService, Reject, invoice_params

log = logging.getLogger(__name__)


class RedeemKind(StrEnum):
    OK = "ok"
    INVALID = "invalid"
    USED = "used"
    ALREADY_BETA = "already_beta"
    HAS_CLIENT = "has_client"
    NEED_ACK = "need_ack"
    NO_CAPACITY = "no_capacity"


class KeyKind(StrEnum):
    ACCEPTED = "accepted"
    REPLACED = "replaced"
    UNCHANGED = "unchanged"
    NO_CLIENT = "no_client"
    BLOCKED = "blocked"


class OfferKind(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RedeemResult:
    kind: RedeemKind
    client: Client | None = None


@dataclass(frozen=True, slots=True)
class KeyResult:
    kind: KeyKind
    client: Client | None = None


@dataclass(frozen=True, slots=True)
class InvoiceOffer:
    kind: OfferKind
    plan: Plan | None = None
    params: dict[str, object] | None = None
    reject: Reject | None = None
    # Новая подписка заменит действующую платную: пересчёта за остаток периода нет.
    replaces_subscription: bool = False


@dataclass(frozen=True, slots=True)
class StatusView:
    client: Client
    plan: Plan | None
    node: Node | None
    sample: Sample | None


class AccessService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        catalog: PlanCatalog,
        billing: BillingService,
        storectl: Storectl,
        risk_version: int,
    ) -> None:
        self._db = db
        self._clock = clock
        self._catalog = catalog
        self._billing = billing
        self._storectl = storectl
        self._risk_version = risk_version

    @property
    def beta_plan(self) -> Plan:
        return self._catalog.beta

    # --- риски и клиент ---

    async def acknowledge_risks(self, tg_id: int) -> None:
        async with self._db.transaction() as conn:
            await ack_risk(conn, tg_id, self._risk_version, self._clock.now())

    async def live_client(self, tg_id: int) -> Client | None:
        async with self._db.read() as conn:
            return await get_live_client(conn, tg_id)

    async def awaiting_key(self, tg_id: int) -> bool:
        """Клиент оплатил или получил инвайт, но ключа у него ещё нет."""
        client = await self.live_client(tg_id)
        return client is not None and client.state is State.PENDING and client.ssh_key_body is None

    # --- инвайт беты ---

    async def redeem_invite(self, tg_id: int, text: str) -> RedeemResult:
        now = self._clock.now()
        beta = self._catalog.beta
        async with self._db.transaction() as conn:
            await ensure_user(conn, tg_id, now)
            if not await has_acked(conn, tg_id, self._risk_version):
                return RedeemResult(RedeemKind.NEED_ACK)
            if await get_live_client(conn, tg_id) is not None:
                return RedeemResult(RedeemKind.HAS_CLIENT)
            if await beta_used(conn, tg_id):
                return RedeemResult(RedeemKind.ALREADY_BETA)
            node = await pick_node(conn, beta.gb)
            if node is None:
                return RedeemResult(RedeemKind.NO_CAPACITY)
            status = await invites_repo.redeem(conn, text, tg_id, now)
            if status is invites_repo.RedeemStatus.INVALID:
                return RedeemResult(RedeemKind.INVALID)
            if status is invites_repo.RedeemStatus.USED:
                return RedeemResult(RedeemKind.USED)
            client = await create_client(
                conn,
                tg_id=tg_id,
                node_id=node.id,
                plan=beta,
                now=now,
                reason="invite",
                actor="system",
                confirm_due_at=now + BETA_CONFIRM_PERIOD_S,
            )
            await mark_beta_used(conn, tg_id, now)
        log.info("invite redeemed by %s, client %s", tg_id, client.id)
        return RedeemResult(RedeemKind.OK, client)

    # --- ключ ---

    async def submit_key(self, tg_id: int, key_body: str) -> KeyResult:
        """Сохраняет проверенное тело ключа и ставит add в очередь."""
        now = self._clock.now()
        async with self._db.transaction() as conn:
            client = await get_live_client(conn, tg_id)
            if client is None:
                return KeyResult(KeyKind.NO_CLIENT)
            if client.state is State.SUSPENDED:
                return KeyResult(KeyKind.BLOCKED, client)
            replace = client.state is not State.PENDING
            if replace and client.ssh_key_body == key_body:
                return KeyResult(KeyKind.UNCHANGED, client)
            fields: dict[str, object] = {"ssh_key_body": key_body}
            if not replace and self._catalog.beta.id == client.plan_id:
                # Бета-цикл считаем с момента, когда клиент получил доступ.
                fields["confirm_due_at"] = now + BETA_CONFIRM_PERIOD_S
            await set_fields(conn, client.id, **fields)
            # Если add уже ждёт повтора, он возьмёт свежий ключ из базы сам.
            if not await jobs_repo.has_open(conn, client.id, Op.ADD):
                await jobs_repo.enqueue(conn, client.id, Op.ADD, now, {"replace": replace})
            fresh = await get_client(conn, client.id)
        return KeyResult(KeyKind.REPLACED if replace else KeyKind.ACCEPTED, fresh)

    # --- оплата ---

    async def offer_plan(self, tg_id: int, plan_id: str) -> InvoiceOffer:
        """Проверяет, можно ли оформить тариф, и готовит аргументы для createInvoiceLink."""
        plan = self._catalog.find(plan_id)
        if plan is None or plan.is_beta:
            return InvoiceOffer(OfferKind.REJECTED, reject=Reject.BAD_PLAN)
        live = await self.live_client(tg_id)
        payload = make_payload(plan.id, live.id if live else 0)
        reject = await self._billing.check_pre_checkout(
            tg_id=tg_id, currency=STARS_CURRENCY, total_amount=plan.stars, payload=payload
        )
        if reject is not None:
            return InvoiceOffer(OfferKind.REJECTED, plan, reject=reject)
        if live is not None and plan.gb < live.quota_gb and live.state is not State.PENDING:
            verdict = await self._fresh_usage_fits(live, plan)
            if verdict is not None:
                return verdict
        replaces = (
            live is not None
            and live.sub_charge_id is not None
            and live.renewal_on
            and live.state in (State.ACTIVE, State.EXPIRING)
        )
        return InvoiceOffer(
            OfferKind.OK,
            plan,
            params=invoice_params(plan, payload),
            replaces_subscription=replaces,
        )

    async def _fresh_usage_fits(self, client: Client, plan: Plan) -> InvoiceOffer | None:
        """Понижение тарифа: занятое место проверяем свежим запросом к ноде."""
        async with self._db.read() as conn:
            node = await get_node(conn, client.node_id)
        if node is None:
            return InvoiceOffer(OfferKind.UNAVAILABLE, plan)
        try:
            usage = await self._storectl.usage(node.addr, client.login)
        except StorectlError:
            log.warning("could not check usage of %s before a downgrade", client.login)
            return InvoiceOffer(OfferKind.UNAVAILABLE, plan)
        if usage.used_bytes > plan.quota_bytes:
            return InvoiceOffer(OfferKind.REJECTED, plan, reject=Reject.TOO_MUCH_DATA)
        return None

    # --- статус ---

    async def status(self, tg_id: int) -> StatusView | None:
        async with self._db.read() as conn:
            client = await get_live_client(conn, tg_id)
            if client is None:
                return None
            node = await get_node(conn, client.node_id)
            sample = await latest_sample(conn, client.id)
        return StatusView(client, self._catalog.find(client.plan_id), node, sample)
