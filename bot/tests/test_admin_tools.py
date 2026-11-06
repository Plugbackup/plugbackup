"""Админы, журнал, экспорт CSV и копия базы."""

import sqlite3
from pathlib import Path

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMyCommands, SendDocument, SetMyCommands

from fakes import FakeClock, make_bot
from harness import ADMIN, OTHER, SUPPORT, USER, Harness
from helpers import NOW, add_client, add_user, fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.commands import ADMIN_COMMANDS, SUPPORT_COMMANDS, set_admin_menu
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.db import payments as payments_repo
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.services.exports import DELIMITER, safe_cell, to_csv
from plugbackup_bot.tg.admin import core, journal
from plugbackup_bot.tg.admin import export as export_module
from plugbackup_bot.tg.admin.core import cb

OWNER = Actor(ADMIN.id, Role.OWNER)
HELPER = Actor(SUPPORT.id, Role.SUPPORT)
NEWBIE = 777001
KEY = fake_key_body(3)


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await harness.directory.add_support(OWNER, SUPPORT.id)
    await add_user(db, USER.id)
    client_id = await add_client(db, USER.id, 1, ssh_key_body=KEY)
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE users SET username = ? WHERE tg_id = ?", ("=cmd|' /C calc'!A0", USER.id)
        )
        await conn.execute(
            "UPDATE clients SET admin_note = 'секретная заметка' WHERE id = ?", (client_id,)
        )
        await payments_repo.insert_payment(
            conn, tg_id=USER.id, client_id=client_id, plan_id="gb50", stars=50,
            charge_id="chg-1", provider_charge_id=None, invoice_payload="x", is_recurring=False,
            is_first=True, sub_expires_at=None, now=NOW,
        )  # fmt: skip
        await conn.execute("DELETE FROM audit_log")
    return harness


def documents(h: Harness) -> list[SendDocument]:
    return h.session.of(SendDocument)


def shown(h: Harness) -> list[str]:
    return [text for text, _, _ in h.last_buttons() if text != T.BTN_ADM_MENU]


# --- CSV ---


def test_formula_like_cells_are_neutralised() -> None:
    for danger in ("=1+1", "+1", "-1", "@SUM(A1)", "\tтаб", "\rперевод"):
        assert safe_cell(danger) == "'" + danger
    assert safe_cell("обычный") == "обычный"
    assert safe_cell(-5) == -5  # числа не трогаем
    assert safe_cell("") == ""


def test_csv_has_bom_semicolons_and_quotes() -> None:
    data = to_csv(["a", "b"], [["x;y", 'он сказал "да"'], ["=cmd", 1]])
    assert data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    assert text.splitlines()[0] == f"a{DELIMITER}b"
    assert '"x;y"' in text and '"он сказал ""да"""' in text and "'=cmd" in text


async def test_clients_export_has_no_secrets_and_no_formulas(h: Harness) -> None:
    file = await h.exports.clients(OWNER)
    assert file is not None and file.rows == 1
    text = file.data.decode("utf-8-sig")
    assert KEY not in text and "секретная заметка" not in text and "ssh" not in text.lower()
    header, row = text.splitlines()
    assert header.startswith("login;tg_id;username;plan;state")
    assert row.startswith("u1001;5;'=cmd|")  # имя пользователя не станет формулой
    assert file.name.startswith("clients-") and file.name.endswith(".csv")


async def test_payments_export(h: Harness) -> None:
    await h.payments.refund(1, OWNER, revoke_access=False)
    file = await h.exports.payments(OWNER)
    assert file is not None and file.rows == 1
    header, row = file.data.decode("utf-8-sig").splitlines()
    assert header.startswith("id;date;tg_id;login;username;plan;stars;kind;status;refunded")
    assert ";50;first;refunded;" in row and row.endswith(";chg-1")


async def test_exports_are_logged_and_closed_to_support(h: Harness) -> None:
    assert await h.exports.clients(HELPER) is None
    assert await h.exports.payments(HELPER) is None
    assert await h.exports.backup(HELPER) is None
    await h.exports.clients(OWNER)
    await h.exports.payments(OWNER)
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=10, offset=0)
    assert {e.action for e in entries} == {"export.clients", "export.payments"}
    assert all(e.details == {"rows": 1} for e in entries)


# --- копия базы ---


async def test_the_backup_is_a_consistent_database_copy(h: Harness, tmp_path: Path) -> None:
    file = await h.exports.backup(OWNER)
    assert file is not None and file.name.startswith("plugbackup-") and file.name.endswith(".db")
    target = tmp_path / "copy.db"
    target.write_bytes(file.data)
    copy = sqlite3.connect(target)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert copy.execute("SELECT COUNT(*) FROM clients").fetchone() == (1,)
        assert copy.execute("SELECT COUNT(*) FROM payments").fetchone() == (1,)
    finally:
        copy.close()


async def test_backup_command_sends_the_file_to_the_owner_only(h: Harness) -> None:
    await h.say("/backup_db", user=SUPPORT)
    assert h.last().text == T.ADM_DENIED and documents(h) == []
    await h.say("/backup_db", user=OTHER)
    assert documents(h) == []

    await h.say("/backup_db", user=ADMIN)
    [sent] = documents(h)
    assert sent.chat_id == ADMIN.id
    assert sent.document.filename.endswith(".db")  # type: ignore[union-attr]
    assert T.EXPORT_BACKUP_SENT in plain(h.last().text)
    async with h.db.read() as conn:
        [entry] = await audit_repo.page(conn, limit=5, offset=0)
    assert entry.action == "export.backup" and "size" in entry.details


async def test_backup_from_a_group_goes_to_the_private_chat(h: Harness) -> None:
    await h.say("/backup_db", user=ADMIN, chat_type="supergroup")
    [sent] = documents(h)
    assert sent.chat_id == ADMIN.id  # не в группу
    assert T.EXPORT_PRIVATE_NOTE in plain(h.last().text)


async def test_the_export_screen_sends_files_to_a_private_chat(h: Harness) -> None:
    await h.press(cb(core.EXPORT), user=ADMIN)
    assert shown(h) == [T.BTN_EXPORT_CLIENTS, T.BTN_EXPORT_PAYMENTS, T.BTN_EXPORT_BACKUP]
    await h.press(cb(core.EXPORT, "cl"), user=ADMIN)
    await h.press(cb(core.EXPORT, "pa"), user=ADMIN)
    await h.press(cb(core.EXPORT, "bk"), user=ADMIN)
    names = [d.document.filename for d in documents(h)]  # type: ignore[union-attr]
    assert [n.split("-")[0] for n in names] == ["clients", "payments", "plugbackup"]
    assert {d.chat_id for d in documents(h)} == {ADMIN.id}
    assert "Файл clients-" in plain(h.session.outputs()[-3].text)
    assert T.EXPORT_BACKUP_SENT in plain(h.last().text)


async def test_a_file_that_cannot_be_delivered_is_reported(h: Harness) -> None:
    h.session.errors.extend(
        [None, TelegramBadRequest(method=SendDocument, message="chat not found")]
    )
    await h.press(cb(core.EXPORT, "cl"), user=ADMIN)
    assert T.EXPORT_FAILED in plain(h.last().text)


async def test_a_file_over_the_telegram_limit_is_refused(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_module, "MAX_FILE_BYTES", 10)
    await h.press(cb(core.EXPORT, "bk"), user=ADMIN)
    assert T.EXPORT_TOO_BIG in plain(h.last().text) and documents(h) == []


async def test_support_cannot_use_exports(h: Harness) -> None:
    await h.say("/admin", user=SUPPORT)
    assert all(data != cb(core.EXPORT) for _, data, _ in h.last_buttons())
    h.session.requests.clear()
    for action in ("", "cl", "pa", "bk"):
        await h.press(cb(core.EXPORT, action), user=SUPPORT)
    assert h.session.outputs() == [] and documents(h) == []


# --- админы ---


async def test_the_owner_adds_a_helper_and_the_menu_is_set(h: Harness) -> None:
    await h.press(cb(core.ADMINS), user=ADMIN)
    assert str(ADMIN.id) in plain(h.last().text) and str(SUPPORT.id) in plain(h.last().text)
    await h.press(cb(core.ADMINS, "add"), user=ADMIN)
    assert await h.fsm_state(ADMIN) == "AdminAdminStates:add_id"
    await h.say("не число", user=ADMIN)
    assert T.ADMINS_BAD_ID in plain(h.last().text)

    h.messenger.sent.clear()
    h.session.requests.clear()
    await h.say(str(NEWBIE), user=ADMIN)
    assert f"Помощник {NEWBIE} добавлен" in plain(h.last().text)
    assert T.ADMINS_MENU_SET in plain(h.last().text)
    assert (await h.directory.actor_for(NEWBIE)) == Actor(NEWBIE, Role.SUPPORT)
    [menu] = h.session.of(SetMyCommands)
    assert menu.scope.chat_id == NEWBIE  # type: ignore[union-attr]
    assert [c.command for c in menu.commands] == [name for name, _ in SUPPORT_COMMANDS]
    assert (NEWBIE, T.ADMINS_WELCOME) in h.messenger.sent
    async with h.db.read() as conn:
        [entry] = await audit_repo.page(conn, limit=5, offset=0)
    assert (entry.action, entry.target_id) == ("admin.add", str(NEWBIE))


async def test_a_known_admin_is_not_added_twice(h: Harness) -> None:
    await h.press(cb(core.ADMINS, "add"), user=ADMIN)
    await h.say(str(SUPPORT.id), user=ADMIN)
    assert T.ADMINS_ALREADY in plain(h.last().text)
    await h.press(cb(core.ADMINS, "add"), user=ADMIN)
    await h.say(str(ADMIN.id), user=ADMIN)  # владелец уже администратор
    assert T.ADMINS_ALREADY in plain(h.last().text)
    assert (await h.directory.actor_for(ADMIN.id)) == OWNER


async def test_removing_a_helper_asks_and_cuts_access_at_once(h: Harness) -> None:
    await h.press(cb(core.ADMINS, "rm", i=SUPPORT.id), user=ADMIN)
    assert T.ADM_CONFIRM_TITLE in plain(h.last().text)
    assert (await h.directory.actor_for(SUPPORT.id)) is not None  # пока подтверждение

    h.session.requests.clear()
    h.messenger.sent.clear()
    await h.press(cb(core.ADMINS, "rm!", i=SUPPORT.id), user=ADMIN)
    assert f"Помощник {SUPPORT.id} убран" in plain(h.last().text)
    assert (await h.directory.actor_for(SUPPORT.id)) is None
    [removed] = h.session.of(DeleteMyCommands)
    assert removed.scope.chat_id == SUPPORT.id  # type: ignore[union-attr]
    assert (SUPPORT.id, T.ADMINS_GOODBYE) in h.messenger.sent

    h.session.requests.clear()
    await h.say("/admin", user=SUPPORT)  # бывший помощник админку уже не откроет
    assert h.session.outputs() == []


async def test_an_owner_cannot_be_removed(h: Harness) -> None:
    await h.press(cb(core.ADMINS, "rm", i=ADMIN.id), user=ADMIN)
    assert T.ADMINS_PROTECTED in plain(h.last().text)
    await h.press(cb(core.ADMINS, "rm!", i=ADMIN.id), user=ADMIN)
    assert T.ADMINS_PROTECTED in plain(h.last().text)
    assert (await h.directory.actor_for(ADMIN.id)) == OWNER
    await h.press(cb(core.ADMINS, "rm", i=424242), user=ADMIN)
    assert T.ADMINS_NOT_FOUND in plain(h.last().text)


async def test_support_cannot_manage_admins(h: Harness) -> None:
    await h.say("/admin", user=SUPPORT)
    assert all(data != cb(core.ADMINS) for _, data, _ in h.last_buttons())
    h.session.requests.clear()
    for data in (
        cb(core.ADMINS),
        cb(core.ADMINS, "add"),
        cb(core.ADMINS, "rm", i=ADMIN.id),
        cb(core.ADMINS, "rm!", i=SUPPORT.id),
    ):
        await h.press(data, user=SUPPORT)
    assert h.session.outputs() == []
    assert (await h.directory.actor_for(SUPPORT.id)) is not None  # себя не удалить


async def test_a_command_menu_failure_does_not_break_the_action() -> None:
    bot, session = make_bot()
    session.errors.append(TelegramBadRequest(method=SetMyCommands, message="chat not found"))
    assert await set_admin_menu(bot, 5, Role.SUPPORT) is False
    assert await set_admin_menu(bot, 5, Role.OWNER) is True
    [_, menu] = session.of(SetMyCommands)
    assert [c.command for c in menu.commands] == [name for name, _ in ADMIN_COMMANDS]


# --- журнал ---


async def test_the_journal_lists_actions_with_pages(h: Harness) -> None:
    await h.support.write(USER.id, "Вопрос")
    ticket = await h.support.current(USER.id)
    assert ticket is not None
    ticket_id = ticket.id
    for _ in range(12):
        await h.support.reply(ticket_id, OWNER, "ответ")
    await h.press(cb(core.JOURNAL), user=ADMIN)
    text = plain(h.last().text)
    assert "Записей: 12" in text and "ответ на обращение" in text
    assert f"{ADMIN.id} (owner)" in text and f"ticket {ticket_id}" in text
    assert text.count("\n") >= journal.PAGE_SIZE
    assert "из 2" in " ".join(label for label, _, _ in h.last_buttons())

    await h.press(cb(core.JOURNAL, "list", p=1), user=ADMIN)
    assert plain(h.last().text).count("ответ на обращение") == 2  # на второй странице остаток
    await h.press(cb(core.JOURNAL, "list", p=99), user=ADMIN)  # страницы за краем не ломают
    assert "ответ на обращение" in plain(h.last().text)


async def test_the_journal_shows_unknown_actions_and_escapes_details(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await audit_repo.record(
            conn, OWNER, "custom.thing", now=NOW, target_type="x", target_id="1", note="<b>&</b>"
        )
    await h.press(cb(core.JOURNAL), user=ADMIN)
    assert "custom.thing" in plain(h.last().text) and "note=<b>&</b>" in plain(h.last().text)
    assert "&lt;b&gt;&amp;&lt;/b&gt;" in h.last().text


async def test_an_empty_journal(h: Harness) -> None:
    await h.press(cb(core.JOURNAL), user=ADMIN)
    assert T.JOURNAL_EMPTY in plain(h.last().text)


async def test_support_cannot_read_the_journal(h: Harness) -> None:
    await h.say("/admin", user=SUPPORT)
    assert all(data != cb(core.JOURNAL) for _, data, _ in h.last_buttons())
    h.session.requests.clear()
    await h.press(cb(core.JOURNAL), user=SUPPORT)
    await h.press(cb(core.JOURNAL, "list", p=1), user=SUPPORT)
    assert h.session.outputs() == []


# --- меню ---


async def test_each_role_sees_exactly_its_sections(h: Harness) -> None:
    await h.say("/admin", user=ADMIN)
    owner = {data for _, data, _ in h.last_buttons() if data and data.startswith("a:")}
    assert owner == {
        cb(section)
        for section in (
            core.DASH, core.CLIENTS, core.TARIFFS, core.INVITES, core.NODES, core.BROADCASTS,
            core.MAINT, core.SUPPORT, core.RESTORE, core.PAYMENTS, core.ADMINS, core.JOURNAL,
            core.EXPORT,
        )
    }  # fmt: skip
    await h.say("/admin", user=SUPPORT)
    helper = {data for _, data, _ in h.last_buttons() if data and data.startswith("a:")}
    assert helper == {cb(core.CLIENTS), cb(core.SUPPORT), cb(core.RESTORE)}
