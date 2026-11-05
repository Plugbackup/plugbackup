"""Handlers оплаты на настоящем Dispatcher aiogram, без сети (FakeSession)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerPreCheckoutQuery
from aiogram.types import (
    BotSubscriptionUpdated,
    Chat,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
    Update,
    User,
)

from fakes import (
    FakeClock,
    FakeSession,
    FakeStars,
    RecordingAlerter,
    RecordingMessenger,
    make_bot,
)
from helpers import CATALOG, add_node, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db.clients import get_live_client
from plugbackup_bot.db.risk import ack_risk
from plugbackup_bot.domain.payload import make_payload
from plugbackup_bot.services.billing import BillingService
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.handlers.payments import build_router
from plugbackup_bot.tg.states import AccessStates
from plugbackup_bot.ui.screen import Ui

USER = User(id=5, is_bot=False, first_name="Тест")
RISK_VERSION = 1


@dataclass
class Env:
    db: Database
    clock: FakeClock
    bot: Bot
    session: FakeSession
    dp: Dispatcher
    storage: MemoryStorage
    stars: FakeStars
    messenger: RecordingMessenger
    alerter: RecordingAlerter
    update_id: int = 0

    async def feed(self, **fields: Any) -> None:
        self.update_id += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.update_id, **fields))

    async def pre_checkout(self, payload: str, amount: int = 50, currency: str = "XTR") -> None:
        query = PreCheckoutQuery(
            id="q1", from_user=USER, currency=currency, total_amount=amount, invoice_payload=payload
        )
        await self.feed(pre_checkout_query=query)

    async def pay(self, payload: str, charge: str = "ch-1", amount: int = 50, **over: Any) -> None:
        paid = {
            "currency": "XTR",
            "total_amount": amount,
            "invoice_payload": payload,
            "telegram_payment_charge_id": charge,
            "provider_payment_charge_id": "prov",
            "subscription_expiration_date": self.clock.now() + 2_592_000,
            "is_recurring": True,
            "is_first_recurring": True,
            **over,
        }
        message = Message(
            message_id=self.update_id + 100,
            date=datetime.now(UTC),
            chat=Chat(id=USER.id, type="private"),
            from_user=USER,
            successful_payment=SuccessfulPayment(**paid),
        )
        await self.feed(message=message)

    async def fsm_state(self) -> str | None:
        key = StorageKey(bot_id=self.bot.id, chat_id=USER.id, user_id=USER.id)
        return await self.storage.get_state(key)

    def answers(self) -> list[AnswerPreCheckoutQuery]:
        return self.session.of(AnswerPreCheckoutQuery)


@pytest_asyncio.fixture
async def env(db: Database, clock: FakeClock) -> Env:
    await add_node(db, capacity_gb=1000)
    bot, session = make_bot()
    stars, messenger, alerter = FakeStars(), RecordingMessenger(), RecordingAlerter()
    billing = BillingService(db, clock, CATALOG, stars, RISK_VERSION)
    storage = MemoryStorage()
    dp = Dispatcher(
        storage=storage,
        billing=billing,
        alerter=alerter,
        messenger=messenger,
        default_tz="Europe/Moscow",
        ui=Ui(),
    )
    dp.include_router(build_router())
    async with db.transaction() as conn:
        await ack_risk(conn, USER.id, RISK_VERSION, clock.now())
    return Env(db, clock, bot, session, dp, storage, stars, messenger, alerter)


async def test_pre_checkout_is_approved(env: Env) -> None:
    await env.pre_checkout(make_payload("gb50"))
    assert [(a.ok, a.error_message) for a in env.answers()] == [(True, None)]


async def test_pre_checkout_rejection_carries_a_readable_reason(env: Env) -> None:
    await env.pre_checkout(make_payload("gb50"), amount=999)
    [answer] = env.answers()
    assert answer.ok is False
    assert answer.error_message == texts.REJECTIONS["price_changed"]  # type: ignore[index]


async def test_pre_checkout_failure_is_answered_not_dropped(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(BillingService, "check_pre_checkout", broken)
    await env.pre_checkout(make_payload("gb50"))
    [answer] = env.answers()
    assert (answer.ok, answer.error_message) == (False, T.TRY_LATER)


async def test_successful_payment_creates_a_client_and_asks_for_the_key(env: Env) -> None:
    await env.pay(make_payload("gb50"))

    async with env.db.read() as conn:
        client = await get_live_client(conn, USER.id)
    assert client is not None
    assert client.plan_id == "gb50"
    card, key_help = env.session.texts()  # карточка об оплате и отдельно подсказка про ключ
    assert "Оплата получена" in plain(card)
    assert "ssh-keygen -t ed25519" in key_help
    assert await env.fsm_state() == AccessStates.waiting_key.state
    assert env.alerter.alerts == []


async def test_redelivered_payment_reasks_for_the_key_without_a_second_client(env: Env) -> None:
    payload = make_payload("gb50")
    await env.pay(payload)
    await env.pay(payload)

    async with env.db.read() as conn, conn.execute("SELECT COUNT(*) FROM clients") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1
    assert len(env.session.texts()) == 3  # карточка, подсказка и повторная подсказка
    assert env.session.texts()[2] == texts.KEY_HELP


async def test_unknown_invoice_alerts_the_admin_and_tells_the_client(env: Env) -> None:
    await env.pay("something-else")
    assert env.session.texts() == [T.PAYMENT_MANUAL]
    [alert] = env.alerter.alerts
    assert "ch-1" in alert
    assert await env.fsm_state() is None


async def test_plan_change_cancels_the_previous_subscription(env: Env) -> None:
    await env.pay(make_payload("gb50"), charge="ch-old")
    async with env.db.read() as conn:
        client = await get_live_client(conn, USER.id)
    assert client is not None
    await env.pay(make_payload("gb100", client.id), charge="ch-new", amount=100)

    assert env.stars.calls == [(USER.id, "ch-old", True)]
    assert env.alerter.alerts == []
    changed = plain(env.session.texts()[-2])  # дальше идёт подсказка про ключ
    assert "Тариф изменён" in changed
    assert "Тариф: 100 ГБ" in changed


async def test_failed_cancellation_of_the_old_subscription_alerts_the_admin(env: Env) -> None:
    await env.pay(make_payload("gb50"), charge="ch-old")
    async with env.db.read() as conn:
        client = await get_live_client(conn, USER.id)
    assert client is not None
    env.stars.fail = True
    await env.pay(make_payload("gb100", client.id), charge="ch-new", amount=100)
    [alert] = env.alerter.alerts
    assert "двойное списание" in alert
    assert "ch-old" in alert


async def test_renewal_is_confirmed_in_one_line(env: Env) -> None:
    payload = make_payload("gb50")
    await env.pay(payload)
    env.session.requests.clear()
    await env.pay(payload, charge="ch-r", is_first_recurring=None)
    [reply] = env.session.texts()
    assert "Продление принято" in plain(reply)
    assert "Доступ действует до" in plain(reply)


async def test_subscription_events_reach_the_client(env: Env) -> None:
    payload = make_payload("gb50")
    await env.pay(payload)

    await env.feed(
        subscription=BotSubscriptionUpdated(user=USER, invoice_payload=payload, state="canceled")
    )
    await env.feed(
        subscription=BotSubscriptionUpdated(user=USER, invoice_payload=payload, state="active")
    )
    await env.feed(
        subscription=BotSubscriptionUpdated(user=USER, invoice_payload=payload, state="failed")
    )

    sent = [text for _, text in env.messenger.sent]
    assert sent[0].startswith("Автопродление отключено")
    assert sent[1] == T.RENEWAL_ENABLED
    assert sent[2].startswith("Не удалось списать оплату")
    [alert] = env.alerter.alerts
    assert alert.startswith("Сбой списания подписки")


async def test_foreign_subscription_events_are_silent(env: Env) -> None:
    await env.pay(make_payload("gb50"))
    await env.feed(
        subscription=BotSubscriptionUpdated(
            user=USER, invoice_payload=make_payload("gb50"), state="canceled"
        )
    )
    assert env.messenger.sent == []
