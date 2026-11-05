"""Клиент, который ждёт ключ, и меню администратора."""

import pytest_asyncio
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SetMyCommands
from aiogram.types import BotCommandScopeChat

from fakes import FakeClock, make_bot
from harness import ADMIN, OTHER, Harness
from helpers import fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.commands import ADMIN_COMMANDS, COMMANDS, COMMANDS_EN
from plugbackup_bot.db import Database
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.runtime import configure_bot
from plugbackup_bot.tg import admin_texts, keyboards, texts
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


# --- меню администратора ---


async def test_admin_command_shows_every_admin_command_and_buttons(h: Harness) -> None:
    await h.say("/admin", user=ADMIN)
    text = h.last().text
    for command in ("/stats", "/nodes", "/invite N", "/client u1001", "/broadcast", "/maint"):
        assert command in text
    assert [(label, data) for label, data, _ in h.last_buttons()] == [
        ("Статистика", admin_texts.ADMIN_STATS),
        ("Ноды", admin_texts.ADMIN_NODES),
        ("Инвайт-код", admin_texts.ADMIN_INVITE),
        ("Рассылка", admin_texts.ADMIN_BROADCAST),
    ]


async def test_admin_menu_is_invisible_to_everyone_else(h: Harness) -> None:
    await h.say("/admin")
    await h.say("/admin", user=OTHER)
    for data in (
        admin_texts.ADMIN_STATS,
        admin_texts.ADMIN_NODES,
        admin_texts.ADMIN_INVITE,
        admin_texts.ADMIN_BROADCAST,
    ):
        await h.press(data)
        await h.press(data, user=OTHER)
    assert h.texts() == []
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 0


async def test_admin_buttons_do_what_the_commands_do(h: Harness) -> None:
    await h.press(admin_texts.ADMIN_STATS, user=ADMIN)
    assert h.last().text.startswith("Статистика")

    await h.press(admin_texts.ADMIN_NODES, user=ADMIN)
    assert h.last().text.startswith("Ноды")
    assert "node1" in h.last().text

    await h.press(admin_texts.ADMIN_INVITE, user=ADMIN)
    assert h.last().text.count("<code>") == 1
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 1

    await h.press(admin_texts.ADMIN_BROADCAST, user=ADMIN)
    assert "Пришлите текст рассылки" in h.last().text
    assert await h.fsm_state(ADMIN) is not None


async def test_an_invite_from_the_button_really_works(h: Harness) -> None:
    await h.press(admin_texts.ADMIN_INVITE, user=ADMIN)
    code = h.last().text.split("<code>")[1].split("</code>")[0]
    await h.press(f"{keyboards.RISK_PREFIX}{keyboards.RISK_BETA}")
    await h.say(code)
    assert "Код принят" in plain(h.last().text)


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
