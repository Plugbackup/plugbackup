"""Бот шлёт сообщения в режиме HTML: любой «<» вне тега Telegram отклоняет целиком."""

import importlib
from collections.abc import Iterator
from types import ModuleType

import pytest
from aiogram.exceptions import TelegramBadRequest

from fakes import check_telegram_html, make_bot
from harness import ADMIN, Harness
from plugbackup_bot.db import Database
from plugbackup_bot.tg import admin_texts

BAD = [
    "/maint <нода|all> <текст>",
    "/client <Telegram id>",
    "<b>без закрытия",
    "</b>",
    "<b><i>вложено</b></i>",
    "a < b",
    "<script>alert(1)</script>",
]
GOOD = [
    "<code>ssh-keygen -t ed25519</code>",
    "<pre>Host plugbackup\n    User u1001</pre>",
    "&lt;нода|all&gt;",
    "<b>жирный</b> и <i>курсив</i>",
    "обычный текст",
    '<a href="https://example.org/x?a=1">ссылка</a>',
    "<code>$env:USERPROFILE\\.ssh\\plugbackup</code>",
]

MODULES = [
    "plugbackup_bot.texts_ru",
    "plugbackup_bot.tg.texts",
    "plugbackup_bot.tg.admin_texts",
    "plugbackup_bot.tg.alert_texts",
    "plugbackup_bot.tg.alert_ui",
    "plugbackup_bot.tg.notices",
    "plugbackup_bot.tg.keyboards",
    "plugbackup_bot.tg.handlers.admin",
    "plugbackup_bot.tg.handlers.access",
    "plugbackup_bot.tg.handlers.alerts",
    "plugbackup_bot.tg.handlers.restore",
    "plugbackup_bot.tg.handlers.status",
    "plugbackup_bot.tg.handlers.start",
    "plugbackup_bot.tg.handlers.lifecycle",
    "plugbackup_bot.tg.handlers.payments",
]


@pytest.mark.parametrize("text", BAD)
def test_the_checker_rejects_broken_markup(text: str) -> None:
    with pytest.raises(ValueError):
        check_telegram_html(text)


@pytest.mark.parametrize("text", GOOD)
def test_the_checker_accepts_valid_markup(text: str) -> None:
    check_telegram_html(text)


async def test_the_fake_telegram_refuses_what_the_real_one_refuses() -> None:
    bot, session = make_bot()
    with pytest.raises(TelegramBadRequest, match="can't parse entities"):
        await bot.send_message(1, "/maint <нода|all> <текст>")
    assert len(session.requests) == 1  # запрос дошёл до сессии и был отклонён
    await bot.send_message(1, "/maint &lt;нода|all&gt;")
    await bot.send_message(1, "<сырой текст>", parse_mode=None)  # без разметки можно


async def test_the_admin_screen_that_failed_in_telegram_now_goes_through(db: Database) -> None:
    """Точный случай сбоя: /admin с «<нода|all>» в справке."""
    from fakes import FakeClock

    harness = await Harness.build(db, FakeClock())
    await harness.say("/admin", user=ADMIN)
    assert harness.last().text.startswith("Админка")
    assert "<нода" not in harness.last().text
    check_telegram_html(admin_texts.ADMIN_HELP)


async def test_error_replies_with_user_input_stay_valid(db: Database) -> None:
    """Подсказки об ошибках админа тоже идут в HTML и не должны ломаться."""
    from fakes import FakeClock

    harness = await Harness.build(db, FakeClock())
    for command in ("/client", "/maint", "/maint node1", "/invite", "/maint_end x"):
        await harness.say(command, user=ADMIN)
    assert len(harness.texts()) == 5


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (tuple, list, frozenset, set)):
        for item in value:
            yield from _strings(item)


@pytest.mark.parametrize("name", MODULES)
def test_no_text_constant_contains_stray_angle_brackets(name: str) -> None:
    module: ModuleType = importlib.import_module(name)
    for attribute, value in vars(module).items():
        if attribute.startswith("__"):
            continue
        for text in _strings(value):
            try:
                check_telegram_html(text)
            except ValueError as error:
                pytest.fail(f"{name}.{attribute}: {error}")


# --- остальные лимиты Telegram ---


async def test_the_fake_telegram_enforces_message_and_button_limits() -> None:
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    bot, _ = make_bot()
    with pytest.raises(TelegramBadRequest, match="too long"):
        await bot.send_message(1, "x" * 4097)
    await bot.send_message(1, "x" * 4096)

    def keyboard(**button: object) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Кнопка", **button)]]
        )  # type: ignore[arg-type]

    with pytest.raises(TelegramBadRequest, match="callback_data"):
        await bot.send_message(1, "текст", reply_markup=keyboard(callback_data="я" * 33))  # 66 байт
    await bot.send_message(1, "текст", reply_markup=keyboard(callback_data="я" * 32))  # 64 байта
    with pytest.raises(TelegramBadRequest, match="exactly one action"):
        await bot.send_message(1, "текст", reply_markup=keyboard())
    with pytest.raises(TelegramBadRequest, match="BUTTON_URL_INVALID"):
        await bot.send_message(1, "текст", reply_markup=keyboard(url="ftp://example.org"))


async def test_callback_answers_are_limited_to_200_characters() -> None:
    bot, _ = make_bot()
    with pytest.raises(TelegramBadRequest):
        await bot.answer_callback_query("1", text="x" * 201)
    await bot.answer_callback_query("1", text="x" * 200)
