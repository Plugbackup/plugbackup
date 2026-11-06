"""Платежи в админке: история, поиск, возврат Stars и его последствия для клиента."""

import json

import pytest_asyncio

from fakes import FakeClock
from harness import ADMIN, OTHER, SUPPORT, USER, Harness
from helpers import NOW, add_client, add_user, fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.db import payments as payments_repo
from plugbackup_bot.db.clients import get_client
from plugbackup_bot.db.users import touch_user
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.domain.states import State
from plugbackup_bot.services.payments_admin import MAX_REASON, RefundKind
from plugbackup_bot.services.stats import month_bounds
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb

OWNER = Actor(ADMIN.id, Role.OWNER)
HELPER = Actor(SUPPORT.id, Role.SUPPORT)
P = core.PAYMENTS
DAY = 24 * 3600
FIRST = "AAAA-first-charge-1"
SECOND = "BBBB-renewal-charge-2"
OTHERS = "CCCC-other-person-3"


async def pay(
    db: Database, tg_id: int, client_id: int | None, charge: str, stars: int, at: int, **kw: bool
) -> int:
    async with db.transaction() as conn:
        return await payments_repo.insert_payment(
            conn, tg_id=tg_id, client_id=client_id, plan_id="gb50", stars=stars,
            charge_id=charge, provider_charge_id=None, invoice_payload="gb50:1",
            is_recurring=kw.get("recurring", False), is_first=kw.get("first", False),
            sub_expires_at=None, now=at,
        )  # fmt: skip


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    await add_user(db, USER.id)
    client_id = await add_client(db, USER.id, 1, ssh_key_body=fake_key_body(0))
    async with db.transaction() as conn:
        await touch_user(conn, USER.id, NOW, "pavel")
        await conn.execute(
            "UPDATE clients SET sub_charge_id = ?, renewal_on = 1 WHERE id = ?", (SECOND, client_id)
        )
    await pay(db, USER.id, client_id, FIRST, 50, NOW - 30 * DAY, first=True)  # №1
    await pay(db, USER.id, client_id, SECOND, 50, NOW - DAY, recurring=True)  # №2
    await add_user(db, OTHER.id)
    await pay(db, OTHER.id, None, OTHERS, 170, NOW - 2 * DAY)  # №3, без клиента
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")
    return harness


def shown(h: Harness) -> list[str]:
    return [text for text, _, _ in h.last_buttons() if text != T.BTN_ADM_MENU]


async def client_state(h: Harness) -> tuple[State, bool, bool]:
    async with h.db.read() as conn:
        client = await get_client(conn, 1)
    assert client is not None
    return client.state, client.renewal_on, client.admin_hold


async def revenue(h: Harness) -> int:
    _, end = month_bounds(NOW, "Europe/Moscow")
    async with h.db.read() as conn:
        return await payments_repo.revenue_stars(conn, NOW - 90 * DAY, end)


# --- чтение и поиск ---


async def test_the_list_is_newest_first_and_searchable(h: Harness) -> None:
    rows, total = await h.payments.page(None, limit=10, offset=0)
    assert total == 3 and [r.id for r in rows] == [2, 3, 1]
    assert rows[0].login == "u1001" and rows[0].username == "pavel"

    async def ids(query: str) -> list[int]:
        return [r.id for r in (await h.payments.page(query, limit=10, offset=0))[0]]

    assert await ids(str(USER.id)) == [2, 1]  # Telegram id
    assert await ids("u1001") == [2, 1]  # логин
    assert await ids("@Pavel") == [2, 1]  # @username без учёта регистра
    assert await ids("renewal-charge") == [2]  # часть номера платежа
    assert await ids("zzzz-nothing") == []
    assert await ids("@nobody") == []
    assert await ids("abc") == []  # короче четырёх знаков номер не ищем
    assert await ids("%%%%") == []  # служебные знаки LIKE не работают как шаблон
    assert await ids("____") == []


async def test_only_the_latest_payment_is_marked_latest(h: Harness) -> None:
    first = await h.payments.get(1)
    second = await h.payments.get(2)
    other = await h.payments.get(3)
    assert first is not None and second is not None and other is not None
    assert (first.is_latest, second.is_latest) == (False, True)
    assert other.client_id is None and other.login is None
    assert await h.payments.get(99) is None


# --- возврат ---


async def test_refund_keeping_access(h: Harness) -> None:
    before = await revenue(h)
    result = await h.payments.refund(2, OWNER, revoke_access=False)
    assert result.kind is RefundKind.OK and result.payment is not None
    assert (result.payment.status, result.payment.refunded_by) == ("refunded", ADMIN.id)
    assert result.payment.refunded_at == NOW and not result.access_revoked
    assert h.stars.refunds == [(USER.id, SECOND)]
    assert await client_state(h) == (State.ACTIVE, True, False)
    assert await revenue(h) == before - 50  # возвращённое в выручку не входит
    async with h.db.read() as conn:
        [entry] = await audit_repo.page(conn, limit=5, offset=0)
    assert (entry.action, entry.target_id) == ("payment.refund", "2")
    assert entry.details == {"revoke_access": False, "stars": 50}


async def test_refund_with_access_off_cancels_renewal_and_suspends(h: Harness) -> None:
    result = await h.payments.refund(2, OWNER, revoke_access=True)
    assert result.kind is RefundKind.OK and result.access_revoked
    assert result.payment is not None and result.payment.refund_revoked
    assert await client_state(h) == (State.SUSPENDED, False, True)
    assert h.stars.calls == [(USER.id, SECOND, True)]  # автопродление выключено
    async with h.db.read() as conn:
        actions = [e.action for e in await audit_repo.page(conn, limit=10, offset=0)]
    assert "payment.refund" in actions and "client.block" in actions


async def test_an_old_payment_never_switches_access_off(h: Harness) -> None:
    result = await h.payments.refund(1, OWNER, revoke_access=True)
    assert result.kind is RefundKind.OK and not result.access_revoked
    assert await client_state(h) == (State.ACTIVE, True, False)
    assert h.stars.calls == []


async def test_a_payment_without_a_client_can_be_refunded(h: Harness) -> None:
    result = await h.payments.refund(3, OWNER, revoke_access=True)
    assert result.kind is RefundKind.OK and not result.access_revoked
    assert h.stars.refunds == [(OTHER.id, OTHERS)]


async def test_telegram_refusal_changes_nothing(h: Harness) -> None:
    h.stars.refund_error = "Bad Request: " + "причина " * 60
    result = await h.payments.refund(2, OWNER, revoke_access=True)
    assert result.kind is RefundKind.FAILED
    assert result.reason is not None and len(result.reason) <= MAX_REASON
    payment = await h.payments.get(2)
    assert payment is not None and payment.status == "paid" and payment.refunded_at is None
    assert await client_state(h) == (State.ACTIVE, True, False)
    async with h.db.read() as conn:
        assert await audit_repo.count(conn) == 0


async def test_a_payment_refunded_in_telegram_already_is_marked_here(h: Harness) -> None:
    h.stars.refund_error = "Bad Request: CHARGE_ALREADY_REFUNDED"
    result = await h.payments.refund(2, OWNER, revoke_access=False)
    assert result.kind is RefundKind.OK
    payment = await h.payments.get(2)
    assert payment is not None and payment.status == "refunded"


async def test_the_second_refund_does_not_reach_telegram(h: Harness) -> None:
    await h.payments.refund(2, OWNER, revoke_access=False)
    again = await h.payments.refund(2, OWNER, revoke_access=False)
    assert again.kind is RefundKind.ALREADY
    assert h.stars.refunds == [(USER.id, SECOND)]
    assert (await h.payments.refund(99, OWNER, revoke_access=False)).kind is RefundKind.NOT_FOUND


async def test_support_cannot_refund(h: Harness) -> None:
    result = await h.payments.refund(2, HELPER, revoke_access=False)
    assert result.kind is RefundKind.DENIED
    assert h.stars.refunds == []
    payment = await h.payments.get(2)
    assert payment is not None and payment.status == "paid"


async def test_the_journal_keeps_no_charge_ids(h: Harness) -> None:
    await h.payments.refund(2, OWNER, revoke_access=False)
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=5, offset=0)
    assert SECOND not in json.dumps([e.details for e in entries])


# --- админка ---


async def test_only_the_owner_sees_the_section(h: Harness) -> None:
    await h.say("/admin", user=ADMIN)
    assert any(data == cb(P) for _, data, _ in h.last_buttons())
    await h.say("/admin", user=SUPPORT)
    assert all(data != cb(P) for _, data, _ in h.last_buttons())
    h.session.requests.clear()
    for data in (cb(P), cb(P, "card", i=2), cb(P, "rf", i=2), cb(P, "rv", i=2), cb(P, "rk", i=2)):
        await h.press(data, user=SUPPORT)
    assert h.session.outputs() == []
    assert h.stars.refunds == []


async def test_list_search_and_card(h: Harness) -> None:
    await h.press(cb(P), user=ADMIN)
    assert "Платежей: 3" in plain(h.last().text)
    assert len(shown(h)) == 4  # поиск и три платежа
    await h.press(cb(P, "srch"), user=ADMIN)
    assert await h.fsm_state(ADMIN) == "PaymentAdminStates:search"
    await h.say("@pavel", user=ADMIN)
    assert "найдено: 2" in plain(h.last().text) and "«@pavel»" in plain(h.last().text)
    assert T.BTN_PAY_RESET in shown(h)

    await h.press(cb(P, "card", i=2), user=ADMIN)
    card = plain(h.last().text)
    assert "u1001 · @pavel" in card and "50 Stars" in card and "продление" in card
    assert "оплачен" in card and SECOND in card
    assert f"<code>{SECOND}</code>" in h.last().text  # номер копируется нажатием
    back = h.last().reply_markup.inline_keyboard[-1][0]
    assert back.callback_data == cb(P, "list", x="q")  # назад к результатам поиска

    await h.press(cb(P, "list"), user=ADMIN)  # сброс поиска
    assert "Платежей: 3" in plain(h.last().text)


async def test_an_empty_search_and_a_bad_query(h: Harness) -> None:
    await h.press(cb(P, "srch"), user=ADMIN)
    await h.say("я" * 101, user=ADMIN)
    assert "не длиннее 100" in plain(h.last().text)
    await h.say("ничегонет", user=ADMIN)
    assert T.PAY_NOTHING in plain(h.last().text)


async def test_refund_dialogue_with_access_off(h: Harness) -> None:
    await h.press(cb(P, "card", i=2), user=ADMIN)
    assert T.BTN_PAY_REFUND in shown(h)
    await h.press(cb(P, "rf", i=2), user=ADMIN)
    ask = plain(h.last().text)
    assert "Вернуть 50 Stars клиенту u1001" in ask and "отменить возврат нельзя" in ask
    assert shown(h) == [T.BTN_PAY_REFUND_REVOKE, T.BTN_PAY_REFUND_KEEP]
    keyboard = [b for row in h.last().reply_markup.inline_keyboard for b in row]
    assert {
        b.style for b in keyboard if b.callback_data in (cb(P, "rv", i=2), cb(P, "rk", i=2))
    } == {"danger"}
    assert h.stars.refunds == []  # пока только экран подтверждения

    h.messenger.sent.clear()
    await h.press(cb(P, "rv", i=2), user=ADMIN)
    text = plain(h.last().text)
    assert "Платёж №2 возвращён: 50 Stars" in text and "доступ приостановлен" in text
    assert "возвращён" in text and T.BTN_PAY_REFUND not in shown(h)
    assert await client_state(h) == (State.SUSPENDED, False, True)
    [(_, message)] = [m for m in h.messenger.sent if m[0] == USER.id][:1]
    assert "50 Stars возвращён" in plain(message) and "/support" in plain(message)


async def test_refund_dialogue_keeping_access(h: Harness) -> None:
    await h.press(cb(P, "rk", i=2), user=ADMIN)
    assert await client_state(h) == (State.ACTIVE, True, False)
    [(_, message)] = [m for m in h.messenger.sent if m[0] == USER.id]
    assert "50 Stars возвращён" in plain(message) and "приостановлен" not in plain(message)
    card = plain(h.last().text)
    assert "Статус: возвращён" in card and "Доступ сохранён" in card


async def test_an_old_payment_offers_one_button(h: Harness) -> None:
    await h.press(cb(P, "rf", i=1), user=ADMIN)
    assert shown(h) == [T.BTN_PAY_REFUND_ONLY]
    assert T.PAY_REFUND_OLD in plain(h.last().text)


async def test_telegram_failure_is_shown_to_the_admin(h: Harness) -> None:
    h.stars.refund_error = "Bad Request: not enough stars"
    await h.press(cb(P, "rv", i=2), user=ADMIN)
    text = plain(h.last().text)
    assert "Telegram не вернул платёж: Bad Request: not enough stars" in text
    assert "оплачен" in text and T.BTN_PAY_REFUND in shown(h)  # можно попробовать ещё раз
    assert not [m for m in h.messenger.sent if m[0] == USER.id]


async def test_pressing_refund_twice_returns_the_money_once(h: Harness) -> None:
    await h.press(cb(P, "rk", i=2), user=ADMIN)
    h.messenger.sent.clear()
    await h.press(cb(P, "rk", i=2), user=ADMIN)  # старая кнопка
    assert T.PAY_REFUND_ALREADY in plain(h.last().text)
    await h.press(cb(P, "rf", i=2), user=ADMIN)
    assert T.PAY_REFUND_ALREADY in plain(h.last().text)
    assert h.stars.refunds == [(USER.id, SECOND)] and h.messenger.sent == []


async def test_unknown_payment(h: Harness) -> None:
    await h.press(cb(P, "card", i=99), user=ADMIN)
    assert T.ADM_NOT_FOUND in plain(h.last().text)
    await h.press(cb(P, "rv", i=99), user=ADMIN)
    assert T.ADM_NOT_FOUND in plain(h.last().text)
