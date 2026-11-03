import pytest_asyncio

from fakes import FakeClock
from harness import OTHER, USER, Harness, StepClock
from helpers import CATALOG
from plugbackup_bot.db import Database
from plugbackup_bot.domain.payload import make_payload
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.middlewares import AttemptLimiter


class ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest_asyncio.fixture
async def frozen(db: Database, clock: FakeClock) -> tuple[Harness, ManualClock]:
    mono = ManualClock()
    return await Harness.build(db, clock, monotonic=mono), mono


async def test_burst_then_throttle_with_a_single_notice(
    frozen: tuple[Harness, ManualClock],
) -> None:
    h, mono = frozen
    for _ in range(6):
        await h.say("/help")
    assert h.texts().count(texts.HELP) == 6

    await h.say("/help")
    await h.say("/help")
    assert h.texts().count(texts.HELP) == 6
    assert h.texts().count(texts.THROTTLED) == 1  # предупреждение не чаще раза в 10 секунд

    mono.now += 11
    await h.say("/help")
    assert h.texts().count(texts.HELP) == 7  # токены восстановились


async def test_buttons_are_throttled_and_answered(frozen: tuple[Harness, ManualClock]) -> None:
    h, _ = frozen
    for _ in range(6):
        await h.press("menu:how")
    await h.press("menu:how")
    answers = h.callback_answers()
    assert len(answers) == 7  # крутилка на кнопке гаснет и при отказе
    assert answers[-1].text == texts.THROTTLED
    assert h.texts().count(texts.HOW_IT_WORKS) == 6


async def test_users_do_not_share_limits(frozen: tuple[Harness, ManualClock]) -> None:
    h, _ = frozen
    for _ in range(8):
        await h.say("/help")
    before = len(h.texts())
    await h.say("/help", user=OTHER)
    assert len(h.texts()) == before + 1
    assert h.last().chat_id == OTHER.id


async def test_payments_are_never_throttled(frozen: tuple[Harness, ManualClock]) -> None:
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, SuccessfulPayment

    h, _ = frozen
    for _ in range(10):
        await h.say("/help")
    await h.access.acknowledge_risks(USER.id)
    payment = SuccessfulPayment(
        currency="XTR",
        total_amount=CATALOG.get("gb50").stars,
        invoice_payload=make_payload("gb50"),
        telegram_payment_charge_id="ch-1",
        provider_payment_charge_id="p",
        subscription_expiration_date=h.clock.now() + 2_592_000,
        is_recurring=True,
        is_first_recurring=True,
    )
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=USER.id, type="private"),
        from_user=USER,
        successful_payment=payment,
    )
    await h._feed(message=message)
    assert await h.access.live_client(USER.id) is not None
    assert h.last().text.startswith("Оплата получена")


async def test_group_chats_are_ignored(frozen: tuple[Harness, ManualClock]) -> None:
    h, _ = frozen
    await h.say("/start", chat_type="group")
    assert h.texts() == []


async def test_writing_to_the_bot_clears_the_blocked_flag(db: Database, clock: FakeClock) -> None:
    h = await Harness.build(db, clock, monotonic=StepClock())
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO users (tg_id, first_seen_at, bot_blocked) VALUES (5, 1, 1)")
    await h.say("/help")
    async with (
        db.read() as conn,
        conn.execute("SELECT bot_blocked FROM users WHERE tg_id = 5") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


def test_attempt_limiter() -> None:
    now = [0.0]
    limiter = AttemptLimiter(max_failures=3, window_s=100, now=lambda: now[0])
    for _ in range(2):
        limiter.fail(1)
    assert not limiter.blocked(1)
    limiter.fail(1)
    assert limiter.blocked(1)
    assert not limiter.blocked(2)
    now[0] = 101  # окно прошло
    assert not limiter.blocked(1)
    limiter.fail(1)
    limiter.reset(1)
    assert not limiter.blocked(1)
