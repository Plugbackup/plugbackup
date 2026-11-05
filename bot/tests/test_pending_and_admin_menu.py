"""Клиент, который ждёт ключ, и меню администратора."""

import pytest_asyncio
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeChat

from fakes import FakeClock, make_bot
from harness import Harness
from helpers import fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.commands import ADMIN_COMMANDS, COMMANDS, COMMANDS_EN
from plugbackup_bot.db import Database
from plugbackup_bot.runtime import configure_bot
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.states import AccessStates

KEY_LINE = f"ssh-ed25519 {fake_key_body(0)} me@home"


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    return await Harness.build(db, clock)


async def waiting_for_key(h: Harness) -> None:
    """Инвайт принят, ключа ещё нет."""
    code = await h.invite()
    await h.press(keyboards.MENU_BETA)
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")
    await h.say(code)


# --- клиент без ключа ---


async def test_the_beta_button_leads_on_instead_of_saying_there_is_a_storage(h: Harness) -> None:
    await waiting_for_key(h)
    h.storage.storage.clear()  # перезапуск бота или потерянное состояние

    await h.press(keyboards.MENU_BETA)
    assert T.PENDING_KEY_PROMPT in plain(h.last().text)
    assert "ssh-keygen -t ed25519" in h.last().text
    assert await h.fsm_state() == AccessStates.waiting_key.state

    await h.say(KEY_LINE)
    assert "Ключ принят" in plain(h.last().text)


async def test_status_tells_a_client_without_a_key_what_to_do(h: Harness) -> None:
    await waiting_for_key(h)
    await h.say("/status")
    text = h.last().text
    assert "Состояние: ожидает ключ" in text
    assert T.PENDING_KEY_PROMPT in text
    assert "Пришлите публичный ключ" in text


async def test_status_of_a_client_with_a_key_has_no_such_hint(h: Harness) -> None:
    await waiting_for_key(h)
    await h.say(KEY_LINE)
    await h.say("/status")
    assert T.PENDING_KEY_PROMPT not in h.last().text


def test_the_second_invite_hint_mentions_the_key() -> None:
    assert "/key" in texts.INVITE_ERRORS["has_client"]  # type: ignore[index]


# --- меню команд в Telegram ---


async def test_admins_get_a_wider_command_menu() -> None:
    bot, session = make_bot()
    await configure_bot(bot, [900, 901])
    general, general_en, first, second = session.of(SetMyCommands)

    assert general.scope is None
    assert general.language_code is None
    assert general_en.language_code == "en"
    assert [c.command for c in general.commands] == [c for c, _ in COMMANDS]
    assert [c.command for c in general_en.commands] == [c for c, _ in COMMANDS_EN]
    for method, admin_id in ((first, 900), (second, 901)):
        assert isinstance(method.scope, BotCommandScopeChat)
        assert method.scope.chat_id == admin_id
        assert [c.command for c in method.commands] == [c for c, _ in ADMIN_COMMANDS]
    assert ADMIN_COMMANDS[0][0] == "admin"
    assert {c for c, _ in COMMANDS} <= {c for c, _ in ADMIN_COMMANDS}


async def test_telegram_refusals_do_not_break_startup() -> None:
    bot, session = make_bot()
    refusal = TelegramAPIError(SetMyCommands(commands=[]), "Bad Request: chat not found")
    session.errors.extend([refusal] * 3)  # оба общих меню и меню админа
    await configure_bot(bot, [900])  # не бросает
    assert len(session.of(SetMyCommands)) == 3


def test_command_descriptions_fit_telegram_limits() -> None:
    for name, description in (*ADMIN_COMMANDS, *COMMANDS_EN):
        assert 1 <= len(name) <= 32 and name == name.lower()
        assert 3 <= len(description) <= 256
    assert len({name for name, _ in ADMIN_COMMANDS}) == len(ADMIN_COMMANDS)
    assert [n for n, _ in COMMANDS_EN] == [n for n, _ in COMMANDS]
