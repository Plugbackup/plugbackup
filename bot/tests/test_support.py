"""Поддержка: обращения клиентов, очередь и ответы в админке, права обеих ролей."""

import json
import sqlite3

import pytest
import pytest_asyncio

from fakes import FakeClock
from harness import ADMIN, ADMIN_CHAT, OTHER, SUPPORT, USER, Harness
from helpers import add_client, add_user, fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.services.support import (
    MAX_MESSAGES,
    MAX_TEXT,
    ReopenKind,
    ReplyKind,
    TicketStatus,
    WriteKind,
)
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb
from plugbackup_bot.tg.support_ui import CLOSE, START

OWNER = Actor(ADMIN.id, Role.OWNER)
HELPER = Actor(SUPPORT.id, Role.SUPPORT)
S = core.SUPPORT


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    await add_user(db, USER.id)
    await add_client(db, USER.id, 1, ssh_key_body=fake_key_body(0))  # клиент u1001
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")
    return harness


def shown(h: Harness) -> list[str]:
    return [text for text, _, _ in h.last_buttons() if text != T.BTN_ADM_MENU]


# --- сервис ---


async def test_messages_join_one_open_ticket(h: Harness) -> None:
    first = await h.support.write(USER.id, "Не могу подключиться")
    second = await h.support.write(USER.id, "Ошибка permission denied")
    assert (first.kind, first.created) == (WriteKind.OK, True)
    assert (second.ticket_id, second.created) == (first.ticket_id, False)
    view = await h.support.get(first.ticket_id or 0)
    assert view is not None and [m.text for m in view.messages] == [
        "Не могу подключиться",
        "Ошибка permission denied",
    ]
    assert view.row.login == "u1001" and view.row.ticket.status is TicketStatus.OPEN


async def test_statuses_follow_the_conversation(h: Harness) -> None:
    ticket_id = (await h.support.write(USER.id, "Вопрос")).ticket_id or 0
    assert await h.support.waiting_count() == 1
    result = await h.support.reply(ticket_id, HELPER, "Ответ")
    assert result.kind is ReplyKind.OK
    view = await h.support.get(ticket_id)
    assert view is not None and view.row.ticket.status is TicketStatus.ANSWERED
    assert await h.support.waiting_count() == 0  # ответили, ждём клиента
    await h.support.write(USER.id, "Спасибо, а ещё…")
    view = await h.support.get(ticket_id)
    assert view is not None and view.row.ticket.status is TicketStatus.OPEN
    assert await h.support.waiting_count() == 1


async def test_a_closed_ticket_is_followed_by_a_new_one(h: Harness) -> None:
    first = (await h.support.write(USER.id, "Раз")).ticket_id or 0
    assert (await h.support.close(first, OWNER)) is not None
    assert await h.support.close(first, OWNER) is None  # второй раз уже закрыто
    second = await h.support.write(USER.id, "Два")
    assert second.created and second.ticket_id != first
    assert await h.support.reopen(first, OWNER) is ReopenKind.OTHER_OPEN
    assert await h.support.reopen(second.ticket_id or 0, OWNER) is ReopenKind.NOT_CLOSED
    await h.support.close(second.ticket_id or 0, OWNER)
    assert await h.support.reopen(first, OWNER) is ReopenKind.OK
    assert await h.support.reopen(999, OWNER) is ReopenKind.NOT_FOUND


async def test_the_database_allows_only_one_open_ticket_per_person(h: Harness) -> None:
    await h.support.write(USER.id, "Раз")
    with pytest.raises(sqlite3.IntegrityError):
        async with h.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO tickets (tg_id, created_at, updated_at) VALUES (?, 0, 0)", (USER.id,)
            )


async def test_text_limits(h: Harness) -> None:
    for bad in ("", "  \n ", "я" * (MAX_TEXT + 1)):
        assert (await h.support.write(USER.id, bad)).kind is WriteKind.INVALID
    assert await h.support.current(USER.id) is None
    ticket_id = (await h.support.write(USER.id, "я" * MAX_TEXT)).ticket_id or 0
    assert (await h.support.reply(ticket_id, OWNER, "")).kind is ReplyKind.INVALID
    assert (await h.support.reply(999, OWNER, "Ответ")).kind is ReplyKind.NOT_FOUND


async def test_a_ticket_cannot_grow_forever(h: Harness) -> None:
    for i in range(MAX_MESSAGES):
        assert (await h.support.write(USER.id, f"сообщение {i}")).kind is WriteKind.OK
    assert (await h.support.write(USER.id, "ещё одно")).kind is WriteKind.FULL


async def test_replying_to_a_closed_ticket_is_refused(h: Harness) -> None:
    ticket_id = (await h.support.write(USER.id, "Вопрос")).ticket_id or 0
    await h.support.close(ticket_id, OWNER)
    assert (await h.support.reply(ticket_id, OWNER, "Поздно")).kind is ReplyKind.CLOSED


async def test_the_client_can_close_the_ticket(h: Harness) -> None:
    assert await h.support.close_own(USER.id) is None
    ticket_id = (await h.support.write(USER.id, "Вопрос")).ticket_id or 0
    closed = await h.support.close_own(USER.id)
    assert closed is not None and closed.id == ticket_id
    view = await h.support.get(ticket_id)
    assert view is not None and view.row.ticket.status is TicketStatus.CLOSED
    assert view.row.ticket.closed_by is None


async def test_the_queue_puts_waiting_tickets_first_and_oldest_first(h: Harness) -> None:
    for tg_id in (11, 12, 13):
        await add_user(h.db, tg_id)
    first = (await h.support.write(11, "Первый")).ticket_id or 0
    h.clock.advance(60)
    second = (await h.support.write(12, "Второй")).ticket_id or 0
    h.clock.advance(60)
    third = (await h.support.write(13, "Третий")).ticket_id or 0
    await h.support.reply(first, OWNER, "Ответ")  # первый отвечен: уходит вниз очереди
    rows, total = await h.support.page(closed=False, limit=10, offset=0)
    assert total == 3 and [r.ticket.id for r in rows] == [second, third, first]
    await h.support.close(third, OWNER)
    rows, total = await h.support.page(closed=True, limit=10, offset=0)
    assert total == 1 and rows[0].ticket.id == third and rows[0].login is None


async def test_the_journal_has_no_message_texts(h: Harness) -> None:
    ticket_id = (await h.support.write(USER.id, "Вопрос")).ticket_id or 0
    await h.support.reply(ticket_id, HELPER, "Секретный ответ")
    await h.support.close(ticket_id, HELPER)
    await h.support.reopen(ticket_id, HELPER)
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=10, offset=0)
    assert {e.action for e in entries} == {"ticket.reply", "ticket.close", "ticket.reopen"}
    assert all("Секретный" not in json.dumps(e.details, ensure_ascii=False) for e in entries)
    assert {e.actor_id for e in entries} == {SUPPORT.id}


# --- клиент ---


async def test_the_client_writes_and_the_admin_chat_is_told(h: Harness) -> None:
    await h.say("/support")
    assert await h.fsm_state() == "SupportStates:message"
    assert T.SUP_INTRO in plain(h.last().text)
    h.messenger.sent.clear()
    await h.say("Не работает <ключ> & ssh")
    assert "Обращение №1 принято" in plain(h.last().text)
    assert await h.fsm_state() is None
    [(chat_id, notice)] = h.messenger.sent
    assert chat_id == ADMIN_CHAT
    assert "Новое обращение №1" in plain(notice) and "u1001" in plain(notice)
    assert "Не работает <ключ> & ssh" in plain(notice)  # текст показан, не разобран как разметка
    markup = h.messenger.markups[-1]
    assert markup is not None
    [[button]] = markup.inline_keyboard
    assert button.callback_data == cb(S, "card", i=1)


async def test_a_second_message_joins_the_same_ticket(h: Harness) -> None:
    await h.say("/support")
    await h.say("Первое")
    await h.say("/support")
    assert "открыто обращение №1" in plain(h.last().text)
    assert T.BTN_SUP_CLOSE in shown(h)
    h.messenger.sent.clear()
    await h.say("Второе")
    assert "добавлено к обращению №1" in plain(h.last().text)
    assert "Новое сообщение в обращении №1" in plain(h.messenger.sent[0][1])
    assert (await h.support.current(USER.id)) is not None


async def test_a_bad_message_keeps_the_dialogue_open(h: Harness) -> None:
    await h.say("/support")
    await h.say("я" * (MAX_TEXT + 1))
    assert "от 1 до 2000" in plain(h.last().text)
    assert await h.support.current(USER.id) is None
    assert await h.fsm_state() == "SupportStates:message"


async def test_commands_are_not_taken_for_a_message(h: Harness) -> None:
    await h.say("/support")
    await h.say("/status")
    assert await h.support.current(USER.id) is None


async def test_the_main_menu_has_a_support_button(h: Harness) -> None:
    await h.say("/start")
    assert T.BTN_SUPPORT in [t for t, _, _ in h.last_buttons()]
    await h.press(START)
    assert await h.fsm_state() == "SupportStates:message"
    assert T.SUP_INTRO in plain(h.last().text)


async def test_the_client_closes_the_ticket(h: Harness) -> None:
    await h.say("/support")
    await h.say("Вопрос")
    await h.press(CLOSE)
    assert "Обращение №1 закрыто" in plain(h.last().text)
    await h.press(CLOSE)
    assert T.SUP_NOTHING_TO_CLOSE in plain(h.last().text)


async def test_an_ordinary_user_cannot_use_the_admin_buttons(h: Harness) -> None:
    await h.support.write(USER.id, "Вопрос")
    h.session.requests.clear()
    for data in (cb(S), cb(S, "card", i=1), cb(S, "rp", i=1), cb(S, "cl", i=1), cb(S, "ro", i=1)):
        await h.press(data, user=OTHER)
    assert h.session.outputs() == []
    view = await h.support.get(1)
    assert view is not None and view.row.ticket.status is TicketStatus.OPEN


# --- администратор и помощник ---


@pytest.mark.parametrize("staff", [ADMIN, SUPPORT], ids=["owner", "support"])
async def test_staff_answers_and_the_client_gets_the_message(h: Harness, staff) -> None:  # type: ignore[no-untyped-def]
    await h.say("/support")
    await h.say("Не подключается")
    h.messenger.sent.clear()

    await h.say("/admin", user=staff)
    assert any(text.startswith(T.BTN_ADM_SUPPORT) and "(1)" in text for text in shown(h))
    await h.press(cb(S), user=staff)
    assert "Ждут ответа: 1" in plain(h.last().text)
    row = next(t for t in shown(h) if "ждёт ответа" in t)
    assert "1 · u1001" in row
    await h.press(cb(S, "card", i=1), user=staff)
    card = plain(h.last().text)
    assert "u1001" in card and "Не подключается" in card and "ждёт ответа" in card

    await h.press(cb(S, "rp", i=1), user=staff)
    assert await h.fsm_state(staff) == "SupportAdminStates:reply"
    await h.say("Проверьте <ключ> в разделе /key", user=staff)
    assert T.SUPA_REPLIED in plain(h.last().text) and "отвечено" in plain(h.last().text)
    [(tg_id, message)] = h.messenger.sent
    assert tg_id == USER.id
    assert "Ответ по обращению №1" in plain(message)
    assert "Проверьте <ключ> в разделе /key" in plain(message)
    assert "&lt;ключ&gt;" in message  # разметка клиенту не уходит
    markup = h.messenger.markups[-1]
    assert markup is not None
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas == [START, CLOSE]
    assert await h.fsm_state(staff) is None

    # Клиент отвечает по кнопке: обращение снова ждёт ответа.
    await h.press(START)
    await h.say("Помогло, спасибо")
    view = await h.support.get(1)
    assert view is not None and view.row.ticket.status is TicketStatus.OPEN
    assert len(view.messages) == 3


async def test_the_client_who_blocked_the_bot_is_reported(h: Harness) -> None:
    await h.support.write(USER.id, "Вопрос")
    h.messenger.undeliverable.add(USER.id)
    await h.press(cb(S, "rp", i=1), user=ADMIN)
    await h.say("Ответ", user=ADMIN)
    assert T.SUPA_REPLIED_LOST in plain(h.last().text)
    view = await h.support.get(1)
    assert view is not None and len(view.messages) == 2  # сохранён в любом случае


async def test_closing_and_reopening_from_the_card(h: Harness) -> None:
    await h.support.write(USER.id, "Вопрос")
    h.messenger.sent.clear()
    await h.press(cb(S, "cl", i=1), user=SUPPORT)
    assert T.SUPA_CLOSED in plain(h.last().text) and "закрыто" in plain(h.last().text)
    assert [tg_id for tg_id, _ in h.messenger.sent] == [USER.id]
    assert "Обращение №1 закрыто" in plain(h.messenger.sent[0][1])
    assert T.BTN_SUPA_REOPEN in shown(h) and T.BTN_SUPA_REPLY not in shown(h)

    await h.press(cb(S, "cl", i=1), user=SUPPORT)  # повторно
    assert T.SUPA_ALREADY_CLOSED in plain(h.last().text)

    await h.press(cb(S, "list", x="c"), user=SUPPORT)
    assert any("1 · u1001" in t for t in shown(h))
    await h.press(cb(S, "ro", i=1), user=SUPPORT)
    assert T.SUPA_REOPENED in plain(h.last().text)
    assert T.BTN_SUPA_REPLY in shown(h)


async def test_replying_to_a_closed_ticket_from_the_dialogue(h: Harness) -> None:
    await h.support.write(USER.id, "Вопрос")
    await h.press(cb(S, "rp", i=1), user=ADMIN)
    await h.support.close(1, OWNER)  # пока админ печатал, обращение закрыли
    h.messenger.sent.clear()
    await h.say("Ответ", user=ADMIN)
    assert T.SUPA_ALREADY_CLOSED in plain(h.last().text)
    assert h.messenger.sent == []


async def test_a_card_for_an_unknown_ticket(h: Harness) -> None:
    await h.press(cb(S, "card", i=99), user=ADMIN)
    assert T.ADM_NOT_FOUND in plain(h.last().text)


async def test_a_long_thread_is_cut_to_the_latest_messages(h: Harness) -> None:
    for i in range(30):
        await h.support.write(USER.id, f"сообщение {i} " + "я" * 400)
    await h.press(cb(S, "card", i=1), user=ADMIN)
    text = h.last().text
    assert len(text) <= 4096
    assert T.SUPA_THREAD_CUT in plain(text)
    assert "сообщение 29" in plain(text) and "сообщение 0 " not in plain(text)


async def test_the_dashboard_counts_waiting_tickets(h: Harness) -> None:
    await h.press(cb(core.DASH), user=ADMIN)
    assert "Открытых обращений: 0" in plain(h.last().text)
    await h.support.write(USER.id, "Вопрос")
    await h.press(cb(core.DASH), user=ADMIN)
    assert "Открытых обращений: 1" in plain(h.last().text)


async def test_a_removed_helper_loses_access_at_once(h: Harness) -> None:
    await h.support.write(USER.id, "Вопрос")
    await h.directory.remove_support(OWNER, SUPPORT.id)
    h.session.requests.clear()
    await h.press(cb(S, "card", i=1), user=SUPPORT)
    await h.press(cb(S, "rp", i=1), user=SUPPORT)
    assert h.session.outputs() == []
