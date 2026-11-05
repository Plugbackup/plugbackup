"""Оформление клиентских экранов: баннеры, «одно окно», навигация, цвет кнопок, копирование."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio
from aiogram.methods import (
    CreateInvoiceLink,
    DeleteMessage,
    EditMessageMedia,
    EditMessageText,
    SendMessage,
    SendPhoto,
)
from aiogram.types import Chat, Message, SuccessfulPayment

from fakes import FakeClock
from harness import USER, Harness
from helpers import CATALOG, fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.domain.plans import GIB
from plugbackup_bot.emoji import E
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.emoji_fallback import EmojiFallback
from plugbackup_bot.ui import banners, buttons
from plugbackup_bot.ui.effects import EFFECT_CONFETTI

DESIGN = Path(__file__).resolve().parents[2] / "design" / "graphics" / "png"
KEY_LINE = f"ssh-ed25519 {fake_key_body(0)} me@home"
MAX_CAPTION = 1024


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    return await Harness.build(db, clock, assets_dir=DESIGN)


def markup_rows(h: Harness):  # type: ignore[no-untyped-def]
    markup = h.last().reply_markup
    assert markup is not None
    return markup.inline_keyboard


def _bare(text: str) -> str:
    return text.split(" ", 1)[1] if text and not text[0].isalnum() and " " in text else text


def nav_of(h: Harness) -> list[tuple[str, str]]:
    """Навигационные кнопки последнего экрана: (подпись, куда ведут)."""
    return [
        (_bare(button.text), button.callback_data)
        for row in markup_rows(h)
        for button in row
        if button.callback_data == buttons.HOME_DATA or _bare(button.text) == T.BTN_BACK
    ]


async def active_beta(h: Harness) -> None:
    code = await h.invite()
    await h.press(keyboards.MENU_BETA)
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")
    await h.say(code)
    await h.say(KEY_LINE)
    await h.provisioner.run_due()


# --- баннеры и «одно окно» ---


async def test_start_shows_the_welcome_banner_with_a_short_caption(h: Harness) -> None:
    await h.say("/start")
    [photo] = h.session.of(SendPhoto)
    assert photo.caption is not None and len(photo.caption) <= MAX_CAPTION
    assert "PlugBackup" in photo.caption
    assert h.last().photo is True
    assert not photo.message_effect_id  # эффект только на оплате и выдаче доступа


async def test_the_banner_is_uploaded_once_then_sent_by_file_id(h: Harness) -> None:
    await h.say("/start")
    await h.say("/start")
    first, second = h.session.of(SendPhoto)
    assert not isinstance(first.photo, str)
    assert isinstance(second.photo, str) and second.photo.startswith("fid-")


async def test_moving_between_banner_screens_edits_the_media(h: Harness) -> None:
    await h.say("/start")
    await h.press(keyboards.MENU_TARIFFS)
    [edit] = h.session.of(EditMessageMedia)
    assert "Тарифы" in edit.media.caption
    assert not h.session.of(DeleteMessage)
    assert len(h.session.of(SendPhoto)) == 1  # новое окно не открывалось


async def test_a_text_screen_replaces_the_banner_window_and_back_restores_it(h: Harness) -> None:
    await h.say("/start")
    await h.press(keyboards.MENU_HOW)
    assert len(h.session.of(SendMessage)) == 1 and len(h.deleted()) == 1  # фото заменено текстом
    assert "Как это работает" in plain(h.last().text)

    await h.press(keyboards.MENU_HOME)
    assert len(h.session.of(SendPhoto)) == 2 and len(h.deleted()) == 2
    assert h.last().photo is True


async def test_text_screens_edit_in_place(h: Harness) -> None:
    await h.press("plan:gb50")  # «сообщение» с кнопкой текстовое
    assert not h.session.of(SendMessage) and len(h.session.of(EditMessageText)) == 1


async def test_every_screen_with_a_banner_fits_the_caption(h: Harness) -> None:
    client = None
    many = tuple(replace(CATALOG.paid[0], id=f"p{i}", gb=10 + i) for i in range(8))
    samples = [
        texts.start(False, 10),
        texts.start(True, 10),
        texts.tariffs(CATALOG.paid, client),
        texts.tariffs(many, client),
        texts.paid_new(CATALOG.paid[0], "2 декабря 2026"),
        texts.plan_changed(CATALOG.paid[1], "2 декабря 2026", resumed=True, ask_key=True),
    ]
    for text in samples:
        assert len(text) <= MAX_CAPTION, text[:40]


# --- навигация ---


async def test_first_level_screens_end_with_the_main_menu(h: Harness) -> None:
    for data in (keyboards.MENU_TARIFFS, keyboards.MENU_HOW):
        await h.press(data)
        assert [label for label, _ in nav_of(h)] == [T.BTN_HOME], data


async def test_deeper_screens_have_back_and_the_main_menu(h: Harness) -> None:
    await h.press("plan:gb50")
    assert nav_of(h) == [(T.BTN_BACK, keyboards.MENU_TARIFFS), (T.BTN_HOME, buttons.HOME_DATA)]

    await h.press("risk:gb50")  # экран оплаты
    assert nav_of(h) == [(T.BTN_BACK, keyboards.MENU_TARIFFS), (T.BTN_HOME, buttons.HOME_DATA)]


async def test_the_beta_risk_screen_goes_back_to_the_menu_only(h: Harness) -> None:
    await h.press(keyboards.MENU_BETA)
    assert nav_of(h) == [(T.BTN_HOME, buttons.HOME_DATA)]


async def test_status_alerts_and_restore_chain_back_correctly(h: Harness) -> None:
    await active_beta(h)
    await h.press(keyboards.MENU_STATUS)
    assert nav_of(h) == [(T.BTN_HOME, buttons.HOME_DATA)]

    await h.press(keyboards.ALERTS_HOME)
    assert nav_of(h)[0] == (T.BTN_BACK, keyboards.MENU_STATUS)
    await h.press("al:q")
    assert nav_of(h)[0] == (T.BTN_BACK, keyboards.ALERTS_HOME)

    await h.press(keyboards.RESTORE_START)
    assert nav_of(h)[0] == (T.BTN_BACK, keyboards.MENU_STATUS)
    date = h.restore.available_dates()[0]
    await h.press(f"restore:d:{date}")
    assert nav_of(h)[0] == (T.BTN_BACK, keyboards.RESTORE_START)


async def test_menu_buttons_clear_an_unfinished_scenario(h: Harness) -> None:
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")  # ждём инвайт-код
    assert await h.fsm_state() is not None
    await h.press(keyboards.MENU_HOME)
    assert await h.fsm_state() is None


# --- цвет кнопок ---


def style_of(h: Harness, label: str) -> str | None:
    for row in markup_rows(h):
        for button in row:
            if _bare(button.text) == label:
                return button.style
    raise AssertionError(f"нет кнопки {label!r}")


async def test_the_main_menu_has_one_blue_button(h: Harness) -> None:
    await h.say("/start")
    styles = [b.style for row in markup_rows(h) for b in row]
    assert styles.count("primary") == 1 and style_of(h, T.BTN_TARIFFS) == "primary"

    await active_beta(h)
    await h.say("/start")
    assert style_of(h, T.BTN_STATUS) == "primary"
    assert [b.style for row in markup_rows(h) for b in row].count("primary") == 1


async def test_confirmations_are_green_and_refusals_are_red(h: Harness) -> None:
    await h.press("plan:gb50")
    assert style_of(h, T.BTN_RISK_OK) == "success"

    await h.press("risk:gb50")
    pay = next(b for row in markup_rows(h) for b in row if b.url)
    assert pay.style == "success"

    await active_beta(h)
    date = h.restore.available_dates()[0]
    await h.press(f"restore:d:{date}")
    assert style_of(h, T.BTN_RESTORE_SEND) == "success"
    assert style_of(h, T.BTN_CANCEL) == "danger"


async def test_renewal_toggle_colors(h: Harness) -> None:
    await h.press("risk:gb100")
    invoice = h.session.of(CreateInvoiceLink)[-1]
    await pay(h, invoice.payload, invoice.prices[0].amount)
    await h.say(KEY_LINE)
    await h.say("/status")
    assert style_of(h, T.BTN_RENEWAL_OFF) == "danger"
    await h.press("renew:off")  # экран статуса обновился на месте
    assert style_of(h, T.BTN_RENEWAL_ON) == "success"
    assert h.last().edited is True


# --- копирование ---


async def test_access_ready_has_copy_buttons_and_the_confetti(h: Harness) -> None:
    await active_beta(h)
    markup = h.messenger.markups[-1]
    assert markup is not None
    copies = [
        b.copy_text.text for row in markup.inline_keyboard for b in row if b.copy_text is not None
    ]
    init, backup = texts.restic_commands()
    assert copies == ["u1001", "node1.example.org", init, backup]
    assert all(
        b.callback_data is None for row in markup.inline_keyboard for b in row if b.copy_text
    )
    assert h.messenger.effects[-1] is True
    assert "Доступ готов" in plain(h.messenger.sent[-1][1])


async def test_status_has_copy_buttons_for_the_login_and_the_host(h: Harness) -> None:
    await active_beta(h)
    await h.say("/status")
    copies = [b.copy_text.text for row in markup_rows(h) for b in row if b.copy_text is not None]
    assert copies == ["u1001", "node1.example.org"]


async def test_key_help_buttons_copy_the_commands(h: Harness) -> None:
    code = await h.invite()
    await h.press(keyboards.MENU_BETA)
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")
    await h.say(code)
    copies = [b.copy_text.text for row in markup_rows(h) for b in row if b.copy_text is not None]
    assert copies == [T.KEY_COMMAND_UNIX, T.KEY_COMMAND_WINDOWS]
    assert T.KEY_COMMAND_UNIX in T.KEY_HOWTO  # команда на кнопке совпадает с текстом


# --- индикатор квоты ---


async def test_status_shows_the_quota_bar(h: Harness) -> None:
    from plugbackup_bot.db.usage import record_sample

    await active_beta(h)
    client = await h.access.live_client(USER.id)
    assert client is not None
    async with h.db.transaction() as conn:
        await record_sample(conn, client.id, h.clock.now(), 5 * GIB, 10 * GIB)
    await h.say("/status")
    assert "<code>▰▰▰▰▰▱▱▱▱▱ 50%</code>" in h.last().text


# --- оплата: баннер, эффект и отдельная подсказка про ключ ---


async def pay(h: Harness, payload: str, amount: int = 50) -> None:
    h._update_id += 1
    message = Message(
        message_id=h._update_id + 500,
        date=datetime.now(UTC),
        chat=Chat(id=USER.id, type="private"),
        from_user=USER,
        successful_payment=SuccessfulPayment(
            currency="XTR",
            total_amount=amount,
            invoice_payload=payload,
            telegram_payment_charge_id=f"charge-{h._update_id}",
            provider_payment_charge_id="prov",
            subscription_expiration_date=h.clock.now() + 2_592_000,
            is_recurring=True,
            is_first_recurring=True,
        ),
    )
    await h._feed(message=message)


async def test_a_new_payment_gets_the_banner_the_confetti_and_a_key_hint(h: Harness) -> None:
    await h.press("risk:gb50")
    invoice = h.session.of(CreateInvoiceLink)[-1]
    await pay(h, invoice.payload)

    [photo] = h.session.of(SendPhoto)
    assert photo.caption is not None and "Оплата получена" in photo.caption
    assert len(photo.caption) <= MAX_CAPTION
    assert photo.message_effect_id == EFFECT_CONFETTI
    key_hint = h.session.of(SendMessage)[-1]
    assert "ssh-keygen" in key_hint.text and not key_hint.message_effect_id
    copies = [
        b.copy_text.text
        for row in key_hint.reply_markup.inline_keyboard
        for b in row
        if b.copy_text is not None
    ]
    assert copies == [T.KEY_COMMAND_UNIX, T.KEY_COMMAND_WINDOWS]


async def test_the_paid_banner_matches_the_design_file() -> None:
    assert (DESIGN / f"{banners.PAID}.png").is_file()


# --- кастомные эмодзи на экранах ---

IDS = {
    "payment": "7000000000000000001",
    "invite": "7000000000000000002",
    "disk": "7000000000000000003",
}


async def test_custom_emoji_icons_do_not_repeat_the_emoji_in_the_text(h: Harness) -> None:
    E.configure(IDS, enabled=True)
    await h.say("/start")
    photo = h.session.of(SendPhoto)[0]
    assert f'<tg-emoji emoji-id="{IDS["disk"]}">' in photo.caption
    primary = photo.reply_markup.inline_keyboard[0][0]
    assert primary.icon_custom_emoji_id == IDS["payment"]
    assert primary.text == T.BTN_TARIFFS and "💳" not in primary.text


async def test_without_premium_the_screen_still_arrives_with_plain_emoji(h: Harness) -> None:
    E.configure(IDS, enabled=True)
    h.session.custom_emoji_allowed = False
    alerts: list[str] = []

    async def alert(reason: str) -> None:
        alerts.append(reason)

    h.bot.session.middleware(EmojiFallback(E, alert))
    await h.say("/start")

    refused, delivered = h.session.of(SendPhoto)
    assert "<tg-emoji" in refused.caption and "<tg-emoji" not in delivered.caption
    first = delivered.reply_markup.inline_keyboard[0][0]
    assert first.icon_custom_emoji_id is None and first.text == f"💳 {T.BTN_TARIFFS}"
    assert len(alerts) == 1 and not E.active

    await h.press(keyboards.MENU_TARIFFS)  # дальше без повторов и новых алертов
    assert len(alerts) == 1


# --- экранирование ---


def test_plan_names_from_the_admin_cannot_break_the_markup() -> None:
    evil = replace(CATALOG.paid[0], name="<i>Хитрый</i> & Ко")
    text = texts.tariffs((evil,), None)
    assert "&lt;i&gt;Хитрый&lt;/i&gt; &amp; Ко" in text
    from fakes import check_telegram_html

    check_telegram_html(text)
    check_telegram_html(texts.plan_offer(evil, replaces=True))
