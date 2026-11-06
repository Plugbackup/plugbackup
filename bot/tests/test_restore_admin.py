"""Заявки на восстановление в админке: очередь, решение с комментарием, права, журнал."""

import json

import pytest_asyncio

from fakes import FakeClock
from harness import ADMIN, OTHER, SUPPORT, USER, Harness
from helpers import add_client, add_user, fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.services.restore import MAX_NOTE
from plugbackup_bot.tg import texts
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb

OWNER = Actor(ADMIN.id, Role.OWNER)
HELPER = Actor(SUPPORT.id, Role.SUPPORT)
R = core.RESTORE
DATE = "2025-10-08"


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    await add_user(db, USER.id)
    await add_client(db, USER.id, 1, ssh_key_body=fake_key_body(0))
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")
    await harness.restore.create(USER.id, DATE)  # заявка №1
    return harness


def shown(h: Harness) -> list[str]:
    return [text for text, _, _ in h.last_buttons() if text != T.BTN_ADM_MENU]


async def status(h: Harness) -> tuple[str, str | None, int | None]:
    async with (
        h.db.read() as conn,
        conn.execute("SELECT status, note, handled_by FROM restore_requests WHERE id = 1") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def client_messages(h: Harness) -> list[str]:
    return [text for tg_id, text in h.messenger.sent if tg_id == USER.id]


# --- сервис ---


async def test_resolving_records_who_and_audits_without_the_comment(h: Harness) -> None:
    resolved = await h.restore.resolve(1, "done", "  Секретный комментарий  ", HELPER)
    assert resolved is not None and resolved.note == "Секретный комментарий"
    assert await status(h) == ("done", "Секретный комментарий", SUPPORT.id)
    async with h.db.read() as conn:
        [entry] = await audit_repo.page(conn, limit=5, offset=0)
    assert (entry.action, entry.actor_id, entry.target_id) == ("restore.resolve", SUPPORT.id, "1")
    assert entry.details == {"status": "done", "comment": True}
    assert "Секретный" not in json.dumps(entry.details, ensure_ascii=False)


async def test_a_long_comment_is_cut_and_an_empty_one_is_dropped(h: Harness) -> None:
    await h.restore.resolve(1, "rejected", "я" * (MAX_NOTE + 50), OWNER)
    state, note, _ = await status(h)
    assert state == "rejected" and note is not None and len(note) == MAX_NOTE
    await h.restore.create(USER.id, DATE)
    await h.restore.resolve(2, "done", "   ", OWNER)
    async with (
        h.db.read() as conn,
        conn.execute("SELECT note FROM restore_requests WHERE id = 2") as c,
    ):
        row = await c.fetchone()
    assert row is not None and row[0] is None


async def test_the_queue_and_the_view(h: Harness) -> None:
    rows, total = await h.restore.page(handled=False, limit=10, offset=0)
    assert total == 1 and rows[0].client.login == "u1001" and rows[0].status == "new"
    assert await h.restore.page(handled=True, limit=10, offset=0) == ([], 0)
    await h.restore.resolve(1, "done", None, OWNER)
    assert (await h.restore.page(handled=False, limit=10, offset=0))[1] == 0
    handled, total = await h.restore.page(handled=True, limit=10, offset=0)
    assert total == 1 and handled[0].handled_by == ADMIN.id and handled[0].handled_at is not None
    assert await h.restore.get(99) is None


# --- админка ---


async def test_the_menu_shows_the_number_of_new_requests(h: Harness) -> None:
    await h.say("/admin", user=SUPPORT)
    assert any(t.startswith(T.BTN_ADM_RESTORE) and "(1)" in t for t in shown(h))
    await h.press(cb(R), user=SUPPORT)
    assert "Новых заявок: 1" in plain(h.last().text)
    assert any("1 · u1001 · 8 октября" in t for t in shown(h))


async def test_done_with_a_comment_reaches_the_client(h: Harness) -> None:
    await h.press(cb(R, "card", i=1), user=ADMIN)
    card = plain(h.last().text)
    assert "u1001" in card and "8 октября" in card and "ждёт решения" in card
    assert shown(h)[:2] == [T.BTN_RA_DONE, T.BTN_RA_REJECT]

    await h.press(cb(R, "dn", i=1), user=ADMIN)
    assert await h.fsm_state(ADMIN) == "RestoreAdminStates:note"
    await h.say("Снимок на месте, откатили <home> & данные", user=ADMIN)
    confirm = plain(h.last().text)
    assert "помечена как выполнена" in confirm and "Снимок на месте" in confirm
    buttons = [b for row in h.last().reply_markup.inline_keyboard for b in row]
    ok = next(b for b in buttons if b.callback_data == cb(R, "ok", i=1))
    assert ok.style == "success"
    assert await status(h) == ("new", None, None)

    await h.press(cb(R, "ok", i=1), user=ADMIN)
    assert await status(h) == ("done", "Снимок на месте, откатили <home> & данные", ADMIN.id)
    [message] = client_messages(h)
    assert plain(message) == (
        plain(texts.restore_resolved(1, "8 октября", done=True))
        + "\n\nКомментарий: Снимок на месте, откатили <home> & данные"
    )
    assert "&lt;home&gt;" in message  # комментарий экранирован
    after = plain(h.last().text)
    assert "Заявка №1 выполнена, клиент уведомлён" in after and "Статус: выполнена" in after
    assert "Комментарий клиенту" in after and f"Решил: {ADMIN.id}" in after
    assert await h.fsm_state(ADMIN) is None


async def test_rejection_asks_with_a_red_button_and_may_skip_the_comment(h: Harness) -> None:
    await h.press(cb(R, "rj", i=1), user=SUPPORT)
    await h.press(cb(R, "sk", i=1, x="r"), user=SUPPORT)
    assert "без комментария" in plain(h.last().text)
    buttons = [b for row in h.last().reply_markup.inline_keyboard for b in row]
    assert next(b for b in buttons if b.callback_data == cb(R, "ok", i=1)).style == "danger"
    await h.press(cb(R, "ok", i=1), user=SUPPORT)
    assert await status(h) == ("rejected", None, SUPPORT.id)
    assert client_messages(h) == [texts.restore_resolved(1, "8 октября", done=False)]


async def test_a_bad_comment_keeps_the_dialogue(h: Harness) -> None:
    await h.press(cb(R, "dn", i=1), user=ADMIN)
    await h.say("я" * (MAX_NOTE + 1), user=ADMIN)
    assert f"от 1 до {MAX_NOTE}" in plain(h.last().text)
    assert await h.fsm_state(ADMIN) == "RestoreAdminStates:note"


async def test_a_forged_or_stale_confirmation_does_nothing(h: Harness) -> None:
    await h.press(cb(R, "ok", i=1), user=ADMIN)  # без выбора решения
    assert T.RA_LOST in plain(h.last().text)
    await h.press(cb(R, "dn", i=1), user=ADMIN)
    await h.press(cb(R, "sk", i=1, x="d"), user=ADMIN)
    await h.press(cb(R, "ok", i=7), user=ADMIN)  # чужой номер
    assert T.RA_LOST in plain(h.last().text)
    assert await status(h) == ("new", None, None) and client_messages(h) == []


async def test_a_request_is_resolved_only_once(h: Harness) -> None:
    await h.press(cb(R, "dn", i=1), user=ADMIN)
    await h.press(cb(R, "sk", i=1, x="d"), user=ADMIN)
    await h.press(cb(R, "ok", i=1), user=ADMIN)
    # Кнопки старого экрана: решение уже принято.
    await h.press(cb(R, "dn", i=1), user=SUPPORT)
    assert T.RA_TOO_LATE in plain(h.last().text)
    await h.press(cb(R, "ok", i=1), user=ADMIN)  # состояние сброшено после первого решения
    assert T.RA_LOST in plain(h.last().text)
    assert len(client_messages(h)) == 1 and (await status(h))[0] == "done"


async def test_the_decision_is_refused_if_someone_else_was_faster(h: Harness) -> None:
    await h.press(cb(R, "rj", i=1), user=ADMIN)
    await h.press(cb(R, "sk", i=1, x="r"), user=ADMIN)
    await h.restore.resolve(1, "done", None, HELPER)  # помощник успел раньше
    await h.press(cb(R, "ok", i=1), user=ADMIN)
    assert T.RA_TOO_LATE in plain(h.last().text) and "Статус: выполнена" in plain(h.last().text)
    assert (await status(h))[0] == "done" and client_messages(h) == []


async def test_the_handled_tab_and_unknown_cards(h: Harness) -> None:
    await h.restore.resolve(1, "rejected", None, OWNER)
    await h.press(cb(R, "list", x="h"), user=ADMIN)
    assert "Обработанных заявок: 1" in plain(h.last().text)
    await h.press(cb(R, "list"), user=ADMIN)
    assert T.RA_EMPTY in plain(h.last().text)
    await h.press(cb(R, "card", i=99), user=ADMIN)
    assert T.ADM_NOT_FOUND in plain(h.last().text)


async def test_ordinary_users_cannot_touch_requests(h: Harness) -> None:
    h.session.requests.clear()
    for data in (cb(R), cb(R, "card", i=1), cb(R, "dn", i=1), cb(R, "ok", i=1), "rq:done:1"):
        await h.press(data, user=OTHER)
    assert h.session.outputs() == [] and (await status(h))[0] == "new"
