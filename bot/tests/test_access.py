import pytest
import pytest_asyncio

from fakes import FakeClock
from harness import Harness
from helpers import CATALOG, NOW, fake_key_body
from plugbackup_bot.db import Database
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.clients import Client, get_client, set_fields, transition
from plugbackup_bot.db.jobs import JobStatus, Op
from plugbackup_bot.db.usage import record_sample
from plugbackup_bot.domain.payload import make_payload, parse_payload
from plugbackup_bot.domain.plans import GIB
from plugbackup_bot.domain.policy import BETA_CONFIRM_PERIOD_S
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.storectl import ErrorKind, StorectlError, Usage
from plugbackup_bot.services.access import KeyKind, OfferKind, RedeemKind
from plugbackup_bot.services.billing import PaymentEvent, Reject

KEY_A = fake_key_body(0)
KEY_B = fake_key_body(1)


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    return await Harness.build(db, clock)


async def new_invite(h: Harness) -> str:
    return await h.invite()


async def paid_client(h: Harness, plan_id: str = "gb50", tg_id: int = 1) -> Client:
    await h.access.acknowledge_risks(tg_id)
    event = PaymentEvent(
        tg_id=tg_id,
        currency="XTR",
        total_amount=CATALOG.get(plan_id).stars,
        payload=make_payload(plan_id),
        charge_id=f"charge-{tg_id}",
        provider_charge_id=None,
        is_recurring=True,
        is_first=True,
        sub_expires_at=NOW + 2_592_000,
    )
    outcome = await h.billing.handle_payment(event)
    assert outcome.client is not None
    async with h.db.transaction() as conn:
        await set_fields(conn, outcome.client.id, ssh_key_body=KEY_A)
        await transition(conn, outcome.client.id, State.ACTIVE, reason="t", actor="system", now=NOW)
        client = await get_client(conn, outcome.client.id)
    assert client is not None
    return client


async def jobs_of(h: Harness, client_id: int) -> list[tuple[Op, JobStatus, dict[str, object]]]:
    async with h.db.read() as conn:
        return [
            (j.op, j.status, j.params) for j in await jobs_repo.list_for_client(conn, client_id)
        ]


# --- инвайты ---


async def test_invite_creates_a_pending_beta_client(h: Harness) -> None:
    code = await new_invite(h)
    await h.access.acknowledge_risks(1)
    result = await h.access.redeem_invite(1, code)
    assert result.kind is RedeemKind.OK
    client = result.client
    assert client is not None
    assert (client.plan_id, client.quota_gb, client.state) == ("beta", 10, State.PENDING)
    assert client.confirm_due_at == NOW + BETA_CONFIRM_PERIOD_S
    assert client.sub_charge_id is None
    assert await h.access.awaiting_key(1)


async def test_invite_requires_the_risk_ack(h: Harness) -> None:
    code = await new_invite(h)
    assert (await h.access.redeem_invite(1, code)).kind is RedeemKind.NEED_ACK
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 1


async def test_used_and_unknown_codes(h: Harness) -> None:
    code = await new_invite(h)
    await h.access.acknowledge_risks(1)
    await h.access.acknowledge_risks(2)
    assert (await h.access.redeem_invite(1, code)).kind is RedeemKind.OK
    assert (await h.access.redeem_invite(2, code)).kind is RedeemKind.USED
    assert (await h.access.redeem_invite(2, "AAAAA-AAAAA")).kind is RedeemKind.INVALID
    assert (await h.access.redeem_invite(2, "привет")).kind is RedeemKind.INVALID


async def test_one_beta_per_account_even_after_deletion(h: Harness) -> None:
    first, second = await new_invite(h), await new_invite(h)
    await h.access.acknowledge_risks(1)
    client = (await h.access.redeem_invite(1, first)).client
    assert client is not None
    assert (await h.access.redeem_invite(1, second)).kind is RedeemKind.HAS_CLIENT
    async with h.db.transaction() as conn:
        await transition(conn, client.id, State.DELETED, reason="t", actor="admin:1", now=NOW)
    assert (await h.access.redeem_invite(1, second)).kind is RedeemKind.ALREADY_BETA
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 1


async def test_no_beta_for_paying_clients(h: Harness) -> None:
    await paid_client(h)
    code = await new_invite(h)
    assert (await h.access.redeem_invite(1, code)).kind is RedeemKind.HAS_CLIENT


async def test_failed_invite_does_not_burn_the_code(db: Database, clock: FakeClock) -> None:
    h = await Harness.build(db, clock, node_capacity_gb=5)
    code = await new_invite(h)
    await h.access.acknowledge_risks(1)
    assert (await h.access.redeem_invite(1, code)).kind is RedeemKind.NO_CAPACITY
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 1


# --- ключ ---


async def beta_client(h: Harness, tg_id: int = 1) -> Client:
    code = await new_invite(h)
    await h.access.acknowledge_risks(tg_id)
    result = await h.access.redeem_invite(tg_id, code)
    assert result.client is not None
    return result.client


async def test_first_key_queues_add(h: Harness) -> None:
    client = await beta_client(h)
    h.clock.advance(3600)
    result = await h.access.submit_key(1, KEY_A)
    assert result.kind is KeyKind.ACCEPTED
    assert result.client is not None
    assert result.client.ssh_key_body == KEY_A
    # Бета-цикл отсчитывается с момента выдачи доступа.
    assert result.client.confirm_due_at == h.clock.now() + BETA_CONFIRM_PERIOD_S
    assert await jobs_of(h, client.id) == [(Op.ADD, JobStatus.PENDING, {"replace": False})]
    assert not await h.access.awaiting_key(1)


async def test_second_key_while_add_is_queued_only_updates_the_key(h: Harness) -> None:
    client = await beta_client(h)
    await h.access.submit_key(1, KEY_A)
    await h.access.submit_key(1, KEY_B)
    assert len(await jobs_of(h, client.id)) == 1
    stored = await h.access.live_client(1)
    assert stored is not None
    assert stored.ssh_key_body == KEY_B
    await h.provisioner.run_due()
    assert h.storectl.calls[-1][-1] == KEY_B


async def test_replacing_a_key_queues_a_replacement(h: Harness) -> None:
    client = await paid_client(h)
    result = await h.access.submit_key(1, KEY_B)
    assert result.kind is KeyKind.REPLACED
    assert await jobs_of(h, client.id) == [(Op.ADD, JobStatus.PENDING, {"replace": True})]
    assert (await h.access.submit_key(1, KEY_B)).kind is KeyKind.UNCHANGED


async def test_blocked_and_unknown_clients_cannot_set_keys(h: Harness) -> None:
    assert (await h.access.submit_key(1, KEY_A)).kind is KeyKind.NO_CLIENT
    client = await paid_client(h)
    async with h.db.transaction() as conn:
        await transition(conn, client.id, State.SUSPENDED, reason="t", actor="system", now=NOW)
    assert (await h.access.submit_key(1, KEY_B)).kind is KeyKind.BLOCKED
    assert await jobs_of(h, client.id) == []


# --- предложение тарифа ---


async def test_offer_for_a_newcomer(h: Harness) -> None:
    await h.access.acknowledge_risks(1)
    offer = await h.access.offer_plan(1, "gb100")
    assert offer.kind is OfferKind.OK
    assert not offer.replaces_subscription
    assert offer.params is not None
    parsed = parse_payload(str(offer.params["payload"]))
    assert parsed is not None
    assert (parsed.plan_id, parsed.client_id) == ("gb100", 0)
    assert offer.params["subscription_period"] == 2_592_000


async def test_offer_requires_the_risk_ack(h: Harness) -> None:
    offer = await h.access.offer_plan(1, "gb50")
    assert (offer.kind, offer.reject) == (OfferKind.REJECTED, Reject.NO_RISK_ACK)


@pytest.mark.parametrize("plan_id", ["beta", "nope", ""])
async def test_offer_rejects_unknown_plans(h: Harness, plan_id: str) -> None:
    offer = await h.access.offer_plan(1, plan_id)
    assert (offer.kind, offer.reject) == (OfferKind.REJECTED, Reject.BAD_PLAN)


async def test_offer_for_a_plan_change_binds_the_client_and_warns(h: Harness) -> None:
    client = await paid_client(h)
    offer = await h.access.offer_plan(1, "gb100")
    assert offer.kind is OfferKind.OK
    assert offer.replaces_subscription
    assert offer.params is not None
    parsed = parse_payload(str(offer.params["payload"]))
    assert parsed is not None
    assert parsed.client_id == client.id


async def test_offer_refuses_the_same_active_plan(h: Harness) -> None:
    await paid_client(h)
    offer = await h.access.offer_plan(1, "gb50")
    assert (offer.kind, offer.reject) == (OfferKind.REJECTED, Reject.ALREADY_SUBSCRIBED)


async def test_downgrade_checks_fresh_usage(h: Harness) -> None:
    client = await paid_client(h, "gb100")
    h.storectl.usage_by_login[client.login] = Usage(60 * GIB, 100 * GIB)
    offer = await h.access.offer_plan(1, "gb50")
    assert (offer.kind, offer.reject) == (OfferKind.REJECTED, Reject.TOO_MUCH_DATA)

    h.storectl.usage_by_login[client.login] = Usage(20 * GIB, 100 * GIB)
    assert (await h.access.offer_plan(1, "gb50")).kind is OfferKind.OK
    assert [call[0] for call in h.storectl.calls] == ["usage", "usage"]


async def test_downgrade_is_unavailable_when_the_node_does_not_answer(h: Harness) -> None:
    await paid_client(h, "gb100")
    h.storectl.fail("usage", StorectlError(ErrorKind.UNREACHABLE, "timeout"))
    assert (await h.access.offer_plan(1, "gb50")).kind is OfferKind.UNAVAILABLE


async def test_upgrade_does_not_touch_the_node(h: Harness) -> None:
    await paid_client(h, "gb50")
    assert (await h.access.offer_plan(1, "gb200")).kind is OfferKind.OK
    assert h.storectl.calls == []


# --- статус ---


async def test_status(h: Harness) -> None:
    assert await h.access.status(1) is None
    client = await paid_client(h)
    async with h.db.transaction() as conn:
        await record_sample(conn, client.id, NOW, 5 * GIB, 50 * GIB)
    view = await h.access.status(1)
    assert view is not None
    assert view.client.id == client.id
    assert view.plan == CATALOG.get("gb50")
    assert view.node is not None
    assert view.sample is not None
    assert view.sample.used_bytes == 5 * GIB
