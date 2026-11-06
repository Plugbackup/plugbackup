"""Общий режим техработ: плашка в ответах бота, рассылки в начале и конце, права и перезапуск."""

import json
from pathlib import Path

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery

from fakes import FakeClock
from harness import ADMIN, SUPPORT, USER, Harness
from helpers import add_user, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.services.maintenance_mode import MAX_TEXT, MaintenanceMode, ModeKind
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb
from plugbackup_bot.tg.maint_banner import MaintenanceBanner
from plugbackup_bot.ui.screen import Screen, Ui

OWNER = Actor(ADMIN.id, Role.OWNER)
HELPER = Actor(SUPPORT.id, Role.SUPPORT)
W = core.MAINT
DESIGN = Path(__file__).resolve().parents[2] / "design" / "graphics" / "png"
WORK = "Идёт обновление хранилища, вернёмся к 18:00."
MAX_CAPTION = 1024


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock, assets_dir=DESIGN)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    await add_user(db, USER.id)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")
    return harness


def shown(h: Harness) -> list[str]:
    return [text for text, _, _ in h.last_buttons() if text != T.BTN_ADM_MENU]


async def turn_on(h: Harness, text: str = WORK) -> None:
    await h.press(cb(W, "on"), user=ADMIN)
    await h.say(text, user=ADMIN)
    await h.press(cb(W, "on!"), user=ADMIN)


# --- сервис ---


async def test_start_and_end_change_the_state_and_queue_broadcasts(h: Harness) -> None:
    assert not h.maintenance.active
    assert await h.maintenance.start(OWNER, f"  {WORK}  ") is ModeKind.OK
    assert h.maintenance.state is not None and h.maintenance.state.text == WORK
    assert await h.maintenance.start(OWNER, "Ещё раз") is ModeKind.ALREADY
    assert await h.maintenance.end(OWNER) is ModeKind.OK
    assert await h.maintenance.end(OWNER) is ModeKind.ALREADY
    rows, total = await h.broadcast.page(limit=10, offset=0)
    assert total == 2 and [item.kind for item in rows] == ["maintenance", "maintenance"]
    assert WORK in rows[1].text and rows[0].text == T.MAINT_BROADCAST_END  # новые сверху


async def test_the_text_is_checked(h: Harness) -> None:
    for bad in ("", "   ", "я" * (MAX_TEXT + 1)):
        assert await h.maintenance.start(OWNER, bad) is ModeKind.INVALID
    assert not h.maintenance.active
    assert await h.maintenance.start(OWNER, "я" * MAX_TEXT) is ModeKind.OK


async def test_support_cannot_switch_the_mode(h: Harness) -> None:
    assert await h.maintenance.start(HELPER, WORK) is ModeKind.DENIED
    await h.maintenance.start(OWNER, WORK)
    assert await h.maintenance.end(HELPER) is ModeKind.DENIED
    assert h.maintenance.active


async def test_the_mode_survives_a_restart(h: Harness) -> None:
    await h.maintenance.start(OWNER, WORK)
    reborn = MaintenanceMode(h.db, h.clock, h.broadcast, h.directory)
    assert not reborn.active
    await reborn.load()
    assert reborn.state is not None and reborn.state.text == WORK
    await reborn.end(OWNER)
    again = MaintenanceMode(h.db, h.clock, h.broadcast, h.directory)
    await again.load()
    assert not again.active


async def test_a_damaged_setting_means_the_mode_is_off(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('maintenance', 'сломано', 0)"
        )
    await h.maintenance.load()
    assert not h.maintenance.active


async def test_the_journal_has_no_text(h: Harness) -> None:
    await h.maintenance.start(OWNER, "Секретная причина работ")
    await h.maintenance.end(OWNER)
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=10, offset=0)
    actions = {entry.action for entry in entries}
    assert {"maintenance.start", "maintenance.end"} <= actions
    assert all("Секретная" not in json.dumps(e.details, ensure_ascii=False) for e in entries)


# --- плашка ---


async def test_clients_see_the_banner_and_admins_do_not(h: Harness) -> None:
    await h.say("/start")
    assert not plain(h.last().text).startswith("⚠️ Технические работы")
    await h.maintenance.start(OWNER, WORK)

    await h.say("/start")
    assert plain(h.last().text).startswith(f"⚠️ {T.MAINT_BANNER_TITLE}\n{WORK}")
    assert not h.last().photo or len(h.last().text) <= MAX_CAPTION

    h.session.requests.clear()
    await h.press("m:home")  # переход по кнопке тоже с плашкой
    assert plain(h.last().text).startswith("⚠️ Технические работы")

    await h.say("/admin", user=ADMIN)  # админу плашка не нужна
    assert "Технические работы" not in plain(h.last().text).split("\n")[0]
    assert WORK not in plain(h.last().text)

    await h.maintenance.end(OWNER)
    await h.say("/start")
    assert WORK not in plain(h.last().text)


async def test_the_banner_is_escaped(h: Harness) -> None:
    await h.maintenance.start(OWNER, "Работы <b>до 18:00</b> & позже")
    await h.say("/start")
    assert "&lt;b&gt;до 18:00&lt;/b&gt; &amp; позже" in h.last().text


async def test_a_banner_screen_loses_its_picture_when_the_caption_overflows() -> None:
    async def notice(tg_id: int) -> str | None:
        return "⚠️ Работы\n" + "я" * 200

    ui = Ui(notice=notice)
    long = Screen("т" * 900, banner="welcome")
    decorated = await ui._with_notice(5, long)
    assert decorated.banner is None and decorated.text.endswith("т" * 900)
    short = await ui._with_notice(5, Screen("коротко", banner="welcome"))
    assert short.banner == "welcome" and short.text.startswith("⚠️ Работы")
    assert await ui._with_notice(-100, long) is long  # группа: без плашки


async def test_a_banner_for_a_non_private_chat_is_never_added() -> None:
    class Mode:
        async def text_for(self, tg_id: int) -> str | None:
            return "работы"

    banner = MaintenanceBanner(Mode())  # type: ignore[arg-type]
    ui = Ui(notice=banner)
    screen = Screen("текст")
    assert await ui._with_notice(-1001, screen) is screen
    assert (await ui._with_notice(7, screen)).text.endswith("текст")


# --- админский сценарий ---


async def test_the_dialogue_switches_the_mode_on_and_off(h: Harness) -> None:
    await h.say("/admin", user=ADMIN)
    assert any(data == cb(W) for _, data, _ in h.last_buttons())
    await h.press(cb(W), user=ADMIN)
    assert T.MAINT_OFF in plain(h.last().text)
    assert shown(h) == [T.BTN_MAINT_START]

    await h.press(cb(W, "on"), user=ADMIN)
    assert await h.fsm_state(ADMIN) == "MaintenanceStates:text"
    await h.say("   ", user=ADMIN)
    assert "от 1 до" in plain(h.last().text)
    await h.say(WORK, user=ADMIN)
    assert T.ADM_CONFIRM_TITLE in plain(h.last().text) and WORK in plain(h.last().text)
    assert not h.maintenance.active  # пока только подтверждение
    confirm = [b for row in h.last().reply_markup.inline_keyboard for b in row]
    assert any(b.callback_data == cb(W, "on!") and b.style == "danger" for b in confirm)

    await h.press(cb(W, "on!"), user=ADMIN)
    assert h.maintenance.active and h.broadcast_kicks == [1]
    assert T.MAINT_STARTED in plain(h.last().text) and WORK in plain(h.last().text)
    assert shown(h) == [T.BTN_MAINT_END]

    h.messenger.sent.clear()
    await h.broadcast.run_due()
    assert {tg_id for tg_id, _ in h.messenger.sent} == {USER.id, ADMIN.id}
    assert all(WORK in text for _, text in h.messenger.sent)

    await h.press(cb(W, "off"), user=ADMIN)
    assert T.ADM_CONFIRM_TITLE in plain(h.last().text) and h.maintenance.active
    await h.press(cb(W, "off!"), user=ADMIN)
    assert not h.maintenance.active and h.broadcast_kicks == [1, 1]
    assert T.MAINT_ENDED in plain(h.last().text)

    h.messenger.sent.clear()
    await h.broadcast.run_due()
    assert all(text == T.MAINT_BROADCAST_END for _, text in h.messenger.sent) and h.messenger.sent


async def test_stale_buttons_and_a_lost_draft_do_not_break_anything(h: Harness) -> None:
    await h.press(cb(W, "on!"), user=ADMIN)  # состояние потеряно
    assert not h.maintenance.active
    assert T.MAINT_LOST in plain(h.last().text)
    await turn_on(h)
    await h.press(cb(W, "on"), user=ADMIN)  # уже включено
    assert T.MAINT_ALREADY_ON in plain(h.last().text)
    await h.press(cb(W, "off!"), user=ADMIN)
    await h.press(cb(W, "off"), user=ADMIN)  # уже выключено
    assert T.MAINT_ALREADY_OFF in plain(h.last().text)


async def test_support_gets_nothing_from_the_maintenance_section(h: Harness) -> None:
    await h.say("/admin", user=SUPPORT)
    assert all(data != cb(W) for _, data, _ in h.last_buttons())
    h.session.requests.clear()
    for data in (cb(W), cb(W, "on"), cb(W, "on!"), cb(W, "off"), cb(W, "off!")):
        await h.press(data, user=SUPPORT)
    assert h.session.outputs() == []
    denied = [a.text for a in h.session.of(AnswerCallbackQuery)]
    assert denied == [T.ADM_DENIED] * 5
    assert not h.maintenance.active


# --- меню админки ---


async def test_every_menu_button_opens_its_section(h: Harness) -> None:
    """Раньше кнопки «Клиенты», «Тарифы», «Инвайты» и «Ноды» отвечали «Эта кнопка устарела»."""
    await h.say("/admin", user=ADMIN)
    sections = [data for _, data, _ in h.last_buttons() if data and data.startswith("a:")]
    assert len(sections) >= 7
    for data in sections:
        h.session.requests.clear()
        await h.press(data, user=ADMIN)
        assert h.session.outputs(), data
        assert T.ADM_STALE not in [a.text for a in h.session.of(AnswerCallbackQuery)], data
        assert h.last().edited
        # Из любого раздела можно вернуться в меню админки.
        assert any(d == cb(core.MENU) for _, d, _ in h.last_buttons(nav=True)), data


async def test_support_menu_buttons_work_too(h: Harness) -> None:
    await h.say("/admin", user=SUPPORT)
    sections = [data for _, data, _ in h.last_buttons() if data and data.startswith("a:")]
    assert sections
    for data in sections:
        h.session.requests.clear()
        await h.press(data, user=SUPPORT)
        assert h.session.outputs(), data
        assert T.ADM_STALE not in [a.text for a in h.session.of(AnswerCallbackQuery)], data


@pytest.mark.parametrize("section", [core.CLIENTS, core.TARIFFS, core.INVITES, core.NODES])
async def test_the_section_root_matches_the_list(h: Harness, section: str) -> None:
    await h.press(cb(section), user=ADMIN)
    root = plain(h.last().text)
    await h.press(cb(section, "list"), user=ADMIN)
    assert plain(h.last().text) == root


async def test_the_dashboard_tells_the_owner_that_the_mode_is_on(h: Harness) -> None:
    await h.press(cb(core.DASH), user=ADMIN)
    assert "Идут технические работы" not in plain(h.last().text)
    await h.maintenance.start(OWNER, WORK)
    await h.press(cb(core.DASH), user=ADMIN)
    assert plain(h.last().text).startswith(f"Идут технические работы: {WORK}")
