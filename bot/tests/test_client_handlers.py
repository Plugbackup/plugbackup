"""Сценарии клиента через настоящий Dispatcher: от /start до выданного доступа."""

import pytest
import pytest_asyncio
from aiogram.methods import CreateInvoiceLink
from aiogram.types import PreCheckoutQuery

from fakes import FakeClock
from harness import OTHER, USER, Harness
from helpers import NOW, fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db.clients import get_live_client, set_fields
from plugbackup_bot.db.usage import record_sample
from plugbackup_bot.domain.plans import GIB
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.storectl import ErrorKind, StorectlError, Usage
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.states import AccessStates

KEY_LINE = f"ssh-ed25519 {fake_key_body(0)} me@home"
KEY_BODY = KEY_LINE.split()[1]
# Заголовок собран из частей, чтобы в исходниках не было похожей на ключ строки.
PRIVATE_KEY_SAMPLE = (
    "-----BEGIN "
    + "OPENSSH PRIVATE KEY-----"
    + chr(10)
    + "abc"
    + chr(10)
    + "-----END "
    + "OPENSSH PRIVATE KEY-----"
)
OTHER_KEY = fake_key_body(1)


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    return await Harness.build(db, clock)


async def start_beta(h: Harness) -> None:
    """Дойти до экрана «пришлите ключ» по инвайту."""
    code = await h.invite()
    await h.press(keyboards.MENU_BETA)
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")
    await h.say(code)


async def active_beta(h: Harness) -> None:
    await start_beta(h)
    await h.say(KEY_LINE)
    await h.provisioner.run_due()


# --- меню и справка ---


async def test_start_shows_the_menu(h: Harness) -> None:
    await h.say("/start")
    assert h.last().text == texts.start(False, 10)
    assert [text for text, _, _ in h.last_buttons()] == [
        T.BTN_TARIFFS,
        T.BTN_BETA,
        T.BTN_HOW,
        T.BTN_SUPPORT,
    ]
    assert "бета" in h.last().text.lower()


async def test_start_for_a_client_adds_the_status_button(h: Harness) -> None:
    await active_beta(h)
    await h.say("/start")
    assert "«Статус»" in h.last().text
    assert h.last_buttons()[0][1] == keyboards.MENU_STATUS


async def test_help_rules_and_how_it_works(h: Harness) -> None:
    await h.say("/help")
    await h.say("/rules")
    await h.press(keyboards.MENU_HOW)
    help_text, rules_text, how_text = h.texts()
    assert "/status" in help_text
    assert "7 дней" in plain(rules_text)
    assert "Пароль шифрования у нас не хранится" in how_text


async def test_tariffs_list_prices_and_buttons(h: Harness) -> None:
    await h.press(keyboards.MENU_TARIFFS)
    text = h.last().text
    for name, rub, stars in (("50 ГБ", 79, 50), ("100 ГБ", 149, 100), ("200 ГБ", 249, 170)):
        assert f"{name} — {rub} ₽/мес, {stars} Stars" in plain(text)
    assert [data for _, data, _ in h.last_buttons()] == ["plan:gb50", "plan:gb100", "plan:gb200"]


async def test_unknown_text_gets_a_hint(h: Harness) -> None:
    await h.say("привет")
    assert h.texts() == [T.NOT_UNDERSTOOD]


# --- бета по инвайту ---


async def test_beta_flow_from_invite_to_access(h: Harness) -> None:
    code = await h.invite()
    await h.press(keyboards.MENU_BETA)
    assert h.last().text == texts.RISK_TEXT
    assert h.last_buttons() == [(T.BTN_RISK_OK, "risk:beta", None)]

    await h.press("risk:beta")
    assert h.last().text == texts.beta_ask_code()
    assert await h.fsm_state() == AccessStates.waiting_invite.state

    await h.say("WRONG-CODE1")
    assert h.last().text == texts.INVITE_ERRORS["invalid"]  # type: ignore[index]

    await h.say(code)
    assert "Код принят" in plain(h.last().text)
    assert "Бета: 10 ГБ" in plain(h.last().text)
    assert "ssh-keygen -t ed25519" in h.last().text
    assert await h.fsm_state() == AccessStates.waiting_key.state

    await h.say(KEY_LINE)
    assert "Ключ принят" in plain(h.last().text)
    assert "Отпечаток" in plain(h.last().text)
    assert h.kicks == [1]
    assert await h.fsm_state() is None

    await h.provisioner.run_due()
    ready = h.messenger.sent[-1][1]
    assert h.messenger.sent[-1][0] == USER.id
    assert "Доступ готов" in plain(ready)
    assert h.messenger.effects[-1] is True  # конфетти на выдаче доступа
    assert "node1.example.org" in ready
    assert "u1001" in ready
    assert "sftp:plugbackup:/data/restic init" in ready
    assert h.storectl.calls == [("add", "192.0.2.10", "u1001", 10, KEY_BODY)]

    async with h.db.read() as conn:
        client = await get_live_client(conn, USER.id)
        assert client is not None
        assert client.state is State.ACTIVE
        async with conn.execute("SELECT text_version FROM risk_acks WHERE tg_id = 5") as cur:
            assert [row[0] for row in await cur.fetchall()] == [texts.RISK_TEXT_VERSION]


async def test_used_code_and_second_beta(h: Harness) -> None:
    code = await h.invite()
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}", user=OTHER)
    await h.say(code, user=OTHER)
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")
    await h.say(code)
    assert h.last().chat_id == USER.id
    assert h.last().text == texts.INVITE_ERRORS["used"]  # type: ignore[index]

    await active_beta(h)
    await h.press(keyboards.MENU_BETA)
    assert h.last().text == texts.INVITE_ERRORS["has_client"]  # type: ignore[index]


async def test_wrong_codes_are_limited(h: Harness) -> None:
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")
    for _ in range(5):
        await h.say("AAAAA-AAAAA")
    await h.say(await h.invite())  # даже верный код после лимита не принимается
    assert h.last().text == T.TOO_MANY_CODES
    assert await h.access.live_client(USER.id) is None


async def test_commands_work_while_waiting_for_a_code(h: Harness) -> None:
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")
    await h.say("/help")
    assert h.last().text == texts.HELP


# --- ключ ---


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("   ", "empty"),
        ("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ me", "not_ed25519"),
        ("hello world", "bad_format"),
        ("A" * 700, "too_long"),
    ],
)
async def test_bad_keys_are_explained(h: Harness, text: str, problem: str) -> None:
    await start_beta(h)
    await h.say(text)
    from plugbackup_bot.domain.keys import KeyProblem

    assert h.last().text == texts.KEY_PROBLEMS[KeyProblem(problem)]
    assert await h.fsm_state() == AccessStates.waiting_key.state
    assert h.kicks == []


async def test_private_key_is_deleted_and_not_stored(h: Harness) -> None:
    await start_beta(h)
    await h.say(PRIVATE_KEY_SAMPLE)
    assert h.last().text.startswith("Это приватный ключ")
    assert len(h.deleted()) == 1
    client = await h.access.live_client(USER.id)
    assert client is not None
    assert client.ssh_key_body is None


async def test_key_is_accepted_without_fsm_state_after_a_restart(h: Harness) -> None:
    await start_beta(h)
    h.storage.storage.clear()  # перезапуск бота: состояния в памяти потеряны
    await h.say(KEY_LINE)
    assert "Ключ принят" in plain(h.last().text)
    assert h.kicks == [1]


async def test_replacing_the_key(h: Harness) -> None:
    await active_beta(h)
    await h.say("/key")
    assert h.last().text == texts.KEY_REPLACE_HELP
    await h.say(KEY_BODY)
    assert h.last().text == T.KEY_UNCHANGED

    await h.say("/key")
    await h.say(OTHER_KEY)
    assert "Ключ принят" in plain(h.last().text)
    await h.provisioner.run_due()
    assert h.messenger.sent[-1][1] == T.KEY_CHANGED_NOTICE
    assert h.storectl.calls[-1][-1] == OTHER_KEY


async def test_key_command_without_storage(h: Harness) -> None:
    await h.say("/key")
    assert h.last().text == T.KEY_NO_CLIENT


async def test_failed_provisioning_tells_the_client_and_the_admin(h: Harness) -> None:
    h.storectl.fail("add", StorectlError(ErrorKind.UNREACHABLE, "ssh: timeout", exit_code=255))
    await start_beta(h)
    await h.say(KEY_LINE)
    await h.provisioner.run_due()
    assert h.messenger.sent[-1][1] == T.ACCESS_DELAYED
    assert "ssh: timeout" in h.alerter.alerts[0]
    assert "u1001" in h.alerter.alerts[0]

    h.clock.advance(60)
    await h.provisioner.run_due()
    assert "Доступ готов" in plain(h.messenger.sent[-1][1])


# --- покупка тарифа: от выбора до выданного доступа ---


async def test_purchase_end_to_end(h: Harness) -> None:
    await h.press("plan:gb50")
    assert h.last().text == texts.RISK_TEXT
    assert h.last_buttons() == [(T.BTN_RISK_OK, "risk:gb50", None)]

    await h.press("risk:gb50")
    [invoice] = h.session.of(CreateInvoiceLink)
    assert invoice.subscription_period == 2_592_000
    assert invoice.currency == "XTR"
    assert invoice.prices[0].amount == 50
    [(label, _, url)] = h.last_buttons()
    assert label.replace("\u00a0", " ") == "Оплатить 50 Stars"
    assert url == "https://t.me/$fake-invoice"
    assert "Пересчёта" not in h.last().text

    # Telegram проверяет оплату, затем присылает платёж.
    await h.dp.feed_update(
        h.bot,
        _update(
            1,
            pre_checkout_query=PreCheckoutQuery(
                id="q",
                from_user=USER,
                currency="XTR",
                total_amount=50,
                invoice_payload=invoice.payload,
            ),
        ),
    )
    from aiogram.methods import AnswerPreCheckoutQuery

    assert h.session.of(AnswerPreCheckoutQuery)[-1].ok is True

    await _pay(h, invoice.payload)
    paid, key_help = h.texts()[-2:]  # карточка об оплате и отдельно подсказка про ключ
    assert "Оплата получена" in plain(paid)
    assert "Тариф: 50 ГБ" in plain(paid)
    assert "ssh-keygen" in key_help
    await h.say(KEY_LINE)
    await h.provisioner.run_due()
    assert "Доступ готов" in plain(h.messenger.sent[-1][1])
    assert h.storectl.calls == [("add", "192.0.2.10", "u1001", 50, KEY_BODY)]


async def test_pre_checkout_is_refused_without_the_risk_screen(h: Harness) -> None:
    from aiogram.methods import AnswerPreCheckoutQuery

    from plugbackup_bot.domain.payload import make_payload

    await h.dp.feed_update(
        h.bot,
        _update(
            2,
            pre_checkout_query=PreCheckoutQuery(
                id="q",
                from_user=USER,
                currency="XTR",
                total_amount=50,
                invoice_payload=make_payload("gb50"),
            ),
        ),
    )
    answer = h.session.of(AnswerPreCheckoutQuery)[-1]
    assert (answer.ok, answer.error_message) == (False, texts.REJECTIONS["no_risk_ack"])  # type: ignore[index]


async def paid_client(h: Harness, plan: str = "gb50") -> None:
    await h.press(f"risk:{plan}")
    invoice = h.session.of(CreateInvoiceLink)[-1]
    await _pay(h, invoice.payload, amount=invoice.prices[0].amount)
    await h.say(KEY_LINE)
    await h.provisioner.run_due()


async def test_upgrade_lists_other_plans_and_warns_about_replacement(h: Harness) -> None:
    await paid_client(h, "gb50")
    await h.say("/upgrade")
    assert [data for _, data, _ in h.last_buttons()] == ["plan:gb100", "plan:gb200"]
    assert "Сейчас у вас 50 ГБ" in h.last().text

    await h.press("risk:gb100")
    assert "Пересчёта за неиспользованные дни нет" in h.last().text
    assert h.last_buttons()[0][2] is not None


async def test_same_plan_is_refused_with_a_reason(h: Harness) -> None:
    await paid_client(h, "gb50")
    await h.press("risk:gb50")
    assert h.last().text == texts.REJECTIONS["already_subscribed"]  # type: ignore[index]


async def test_downgrade_with_too_much_data_is_refused(h: Harness) -> None:
    await paid_client(h, "gb100")
    h.storectl.usage_by_login["u1001"] = Usage(70 * GIB, 100 * GIB)
    await h.press("risk:gb50")
    assert h.last().text == texts.REJECTIONS["too_much_data"]  # type: ignore[index]


async def test_downgrade_with_unreachable_node_asks_to_retry(h: Harness) -> None:
    await paid_client(h, "gb100")
    h.storectl.fail("usage", StorectlError(ErrorKind.UNREACHABLE, "timeout"))
    await h.press("risk:gb50")
    assert h.last().text == T.OFFER_UNAVAILABLE


# --- /status и продление ---


async def test_status_without_storage(h: Harness) -> None:
    await h.say("/status")
    assert h.last().text == T.NO_STORAGE


async def test_status_of_a_beta_client_has_no_renewal_button(h: Harness) -> None:
    await active_beta(h)
    client = await h.access.live_client(USER.id)
    assert client is not None
    async with h.db.transaction() as conn:
        await record_sample(conn, client.id, NOW, 2 * GIB, 10 * GIB)
    await h.say("/status")
    text = h.last().text
    assert "Тариф: Бета" in text
    assert "Состояние: активно" in text
    assert "▰▰▱▱▱▱▱▱▱▱ 20%" in text
    assert "2,0 ГБ из 10 ГБ" in plain(text)
    assert "Я ещё пользуюсь" in text
    assert "u1001" in text
    assert not [data for _, data, _ in h.last_buttons() if (data or "").startswith("renew:")]


async def test_status_of_a_paid_client_and_renewal_toggle(h: Harness) -> None:
    await paid_client(h, "gb100")
    await h.say("/status")
    assert "Следующее списание:" in h.last().text
    assert (T.BTN_RENEWAL_OFF, "renew:off", None) in h.last_buttons()
    assert "renew:on" not in [data for _, data, _ in h.last_buttons()]

    await h.press("renew:off")
    assert h.stars.calls[-1][2] is True
    client = await h.access.live_client(USER.id)
    assert client is not None
    assert client.renewal_on is False

    await h.say("/status")
    assert "продление отключено" in h.last().text
    assert (T.BTN_RENEWAL_ON, "renew:on", None) in h.last_buttons()
    assert "renew:off" not in [data for _, data, _ in h.last_buttons()]

    await h.press("renew:on")
    assert h.stars.calls[-1][2] is False


async def test_renewal_toggle_failure_is_reported(h: Harness) -> None:
    await paid_client(h, "gb100")
    h.stars.fail = True
    await h.press("renew:off")
    answer = h.callback_answers()[-1]
    assert (answer.text, answer.show_alert) == (T.RENEWAL_CHANGE_FAILED, True)
    client = await h.access.live_client(USER.id)
    assert client is not None
    assert client.renewal_on is True


async def test_status_of_a_suspended_client(h: Harness) -> None:
    await active_beta(h)
    client = await h.access.live_client(USER.id)
    assert client is not None
    async with h.db.transaction() as conn:
        from plugbackup_bot.db.clients import transition

        await transition(conn, client.id, State.SUSPENDED, reason="t", actor="system", now=NOW)
        await set_fields(conn, client.id, confirm_due_at=None, expires_at=NOW + 86400)
    await h.say("/status")
    assert "заблокировано" in h.last().text


# --- вспомогательное ---


def _update(update_id: int, **fields):  # type: ignore[no-untyped-def]
    from aiogram.types import Update

    return Update(update_id=update_id, **fields)


async def _pay(h: Harness, payload: str, amount: int = 50) -> None:
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, SuccessfulPayment

    h._update_id += 1
    charge = f"charge-{h._update_id}"
    message = Message(
        message_id=h._update_id + 500,
        date=datetime.now(UTC),
        chat=Chat(id=USER.id, type="private"),
        from_user=USER,
        successful_payment=SuccessfulPayment(
            currency="XTR",
            total_amount=amount,
            invoice_payload=payload,
            telegram_payment_charge_id=charge,
            provider_payment_charge_id="prov",
            subscription_expiration_date=h.clock.now() + 2_592_000,
            is_recurring=True,
            is_first_recurring=True,
        ),
    )
    await h._feed(message=message)
