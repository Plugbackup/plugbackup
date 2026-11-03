from dataclasses import dataclass, replace

import pytest
import pytest_asyncio

from fakes import FakeClock, FakeStars, FakeStorectl, RecordingHooks
from helpers import CATALOG, NOW, add_node, fake_key_body
from plugbackup_bot.db import Database
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.clients import Client, get_client, get_live_client, set_fields, transition
from plugbackup_bot.db.jobs import JobStatus, Op
from plugbackup_bot.db.risk import ack_risk
from plugbackup_bot.db.usage import record_sample
from plugbackup_bot.domain.payload import make_payload
from plugbackup_bot.domain.plans import GIB, SUBSCRIPTION_PERIOD_S
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.storectl import ErrorKind, StorectlError
from plugbackup_bot.services.billing import (
    BillingService,
    PaymentEvent,
    PaymentKind,
    Reject,
)
from plugbackup_bot.services.provisioning import Provisioner

KEY = fake_key_body(0)
MONTH = SUBSCRIPTION_PERIOD_S
RISK_VERSION = 1


@dataclass
class Env:
    db: Database
    clock: FakeClock
    stars: FakeStars
    billing: BillingService
    node_id: int

    async def ack(self, tg_id: int = 1, version: int = RISK_VERSION) -> None:
        async with self.db.transaction() as conn:
            await ack_risk(conn, tg_id, version, self.clock.now())

    def event(self, payload: str, *, charge: str = "ch-1", tg_id: int = 1, **over) -> PaymentEvent:
        plan_id = payload.split(":")[1]
        base = PaymentEvent(
            tg_id=tg_id,
            currency="XTR",
            total_amount=CATALOG.get(plan_id).stars,
            payload=payload,
            charge_id=charge,
            provider_charge_id="prov",
            is_recurring=True,
            is_first=True,
            sub_expires_at=self.clock.now() + MONTH,
        )
        return replace(base, **over)

    async def onboard(self, plan_id: str = "gb50", tg_id: int = 1, charge: str = "ch-1"):
        """Клиент с оплаченной подпиской, ключом и состоянием active: (клиент, payload)."""
        await self.ack(tg_id)
        payload = make_payload(plan_id)
        outcome = await self.billing.handle_payment(self.event(payload, charge=charge, tg_id=tg_id))
        assert outcome.client is not None
        async with self.db.transaction() as conn:
            await set_fields(conn, outcome.client.id, ssh_key_body=KEY)
            await transition(
                conn, outcome.client.id, State.ACTIVE, reason="t", actor="system", now=NOW
            )
        return await self.client(outcome.client.id), payload

    async def client(self, client_id: int) -> Client:
        async with self.db.read() as conn:
            client = await get_client(conn, client_id)
        assert client is not None
        return client

    async def set_state(self, client_id: int, state: State) -> None:
        async with self.db.transaction() as conn:
            current = (await get_client(conn, client_id)).state  # type: ignore[union-attr]
            if current is State.PENDING and state is State.SUSPENDED:
                await transition(conn, client_id, State.ACTIVE, reason="t", actor="system", now=NOW)
            await transition(conn, client_id, state, reason="t", actor="system", now=NOW)

    async def jobs(self, client_id: int) -> list[tuple[Op, JobStatus]]:
        async with self.db.read() as conn:
            return [(j.op, j.status) for j in await jobs_repo.list_for_client(conn, client_id)]

    async def count(self, table: str) -> int:
        async with self.db.read() as conn, conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # noqa: S608
            row = await cur.fetchone()
        assert row is not None
        return row[0]

    async def usage(self, client_id: int, used_bytes: int) -> None:
        async with self.db.transaction() as conn:
            await record_sample(conn, client_id, NOW, used_bytes, 50 * GIB)


@pytest_asyncio.fixture
async def env(db: Database, clock: FakeClock) -> Env:
    node_id = await add_node(db, capacity_gb=1000)
    stars = FakeStars()
    billing = BillingService(db, clock, CATALOG, stars, RISK_VERSION)
    return Env(db, clock, stars, billing, node_id)


async def check(env: Env, payload: str, tg_id: int = 1, **over) -> Reject | None:
    plan_id = payload.split(":")[1]
    args = {
        "tg_id": tg_id,
        "currency": "XTR",
        "total_amount": CATALOG.get(plan_id).stars if CATALOG.find(plan_id) else 1,
    }
    args.update(over)
    return await env.billing.check_pre_checkout(payload=payload, **args)  # type: ignore[arg-type]


# --- pre_checkout ---


async def test_pre_checkout_accepts_a_new_purchase(env: Env) -> None:
    await env.ack()
    assert await check(env, make_payload("gb50")) is None


async def test_pre_checkout_needs_the_current_risk_ack(env: Env) -> None:
    assert await check(env, make_payload("gb50")) is Reject.NO_RISK_ACK
    await env.ack(version=RISK_VERSION - 1)
    assert await check(env, make_payload("gb50")) is Reject.NO_RISK_ACK
    await env.ack()
    assert await check(env, make_payload("gb50")) is None


@pytest.mark.parametrize(
    ("payload", "over", "expected"),
    [
        ("garbage", {}, Reject.BAD_PLAN),
        (make_payload("beta"), {"total_amount": 1}, Reject.BAD_PLAN),
        (make_payload("nope"), {"total_amount": 1}, Reject.BAD_PLAN),
        (make_payload("gb50"), {"currency": "USD"}, Reject.BAD_PLAN),
        (make_payload("gb50"), {"total_amount": 51}, Reject.PRICE_CHANGED),
    ],
)
async def test_pre_checkout_rejects_bad_invoices(env: Env, payload, over, expected) -> None:
    await env.ack()
    args = {"tg_id": 1, "currency": "XTR", "total_amount": 50, **over}
    assert await env.billing.check_pre_checkout(payload=payload, **args) is expected


async def test_pre_checkout_blocks_a_second_storage(env: Env) -> None:
    await env.onboard()
    assert await check(env, make_payload("gb100")) is Reject.HAS_CLIENT


async def test_pre_checkout_checks_the_client_id(env: Env) -> None:
    client, _ = await env.onboard()
    await env.ack(2)
    assert await check(env, make_payload("gb100", client.id), tg_id=2) is Reject.NO_CLIENT
    assert await check(env, make_payload("gb100", client.id + 5)) is Reject.NO_CLIENT


async def test_pre_checkout_blocks_a_duplicate_subscription(env: Env) -> None:
    client, _ = await env.onboard()
    assert await check(env, make_payload("gb50", client.id)) is Reject.ALREADY_SUBSCRIBED
    # Другой тариф можно, и тот же тариф можно после отключения продления.
    assert await check(env, make_payload("gb100", client.id)) is None
    async with env.db.transaction() as conn:
        await set_fields(conn, client.id, renewal_on=False)
    assert await check(env, make_payload("gb50", client.id)) is None


async def test_pre_checkout_allows_resubscribing_after_suspension(env: Env) -> None:
    client, _ = await env.onboard()
    await env.set_state(client.id, State.SUSPENDED)
    assert await check(env, make_payload("gb50", client.id)) is None


async def test_pre_checkout_respects_manual_hold(env: Env) -> None:
    client, _ = await env.onboard()
    async with env.db.transaction() as conn:
        await set_fields(conn, client.id, admin_hold=True)
    assert await check(env, make_payload("gb100", client.id)) is Reject.ADMIN_HOLD


async def test_pre_checkout_needs_free_space_for_new_clients(
    db: Database, clock: FakeClock
) -> None:
    await add_node(db, capacity_gb=40)
    billing = BillingService(db, clock, CATALOG, FakeStars(), RISK_VERSION)
    async with db.transaction() as conn:
        await ack_risk(conn, 1, RISK_VERSION, NOW)
    result = await billing.check_pre_checkout(
        tg_id=1, currency="XTR", total_amount=50, payload=make_payload("gb50")
    )
    assert result is Reject.NO_CAPACITY


async def test_pre_checkout_needs_free_space_for_an_upgrade(db: Database, clock: FakeClock) -> None:
    await add_node(db, capacity_gb=60)
    env = Env(db, clock, FakeStars(), BillingService(db, clock, CATALOG, FakeStars(), 1), 1)
    client, _ = await env.onboard()  # занято 50 из 60
    assert await check(env, make_payload("gb100", client.id)) is Reject.NO_CAPACITY


async def test_pre_checkout_blocks_a_downgrade_below_the_data(env: Env) -> None:
    client, _ = await env.onboard("gb100")
    assert await check(env, make_payload("gb50", client.id)) is None  # замеров нет
    await env.usage(client.id, 60 * GIB)
    assert await check(env, make_payload("gb50", client.id)) is Reject.TOO_MUCH_DATA
    await env.usage(client.id, 40 * GIB)
    assert await check(env, make_payload("gb50", client.id)) is None


# --- новый клиент и идемпотентность ---


async def test_first_payment_creates_a_pending_client(env: Env) -> None:
    await env.ack()
    payload = make_payload("gb50")
    event = env.event(payload)
    outcome = await env.billing.handle_payment(event)

    assert outcome.kind is PaymentKind.NEW_CLIENT
    assert outcome.ask_key
    client = outcome.client
    assert client is not None
    assert client.state is State.PENDING
    assert (client.plan_id, client.quota_gb, client.node_id) == ("gb50", 50, env.node_id)
    assert client.expires_at == event.sub_expires_at
    assert (client.sub_charge_id, client.sub_payload, client.renewal_on) == ("ch-1", payload, True)
    assert await env.count("payments") == 1
    assert await env.jobs(client.id) == []  # ADD ставится после получения ключа


async def test_repeated_payment_does_not_create_a_second_client(env: Env) -> None:
    await env.ack()
    event = env.event(make_payload("gb50"))
    first = await env.billing.handle_payment(event)
    second = await env.billing.handle_payment(event)
    third = await env.billing.handle_payment(event)

    assert first.kind is PaymentKind.NEW_CLIENT
    assert second.kind is third.kind is PaymentKind.DUPLICATE
    assert second.client is not None
    assert second.client.id == first.client.id  # type: ignore[union-attr]
    assert second.ask_key  # ключа ещё нет: просим снова, на случай потерянного сообщения
    assert await env.count("clients") == 1
    assert await env.count("payments") == 1
    assert await env.count("state_log") == 1


async def test_duplicate_stops_asking_for_the_key_once_it_is_known(env: Env) -> None:
    client, payload = await env.onboard()
    again = await env.billing.handle_payment(env.event(payload))
    assert again.kind is PaymentKind.DUPLICATE
    assert not again.ask_key
    assert again.client == await env.client(client.id)


async def test_new_client_goes_to_the_node_with_most_free_space(
    db: Database, clock: FakeClock
) -> None:
    await add_node(db, "small", capacity_gb=100)
    big = await add_node(db, "big", capacity_gb=500)
    env = Env(db, clock, FakeStars(), BillingService(db, clock, CATALOG, FakeStars(), 1), big)
    client, _ = await env.onboard()
    assert client.node_id == big


async def test_no_free_space_is_an_anomaly_but_the_payment_is_kept(
    db: Database, clock: FakeClock
) -> None:
    await add_node(db, capacity_gb=10)
    env = Env(db, clock, FakeStars(), BillingService(db, clock, CATALOG, FakeStars(), 1), 1)
    outcome = await env.billing.handle_payment(env.event(make_payload("gb50")))
    assert outcome.kind is PaymentKind.ANOMALY
    assert outcome.admin_alert and "нет места" in outcome.admin_alert
    assert await env.count("clients") == 0
    assert await env.count("payments") == 1


@pytest.mark.parametrize(
    "over",
    [
        {"payload": "not-ours"},
        {"payload": make_payload("beta"), "total_amount": 5},
        {"currency": "USD"},
    ],
)
async def test_unknown_invoices_are_recorded_and_reported(env: Env, over) -> None:
    event = replace(env.event(make_payload("gb50")), **over)
    first = await env.billing.handle_payment(event)
    again = await env.billing.handle_payment(event)
    assert first.kind is PaymentKind.ANOMALY
    assert first.admin_alert and event.charge_id in first.admin_alert
    assert again.kind is PaymentKind.DUPLICATE
    assert again.admin_alert is None
    assert await env.count("clients") == 0
    assert await env.count("payments") == 1


async def test_zero_amount_is_reported_without_a_row(env: Env) -> None:
    outcome = await env.billing.handle_payment(env.event(make_payload("gb50"), total_amount=0))
    assert outcome.kind is PaymentKind.ANOMALY
    assert await env.count("payments") == 0


async def test_paying_again_for_a_new_storage_is_an_anomaly(env: Env) -> None:
    client, _ = await env.onboard()
    outcome = await env.billing.handle_payment(
        env.event(make_payload("gb100"), charge="ch-2", total_amount=100)
    )
    assert outcome.kind is PaymentKind.ANOMALY
    assert await env.count("clients") == 1
    assert await env.count("payments") == 2
    assert (await env.client(client.id)).plan_id == "gb50"


# --- продления ---


def renewal(env: Env, payload: str, charge: str = "ch-r1", **over) -> PaymentEvent:
    return env.event(
        payload,
        charge=charge,
        is_first=False,
        sub_expires_at=env.clock.now() + 2 * MONTH,
        **over,
    )


async def test_renewal_extends_the_period(env: Env) -> None:
    client, payload = await env.onboard()
    event = renewal(env, payload)
    outcome = await env.billing.handle_payment(event)
    assert outcome.kind is PaymentKind.RENEWED
    renewed = await env.client(client.id)
    assert renewed.expires_at == event.sub_expires_at
    assert renewed.sub_charge_id == "ch-1"  # идентификатор подписки не меняется
    assert renewed.state is State.ACTIVE
    assert await env.count("payments") == 2
    assert await env.jobs(client.id) == []


async def test_renewal_brings_an_expiring_client_back(env: Env) -> None:
    client, payload = await env.onboard()
    await env.set_state(client.id, State.EXPIRING)
    await env.billing.handle_payment(renewal(env, payload))
    assert (await env.client(client.id)).state is State.ACTIVE


async def test_renewal_wakes_a_suspended_client(env: Env) -> None:
    client, payload = await env.onboard()
    await env.set_state(client.id, State.SUSPENDED)
    outcome = await env.billing.handle_payment(renewal(env, payload))
    assert outcome.resumed
    assert (await env.client(client.id)).state is State.ACTIVE
    assert await env.jobs(client.id) == [(Op.RESUME, JobStatus.PENDING)]


async def test_renewal_does_not_override_a_manual_hold(env: Env) -> None:
    client, payload = await env.onboard()
    await env.set_state(client.id, State.SUSPENDED)
    async with env.db.transaction() as conn:
        await set_fields(conn, client.id, admin_hold=True)
    outcome = await env.billing.handle_payment(renewal(env, payload))
    assert not outcome.resumed
    assert outcome.admin_alert and client.login in outcome.admin_alert
    assert (await env.client(client.id)).state is State.SUSPENDED
    assert await env.jobs(client.id) == []


async def test_renewal_of_someone_elses_subscription_is_an_anomaly(env: Env) -> None:
    client, _ = await env.onboard()
    stale = renewal(env, make_payload("gb50", client.id), charge="ch-old")
    outcome = await env.billing.handle_payment(stale)
    assert outcome.kind is PaymentKind.ANOMALY
    assert (await env.client(client.id)).expires_at == client.expires_at


async def test_renewal_without_a_storage_is_an_anomaly(env: Env) -> None:
    outcome = await env.billing.handle_payment(renewal(env, make_payload("gb50")))
    assert outcome.kind is PaymentKind.ANOMALY
    assert await env.count("payments") == 1


async def test_a_reused_charge_id_still_extends_the_period(env: Env) -> None:
    """Если Telegram повторит charge id у продления, клиент не должен остаться без периода."""
    client, payload = await env.onboard()
    reused = renewal(env, payload, charge="ch-1")
    outcome = await env.billing.handle_payment(reused)
    assert outcome.kind is PaymentKind.RENEWED
    assert (await env.client(client.id)).expires_at == reused.sub_expires_at
    assert await env.count("payments") == 1
    # Тот же платёж ещё раз: период уже продлён, менять нечего.
    assert (await env.billing.handle_payment(reused)).kind is PaymentKind.DUPLICATE


# --- смена тарифа ---


async def test_upgrade_resizes_and_replaces_the_subscription(env: Env) -> None:
    client, _ = await env.onboard("gb50", charge="ch-old")
    payload = make_payload("gb100", client.id)
    event = env.event(payload, charge="ch-new", total_amount=100)
    outcome = await env.billing.handle_payment(event)

    assert outcome.kind is PaymentKind.PLAN_CHANGED
    assert outcome.previous_plan_id == "gb50"
    assert outcome.cancel_charge_id == "ch-old"
    assert not outcome.ask_key
    changed = await env.client(client.id)
    assert (changed.plan_id, changed.quota_gb) == ("gb100", 100)
    assert (changed.sub_charge_id, changed.sub_payload) == ("ch-new", payload)
    assert changed.expires_at == event.sub_expires_at
    assert await env.jobs(client.id) == [(Op.RESIZE, JobStatus.PENDING)]


async def test_downgrade_is_the_same_path(env: Env) -> None:
    client, _ = await env.onboard("gb100", charge="ch-old")
    event = env.event(make_payload("gb50", client.id), charge="ch-new")
    outcome = await env.billing.handle_payment(event)
    assert outcome.kind is PaymentKind.PLAN_CHANGED
    assert (await env.client(client.id)).quota_gb == 50
    assert await env.jobs(client.id) == [(Op.RESIZE, JobStatus.PENDING)]


async def test_beta_client_moves_to_a_paid_plan_without_cancelling_anything(env: Env) -> None:
    await env.ack()
    async with env.db.transaction() as conn:
        from plugbackup_bot.db.clients import create_client

        beta = await create_client(
            conn, tg_id=1, node_id=env.node_id, plan=CATALOG.beta, now=NOW, reason="invite",
            actor="system", confirm_due_at=NOW + 30 * 86400,
        )  # fmt: skip
        await set_fields(conn, beta.id, ssh_key_body=KEY)
        await transition(conn, beta.id, State.ACTIVE, reason="t", actor="system", now=NOW)
    outcome = await env.billing.handle_payment(env.event(make_payload("gb50", beta.id)))
    assert outcome.kind is PaymentKind.PLAN_CHANGED
    assert outcome.cancel_charge_id is None
    upgraded = await env.client(beta.id)
    assert (upgraded.plan_id, upgraded.quota_gb, upgraded.confirm_due_at) == ("gb50", 50, None)


async def test_resubscribing_the_same_plan_does_not_resize(env: Env) -> None:
    client, _ = await env.onboard(charge="ch-old")
    await env.set_state(client.id, State.SUSPENDED)
    outcome = await env.billing.handle_payment(
        env.event(make_payload("gb50", client.id), charge="ch-new")
    )
    assert outcome.resumed
    assert outcome.cancel_charge_id == "ch-old"
    assert (await env.client(client.id)).state is State.ACTIVE
    assert await env.jobs(client.id) == [(Op.RESUME, JobStatus.PENDING)]


async def test_suspended_client_changing_plan_is_resumed_then_resized(env: Env) -> None:
    client, _ = await env.onboard()
    await env.set_state(client.id, State.SUSPENDED)
    await env.billing.handle_payment(
        env.event(make_payload("gb100", client.id), charge="ch-new", total_amount=100)
    )
    assert [op for op, _ in await env.jobs(client.id)] == [Op.RESUME, Op.RESIZE]


async def test_plan_change_before_the_key_does_not_touch_the_node(env: Env) -> None:
    await env.ack()
    first = await env.billing.handle_payment(env.event(make_payload("gb50")))
    assert first.client is not None
    outcome = await env.billing.handle_payment(
        env.event(make_payload("gb100", first.client.id), charge="ch-2", total_amount=100)
    )
    assert outcome.kind is PaymentKind.PLAN_CHANGED
    assert outcome.ask_key
    assert await env.jobs(first.client.id) == []


async def test_subscription_for_a_foreign_storage_is_an_anomaly(env: Env) -> None:
    client, _ = await env.onboard()
    outcome = await env.billing.handle_payment(
        env.event(make_payload("gb100", client.id + 7), charge="ch-2", total_amount=100, tg_id=1)
    )
    assert outcome.kind is PaymentKind.ANOMALY
    assert (await env.client(client.id)).plan_id == "gb50"


# --- события подписки и управление продлением ---


@pytest.mark.parametrize(
    ("state", "renewal_on"), [("canceled", False), ("failed", False), ("active", True)]
)
async def test_subscription_updates_toggle_renewal(env: Env, state: str, renewal_on: bool) -> None:
    client, payload = await env.onboard()
    async with env.db.transaction() as conn:
        await set_fields(conn, client.id, renewal_on=not renewal_on)
    outcome = await env.billing.handle_subscription_update(1, payload, state)
    assert outcome is not None
    assert outcome.state == state
    assert (await env.client(client.id)).renewal_on is renewal_on


async def test_updates_for_old_subscriptions_are_ignored(env: Env) -> None:
    client, payload = await env.onboard("gb50", charge="ch-old")
    await env.billing.handle_payment(
        env.event(make_payload("gb100", client.id), charge="ch-new", total_amount=100)
    )
    # Telegram сообщает об отмене прежней подписки: на новую это влиять не должно.
    assert await env.billing.handle_subscription_update(1, payload, "canceled") is None
    assert (await env.client(client.id)).renewal_on is True


async def test_unknown_users_and_states_are_ignored(env: Env) -> None:
    client, payload = await env.onboard()
    assert await env.billing.handle_subscription_update(99, payload, "canceled") is None
    assert await env.billing.handle_subscription_update(1, payload, "paused") is None
    assert (await env.client(client.id)).renewal_on is True


async def test_disable_subscription_reports_the_result(env: Env) -> None:
    assert await env.billing.disable_subscription(1, "ch-old") is True
    assert env.stars.calls == [(1, "ch-old", True)]
    env.stars.fail = True
    assert await env.billing.disable_subscription(1, "ch-old") is False


async def test_set_renewal_updates_the_database_only_on_success(env: Env) -> None:
    client, _ = await env.onboard()
    assert await env.billing.set_renewal(client, False) is True
    assert env.stars.calls[-1] == (1, "ch-1", True)
    assert (await env.client(client.id)).renewal_on is False
    assert await env.billing.set_renewal(client, True) is True
    assert env.stars.calls[-1] == (1, "ch-1", False)
    assert (await env.client(client.id)).renewal_on is True

    env.stars.fail = True
    assert await env.billing.set_renewal(client, False) is False
    assert (await env.client(client.id)).renewal_on is True


async def test_set_renewal_without_a_subscription(env: Env) -> None:
    client, _ = await env.onboard()
    bare = replace(client, sub_charge_id=None)
    assert await env.billing.set_renewal(bare, False) is False
    assert env.stars.calls == []


# --- от оплаты до выдачи доступа ---


async def test_payment_to_active_client_with_mocked_storectl(env: Env) -> None:
    await env.ack()
    outcome = await env.billing.handle_payment(env.event(make_payload("gb100"), total_amount=100))
    client = outcome.client
    assert client is not None

    # Клиент прислал ключ: бот записывает его и ставит add в очередь.
    async with env.db.transaction() as conn:
        await set_fields(conn, client.id, ssh_key_body=KEY)
        await jobs_repo.enqueue(conn, client.id, Op.ADD, env.clock.now())

    storectl, hooks = FakeStorectl(), RecordingHooks()
    await Provisioner(env.db, storectl, env.clock, hooks).run_due()

    assert storectl.calls == [("add", "192.0.2.10", client.login, 100, KEY)]
    assert (await env.client(client.id)).state is State.ACTIVE
    assert len(hooks.done) == 1


async def test_storectl_failure_after_payment_keeps_the_job_and_alerts(env: Env) -> None:
    await env.ack()
    outcome = await env.billing.handle_payment(env.event(make_payload("gb50")))
    assert outcome.client is not None
    async with env.db.transaction() as conn:
        await set_fields(conn, outcome.client.id, ssh_key_body=KEY)
        await jobs_repo.enqueue(conn, outcome.client.id, Op.ADD, env.clock.now())

    storectl, hooks = FakeStorectl(), RecordingHooks()
    storectl.fail("add", StorectlError(ErrorKind.UNREACHABLE, "ssh: timeout", exit_code=255))
    provisioner = Provisioner(env.db, storectl, env.clock, hooks)
    await provisioner.run_due()

    assert (await env.client(outcome.client.id)).state is State.PENDING
    assert await env.jobs(outcome.client.id) == [(Op.ADD, JobStatus.PENDING)]
    assert [final for _, _, final in hooks.problems] == [False]  # админ получил алерт
    # Платёж на месте, повтор доставки не создаёт второго клиента.
    await env.billing.handle_payment(env.event(make_payload("gb50")))
    assert await env.count("clients") == 1

    env.clock.advance(60)
    await provisioner.run_due()
    assert (await env.client(outcome.client.id)).state is State.ACTIVE


async def test_live_client_lookup_ignores_deleted(env: Env) -> None:
    client, _ = await env.onboard()
    async with env.db.transaction() as conn:
        await transition(conn, client.id, State.SUSPENDED, reason="t", actor="system", now=NOW)
        await transition(conn, client.id, State.DELETED, reason="t", actor="system", now=NOW)
    async with env.db.read() as conn:
        assert await get_live_client(conn, 1) is None
