"""Админка через настоящий Dispatcher: меню по ролям, права на сервере, клиенты, подтверждения."""

import inspect

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery

from fakes import FakeClock
from harness import ADMIN, OTHER, SUPPORT, USER, Harness
from helpers import NOW, add_client, add_user, fake_key_body, plain
from plugbackup_bot import texts_ru as T
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.db.clients import get_client
from plugbackup_bot.db.jobs import Op
from plugbackup_bot.db.usage import record_sample
from plugbackup_bot.domain.plans import GIB
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.domain.states import State
from plugbackup_bot.tg.admin import core
from plugbackup_bot.tg.admin.core import cb, permission_of
from plugbackup_bot.tg.admin.router import build_router
from plugbackup_bot.tg.handlers import emoji_preview

KEY = fake_key_body(0)
C = core.CLIENTS
OWNER = Actor(ADMIN.id, Role.OWNER)
DAY = 24 * 3600


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await add_user(db, USER.id)
    await add_client(db, USER.id, 1, ssh_key_body=KEY)  # клиент u1001
    await harness.directory.add_support(OWNER, SUPPORT.id)
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM audit_log")  # настройка стенда в журнал не входит
    return harness


async def client(h: Harness, client_id: int = 1):  # type: ignore[no-untyped-def]
    async with h.db.read() as conn:
        found = await get_client(conn, client_id)
    assert found is not None
    return found


async def actions(h: Harness) -> list[str]:
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=100, offset=0)
    return [e.action for e in reversed(entries)]


def labels(h: Harness) -> list[str]:
    markup = h.last().reply_markup
    assert markup is not None
    return [b.text for row in markup.inline_keyboard for b in row]


def button(h: Harness, text: str):  # type: ignore[no-untyped-def]
    markup = h.last().reply_markup
    assert markup is not None
    for row in markup.inline_keyboard:
        for found in row:
            if found.text.replace(" ", " ").endswith(text):
                return found
    raise AssertionError(f"нет кнопки {text!r} среди {labels(h)}")


async def press_button(h: Harness, text: str, user=ADMIN) -> None:  # type: ignore[no-untyped-def]
    await h.press(button(h, text).callback_data, user=user)


# --- кто может открыть админку ---


async def test_admins_get_the_menu_by_their_role(h: Harness) -> None:
    await h.say("/admin", user=ADMIN)
    assert "Роль: owner" in plain(h.last().text)
    owner_labels = {name.split(" ", 1)[1] for name in labels(h) if " " in name}
    assert {T.BTN_ADM_DASHBOARD, T.BTN_ADM_CLIENTS} <= owner_labels

    await h.say("/admin", user=SUPPORT)
    assert "Роль: support" in plain(h.last().text)
    helper_labels = {name.split(" ", 1)[1] for name in labels(h) if " " in name}
    assert T.BTN_ADM_CLIENTS in helper_labels
    assert T.BTN_ADM_DASHBOARD not in helper_labels  # дашборд только владельцу


async def test_ordinary_users_do_not_get_an_admin_menu(h: Harness) -> None:
    await h.say("/admin", user=USER)
    await h.press(cb(C, "list"), user=USER)  # нажатие чужой админской кнопки ни к чему не ведёт
    assert h.session.outputs() == []


async def test_the_role_is_read_on_every_action(h: Harness) -> None:
    await h.say("/client u1001", user=SUPPORT)
    assert "Клиент" in plain(h.last().text)
    await h.directory.remove_support(OWNER, SUPPORT.id)
    h.session.requests.clear()
    await h.say("/client u1001", user=SUPPORT)
    assert h.session.outputs() == []  # права сняты, перезапуск не нужен


async def test_env_owners_are_synced_and_bot_supports_survive(
    db: Database, clock: FakeClock
) -> None:
    from plugbackup_bot.services.admins import AdminDirectory

    first = AdminDirectory(db, clock, [900, 902])
    await first.sync()
    await first.add_support(Actor(900, Role.OWNER), 901)
    second = AdminDirectory(db, clock, [900])  # 902 убрали из .env
    await second.sync()
    assert (await second.actor_for(900)).role is Role.OWNER  # type: ignore[union-attr]
    assert await second.actor_for(902) is None
    assert (await second.actor_for(901)).role is Role.SUPPORT  # type: ignore[union-attr]


async def test_owners_cannot_be_removed_and_support_cannot_add_admins(h: Harness) -> None:
    from plugbackup_bot.services.admins import AddKind, RemoveKind

    helper = Actor(SUPPORT.id, Role.SUPPORT)
    assert await h.directory.add_support(helper, 777) is AddKind.DENIED
    assert await h.directory.remove_support(helper, SUPPORT.id) is RemoveKind.DENIED
    assert await h.directory.remove_support(OWNER, ADMIN.id) is RemoveKind.PROTECTED
    assert await h.directory.add_support(OWNER, SUPPORT.id) is AddKind.ALREADY
    assert await h.directory.remove_support(OWNER, 12345) is RemoveKind.NOT_FOUND


# --- права проверяются на сервере ---


def all_admin_handlers() -> list[tuple[str, object]]:
    found = []
    for router in (build_router(None), emoji_preview.build_router(None)):  # type: ignore[arg-type]
        for observer in (router.message, router.callback_query):
            for handler in observer.handlers:
                found.append((handler.callback.__name__, handler.callback))
    return found


def test_every_admin_handler_declares_a_permission_and_takes_the_actor() -> None:
    handlers = all_admin_handlers()
    assert len(handlers) > 20
    for name, callback in handlers:
        assert permission_of(callback) is not None, f"{name}: нет права"  # type: ignore[arg-type]
        assert "actor" in inspect.signature(inspect.unwrap(callback)).parameters, name  # type: ignore[arg-type]


OWNER_ONLY = [
    ("ex", dict(i=1)),
    ("exd", dict(i=1, p=7)),
    ("pl", dict(i=1)),
    ("pld", dict(i=1, x="gb100")),
    ("bn", dict(i=1)),
    ("bnd", dict(i=1, p=10)),
    ("dl", dict(i=1)),
    ("dl!", dict(i=1)),
]


@pytest.mark.parametrize(("action", "args"), OWNER_ONLY)
async def test_support_cannot_run_owner_actions_even_with_a_crafted_callback(
    h: Harness, action: str, args: dict[str, object]
) -> None:
    before = await client(h)
    await h.press(cb(C, action, **args), user=SUPPORT)  # type: ignore[arg-type]
    answer = h.session.of(AnswerCallbackQuery)[-1]
    assert (answer.text, answer.show_alert) == (T.ADM_DENIED, True)
    assert await client(h) == before
    assert not await actions(h)


async def test_support_cannot_open_the_dashboard(h: Harness) -> None:
    await h.say("/stats", user=SUPPORT)
    assert h.last().text == T.ADM_DENIED
    await h.press(cb(core.DASH), user=SUPPORT)
    assert h.session.of(AnswerCallbackQuery)[-1].text == T.ADM_DENIED


async def test_support_can_do_what_the_role_allows(h: Harness) -> None:
    await h.press(cb(C, "card", i=1), user=SUPPORT)
    assert "Клиент" in plain(h.last().text)
    assert any(text.endswith(T.BTN_CLIENT_BLOCK) for text in labels(h))
    assert not any(text.endswith(T.BTN_CLIENT_DELETE) for text in labels(h))  # кнопок нет
    assert not any(text.endswith(T.BTN_CLIENT_EXTEND) for text in labels(h))
    await h.press(cb(C, "bk!", i=1), user=SUPPORT)
    assert (await client(h)).state is State.SUSPENDED
    assert (await actions(h)) == ["client.block"]


async def test_the_owner_sees_every_client_action(h: Harness) -> None:
    await h.press(cb(C, "card", i=1), user=ADMIN)
    for title in (
        T.BTN_CLIENT_EXTEND,
        T.BTN_CLIENT_PLAN,
        T.BTN_CLIENT_BONUS,
        T.BTN_CLIENT_NOTE,
        T.BTN_CLIENT_MESSAGE,
        T.BTN_CLIENT_BLOCK,
        T.BTN_CLIENT_DELETE,
    ):
        assert any(text.endswith(title) for text in labels(h)), title


async def test_a_stale_admin_button_is_explained(h: Harness) -> None:
    await h.press("a:zzz:???", user=ADMIN)
    answer = h.session.of(AnswerCallbackQuery)[-1]
    assert (answer.text, answer.show_alert) == (T.ADM_STALE, True)


async def test_all_admin_callbacks_fit_into_64_bytes() -> None:
    longest = cb(C, "pld", i=2**40, p=-999_999, x="gb_1234567890ab")
    assert len(longest.encode()) <= 64


# --- дашборд ---


async def test_dashboard_for_the_owner(h: Harness) -> None:
    await h.say("/stats", user=ADMIN)
    text = plain(h.last().text)
    assert "Дашборд" in text and "Клиенты по тарифам" in text
    assert "50 ГБ — 1" in text
    assert "Новых за неделю: 1" in text
    assert "Продано: 50 ГБ из 1000 ГБ (5%)" in text
    assert "node1: онлайн" in text
    assert "Задачи на нодах: ждут 0, упали 0" in text


# --- список и поиск ---


async def test_the_client_list_pages_and_labels(h: Harness) -> None:
    for tg_id in range(100, 120):
        await add_user(h.db, tg_id)
        await add_client(h.db, tg_id, 1)
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE users SET username = 'anna' WHERE tg_id = 119")
    await h.say("/clients", user=ADMIN)
    text = plain(h.last().text)
    assert "Всего: 21" in text
    rows = [name for name in labels(h) if "u10" in name]
    assert len(rows) == 8 and "@anna" in rows[0]
    assert "1 из 3" in " ".join(labels(h))

    await h.press(cb(C, "list", p=1), user=ADMIN)
    assert "2 из 3" in " ".join(labels(h))
    await h.press(cb(C, "list", p=99), user=ADMIN)  # страницы нет: показывается последняя
    assert "3 из 3" in " ".join(labels(h))


async def test_search_by_login_id_and_username(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE users SET username = 'anna' WHERE tg_id = 5")
    await h.press(cb(C, "find"), user=ADMIN)
    assert "логин" in plain(h.last().text)
    for query in ("u1001", "5", "@anna"):
        await h.press(cb(C, "find"), user=ADMIN)
        await h.say(query, user=ADMIN)
        assert "Клиент u1001" in plain(h.last().text), query
    await h.press(cb(C, "find"), user=ADMIN)
    await h.say("u9999", user=ADMIN)
    assert T.CLIENT_NOT_FOUND in plain(h.last().text)


async def test_client_command(h: Harness) -> None:
    await h.say("/client u1001", user=ADMIN)
    text = plain(h.last().text)
    assert "Клиент u1001" in text and "tg_id: 5" in text and "Тариф: 50 ГБ" in text
    await h.say("/client", user=ADMIN)
    assert "Пришлите логин" in plain(h.last().text)
    await h.say("/client nobody", user=ADMIN)
    assert T.CLIENT_NOT_FOUND in plain(h.last().text)


async def test_the_card_shows_the_quota_bar_the_note_and_the_history(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await record_sample(conn, 1, NOW, 10 * GIB, 50 * GIB)
        await conn.execute(
            "UPDATE clients SET admin_note = ?, bonus_gb = 5 WHERE id = 1",
            ("<b>жирный</b> & тихий",),
        )
        await conn.execute(
            "INSERT INTO notifications (client_id, kind, dedupe_key, sent_at)"
            " VALUES (1, 'alert:drop', 'k', ?)",
            (NOW,),
        )
        await conn.execute(
            "INSERT INTO payments (tg_id, plan_id, stars, telegram_payment_charge_id, at)"
            " VALUES (5, 'gb50', 50, 'c1', ?)",
            (NOW,),
        )
    await h.press(cb(C, "card", i=1), user=ADMIN)
    raw, text = h.last().text, plain(h.last().text)
    assert "<code>▰▰▱▱▱▱▱▱▱▱ 20%</code>" in raw
    assert "Бонус: +5 ГБ к тарифу" in text
    assert "&lt;b&gt;жирный&lt;/b&gt; &amp; тихий" in raw  # заметка экранирована
    assert "Резкое уменьшение" in text  # история уведомлений читается по-человечески
    assert "50 Stars" in text


# --- действия над клиентом ---


async def test_extend_by_a_button_tells_the_client(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE clients SET expires_at = ? WHERE id = 1", (NOW + 5 * DAY,))
    await h.press(cb(C, "card", i=1), user=ADMIN)
    await press_button(h, T.BTN_CLIENT_EXTEND)
    assert "На сколько дней" in plain(h.last().text)
    await press_button(h, "7 дн.")
    assert "Доступ клиента u1001 продлён на 7 дн." in plain(h.last().text)
    assert (await client(h)).expires_at == NOW + 12 * DAY
    assert "продлил доступ на 7 дн." in plain(h.messenger.sent[-1][1])
    assert h.messenger.sent[-1][0] == USER.id
    assert await actions(h) == ["client.extend"]


async def test_extend_with_a_typed_number_and_a_bad_one(h: Harness) -> None:
    await h.press(cb(C, "ex", i=1), user=ADMIN)
    await h.say("abc", user=ADMIN)
    assert "Нужно число дней" in plain(h.last().text)
    await h.say("45", user=ADMIN)
    assert "продлён на 45 дн." in plain(h.last().text)
    assert await h.fsm_state(ADMIN) is None


async def test_plan_change_lists_other_plans_and_applies_one(h: Harness) -> None:
    await h.press(cb(C, "pl", i=1), user=ADMIN)
    offered = [name for name in labels(h) if "ГБ" in name]
    assert [name.split(" ", 1)[-1] for name in offered] == [
        "100 ГБ",
        "200 ГБ",
    ]  # текущий не предлагается
    await press_button(h, "100 ГБ")
    assert "переведён на тариф 100 ГБ" in plain(h.last().text)
    fresh = await client(h)
    assert (fresh.plan_id, fresh.quota_gb) == ("gb100", 100)
    assert "тариф 100 ГБ" in plain(h.messenger.sent[-1][1])
    assert h.kicks  # resize отправлен на ноду без ожидания планировщика


async def test_plan_change_for_a_beta_client_points_to_bonus_gb(h: Harness) -> None:
    await add_user(h.db, 6)
    beta = await add_client(h.db, 6, 1, plan_id="beta", quota_gb=10)
    await h.press(cb(C, "pl", i=beta), user=ADMIN)
    assert "бонусные ГБ" in plain(h.last().text)


async def test_bonus_gb_by_buttons_typed_numbers_and_reset(h: Harness) -> None:
    await h.press(cb(C, "bn", i=1), user=ADMIN)
    await press_button(h, "+10 ГБ")
    assert "Бонус клиента u1001: +10 ГБ, всего 10 ГБ" in plain(h.last().text)
    assert (await client(h)).quota_gb == 60
    assert "+10 ГБ" in plain(h.messenger.sent[-1][1])

    await h.press(cb(C, "bn", i=1), user=ADMIN)
    assert any(name.endswith(T.BTN_BONUS_RESET) for name in labels(h))
    await h.say("-4", user=ADMIN)
    assert (await client(h)).bonus_gb == 6
    await h.press(cb(C, "bn", i=1), user=ADMIN)
    await press_button(h, T.BTN_BONUS_RESET)
    assert (await client(h)).bonus_gb == 0
    await h.press(cb(C, "bn", i=1), user=ADMIN)
    await h.say("0", user=ADMIN)
    assert "Нужно ненулевое число" in plain(h.last().text)
    await h.say("-5", user=ADMIN)  # бонус уже ноль: ниже нуля нельзя
    assert T.BONUS_NEGATIVE in plain(h.last().text)


async def test_blocking_needs_a_red_confirmation_and_tells_the_client(h: Harness) -> None:
    await h.press(cb(C, "bk", i=1), user=ADMIN)
    assert "Заблокировать клиента u1001" in plain(h.last().text)
    confirm = button(h, T.BTN_ADM_CONFIRM)
    assert confirm.style == "danger"  # красная кнопка
    assert (await client(h)).state is State.ACTIVE  # пока ничего не сделано
    back = button(h, T.BTN_BACK)
    assert back.callback_data == cb(C, "card", i=1)  # «Назад» отменяет

    await h.press(confirm.callback_data, user=ADMIN)
    assert (await client(h)).state is State.SUSPENDED
    assert "заблокирован" in plain(h.last().text)
    assert plain(h.messenger.sent[-1][1]) == T.CLIENT_NOTICE_SUSPENDED
    assert h.kicks

    await press_button(h, T.BTN_CLIENT_UNBLOCK)
    assert (await client(h)).state is State.ACTIVE
    assert plain(h.messenger.sent[-1][1]) == T.CLIENT_NOTICE_RESUMED
    assert await actions(h) == ["client.block", "client.unblock"]


async def test_deleting_needs_confirmation_and_cannot_be_repeated(h: Harness) -> None:
    await h.press(cb(C, "dl", i=1), user=ADMIN)
    assert "Данные на ноде будут удалены" in plain(h.last().text)
    assert button(h, T.BTN_ADM_CONFIRM).style == "danger"
    assert (await client(h)).state is State.ACTIVE
    confirm = button(h, T.BTN_ADM_CONFIRM).callback_data
    await h.press(confirm, user=ADMIN)
    assert (await client(h)).state is State.DELETED
    assert plain(h.messenger.sent[-1][1]) == T.CLIENT_NOTICE_DELETED
    async with h.db.read() as conn:
        async with conn.execute("SELECT op FROM provision_jobs") as cur:
            assert [Op(r[0]) for r in await cur.fetchall()] == [Op.DELETE]
    await h.press(confirm, user=ADMIN)  # повторное нажатие
    assert T.ADM_STATE_CHANGED in plain(h.last().text)
    assert await actions(h) == ["client.delete"]


async def test_notes(h: Harness) -> None:
    await h.press(cb(C, "nt", i=1), user=ADMIN)
    await h.say("платит вручную <каждый месяц>", user=ADMIN)
    assert T.NOTE_SAVED in plain(h.last().text)
    assert "платит вручную &lt;каждый месяц&gt;" in h.last().text
    await h.press(cb(C, "nt", i=1), user=ADMIN)
    await h.say("-", user=ADMIN)
    assert T.NOTE_CLEARED in plain(h.last().text)
    assert (await client(h)).admin_note is None
    await h.press(cb(C, "nt", i=1), user=ADMIN)
    await h.say("я" * 501, user=ADMIN)
    assert "не длиннее 500" in plain(h.last().text)


async def test_a_message_to_the_client_goes_out_from_the_bot_and_is_logged_without_the_text(
    h: Harness,
) -> None:
    await h.press(cb(C, "ms", i=1), user=SUPPORT)  # помощнику это можно
    await h.say("Здравствуйте, <тест> & проверка", user=SUPPORT)
    assert T.MESSAGE_SENT in plain(h.last().text)
    tg_id, text = h.messenger.sent[-1]
    assert tg_id == USER.id
    assert "Сообщение от администратора" in plain(text)
    assert "&lt;тест&gt; &amp; проверка" in text
    async with h.db.read() as conn:
        [entry] = await audit_repo.page(conn, limit=5, offset=0)
    assert entry.action == "client.message" and entry.actor_role == "support"
    assert entry.details == {"delivered": True, "length": 31}
    assert "тест" not in str(entry)


async def test_cancel_clears_an_unfinished_input(h: Harness) -> None:
    await h.press(cb(C, "find"), user=ADMIN)
    assert await h.fsm_state(ADMIN) is not None
    await h.say("/cancel", user=ADMIN)
    assert await h.fsm_state(ADMIN) is None
    assert T.ADM_CANCELLED in plain(h.last().text)


async def test_requeue_button_for_a_failed_job(h: Harness) -> None:
    from plugbackup_bot.db import jobs as jobs_repo

    async with h.db.transaction() as conn:
        failed = await jobs_repo.enqueue(conn, 1, Op.RESIZE, NOW)
        await jobs_repo.mark_failed(conn, failed, NOW, "quota_below_usage")
    await h.press(cb(C, "card", i=1), user=ADMIN)
    assert "quota_below_usage" in plain(h.last().text)
    await press_button(h, f"Повторить #{failed}")
    assert h.session.of(AnswerCallbackQuery)[-1].text == T.ADM_DONE
    assert h.kicks


async def test_other_users_do_not_slip_into_the_admin_flows(h: Harness) -> None:
    await h.press(cb(C, "find"), user=ADMIN)  # админ ждёт запрос
    await h.say("u1001", user=OTHER)  # чужой текст админской машиной состояний не обрабатывается
    assert "Клиент" not in plain(h.last().text)
