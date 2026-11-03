import pytest
import pytest_asyncio

from fakes import FakeClock
from harness import ADMIN, OTHER, USER, Harness
from helpers import NOW, add_client, add_user, fake_key_body
from plugbackup_bot.db import Database
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.clients import get_client
from plugbackup_bot.db.jobs import JobStatus, Op
from plugbackup_bot.db.usage import record_sample
from plugbackup_bot.domain.plans import GIB
from plugbackup_bot.domain.states import State
from plugbackup_bot.services.admin import MAX_INVITES_AT_ONCE, ActionKind
from plugbackup_bot.tg import admin_texts

KEY = fake_key_body(0)


@pytest_asyncio.fixture
async def h(db: Database, clock: FakeClock) -> Harness:
    harness = await Harness.build(db, clock)
    await make(harness, USER.id)
    return harness


async def make(h: Harness, tg_id: int, state: str = "active", key: str | None = KEY, **cols) -> int:  # type: ignore[no-untyped-def]
    await add_user(h.db, tg_id)
    client_id = await add_client(h.db, tg_id, 1, state=state, ssh_key_body=key)
    if cols:
        assignments = ", ".join(f"{name} = ?" for name in cols)
        async with h.db.transaction() as conn:
            await conn.execute(
                f"UPDATE clients SET {assignments} WHERE id = ?",  # noqa: S608
                (*cols.values(), client_id),
            )
    return client_id


async def client(h: Harness, client_id: int = 1):  # type: ignore[no-untyped-def]
    async with h.db.read() as conn:
        found = await get_client(conn, client_id)
    assert found is not None
    return found


async def jobs(h: Harness, client_id: int = 1) -> list[tuple[Op, JobStatus]]:
    async with h.db.read() as conn:
        return [(j.op, j.status) for j in await jobs_repo.list_for_client(conn, client_id)]


# --- поиск и карточка ---


async def test_find_client(h: Harness) -> None:
    deleted = await make(h, 6, state="deleted")
    assert (await h.admin.find_client("u1001")).id == 1  # type: ignore[union-attr]
    assert (await h.admin.find_client("  u1001 ")).id == 1  # type: ignore[union-attr]
    assert (await h.admin.find_client("5")).id == 1  # type: ignore[union-attr]
    assert (await h.admin.find_client("6")).id == deleted  # type: ignore[union-attr]
    for query in ("u9999", "u1001;ls", "abc", "", "777"):
        assert await h.admin.find_client(query) is None


async def test_card(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await record_sample(conn, 1, NOW, 5 * GIB, 50 * GIB)
        for number in range(7):
            await conn.execute(
                "INSERT INTO payments (tg_id, plan_id, stars, telegram_payment_charge_id, at)"
                " VALUES (5, 'gb50', 50, ?, ?)",
                (f"c{number}", NOW + number),
            )
        await jobs_repo.enqueue(conn, 1, Op.RESIZE, NOW)
        failed = await jobs_repo.enqueue(conn, 1, Op.SUSPEND, NOW)
        await jobs_repo.mark_failed(conn, failed, NOW, "quota_below_usage: занято больше")
    card = await h.admin.card(1)
    assert card is not None
    assert card.node is not None
    assert card.node.name == "node1"
    assert card.sample is not None
    assert len(card.payments) == 5
    assert card.payments[0].at == NOW + 6
    assert [(j.op, j.status.value) for j in card.open_jobs] == [
        (Op.RESIZE, "pending"),
        (Op.SUSPEND, "failed"),
    ]
    assert await h.admin.card(99) is None

    text = admin_texts.client_card(card, h.access._catalog, "Europe/Moscow")
    assert "Клиент <code>u1001</code>" in text
    assert "tg_id: <code>5</code>" in text
    assert "Состояние: активно" in text
    assert "Занято: 5,0 ГБ" in text
    assert "#2 suspend — упала" in text
    assert "quota_below_usage" in text
    labels = [b.text for row in admin_texts.client_buttons(card).inline_keyboard for b in row]
    assert labels == ["Заблокировать", "Удалить", "Повторить #2"]


# --- блокировка, возврат, удаление ---


async def test_suspend_and_resume(h: Harness) -> None:
    result = await h.admin.suspend(1, ADMIN.id)
    assert result.kind is ActionKind.OK
    suspended = await client(h)
    assert (suspended.state, suspended.admin_hold) == (State.SUSPENDED, True)
    assert await jobs(h) == [(Op.SUSPEND, JobStatus.PENDING)]
    async with (
        h.db.read() as conn,
        conn.execute(
            "SELECT actor, reason FROM state_log WHERE client_id = 1 ORDER BY id DESC LIMIT 1"
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert (row["actor"], row["reason"]) == ("admin:900", "admin_suspend")

    assert (await h.admin.suspend(1, ADMIN.id)).kind is ActionKind.WRONG_STATE

    resumed = await h.admin.resume(1, ADMIN.id)
    assert resumed.kind is ActionKind.OK
    fresh = await client(h)
    assert (fresh.state, fresh.admin_hold) == (State.ACTIVE, False)
    assert [op for op, _ in await jobs(h)] == [Op.SUSPEND, Op.RESUME]
    assert (await h.admin.resume(1, ADMIN.id)).kind is ActionKind.WRONG_STATE


async def test_actions_on_unknown_or_pending_clients(h: Harness) -> None:
    assert (await h.admin.suspend(99, ADMIN.id)).kind is ActionKind.NOT_FOUND
    assert (await h.admin.resume(99, ADMIN.id)).kind is ActionKind.NOT_FOUND
    assert (await h.admin.delete(99, ADMIN.id)).kind is ActionKind.NOT_FOUND
    pending = await make(h, 7, state="pending", key=None)
    assert (await h.admin.suspend(pending, ADMIN.id)).kind is ActionKind.WRONG_STATE


async def test_delete_stops_the_subscription_and_queues_the_node_cleanup(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute(
            "UPDATE clients SET sub_charge_id = 'charge-1', renewal_on = 1 WHERE id = 1"
        )
    assert (await h.admin.delete(1, ADMIN.id)).kind is ActionKind.OK
    assert (await client(h)).state is State.DELETED
    assert await jobs(h) == [(Op.DELETE, JobStatus.PENDING)]
    assert h.stars.calls == [(5, "charge-1", True)]
    assert (await h.admin.delete(1, ADMIN.id)).kind is ActionKind.WRONG_STATE


async def test_delete_without_subscription_or_key(h: Harness) -> None:
    pending = await make(h, 7, state="pending", key=None)
    await h.admin.delete(pending, ADMIN.id)
    assert await jobs(h, pending) == []  # на ноде клиента не было
    assert h.stars.calls == []
    keyed = await make(h, 8, state="suspended", renewal_on=0, sub_charge_id="c")
    await h.admin.delete(keyed, ADMIN.id)
    assert await jobs(h, keyed) == [(Op.DELETE, JobStatus.PENDING)]
    assert h.stars.calls == []  # продление уже выключено


async def test_requeue(h: Harness) -> None:
    async with h.db.transaction() as conn:
        failed = await jobs_repo.enqueue(conn, 1, Op.ADD, NOW)
        pending = await jobs_repo.enqueue(conn, 1, Op.RESIZE, NOW)
        await jobs_repo.mark_failed(conn, failed, NOW, "x")
    assert await h.admin.requeue_job(failed) is True
    assert await h.admin.requeue_job(pending) is False
    assert await h.admin.requeue_job(99) is False
    assert await jobs(h) == [(Op.ADD, JobStatus.PENDING), (Op.RESIZE, JobStatus.PENDING)]


# --- инвайты, ноды, работы ---


async def test_invites(h: Harness) -> None:
    codes = await h.admin.create_invites(3, ADMIN.id)
    assert len(set(codes)) == 3
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 3
    for bad in (0, -1, MAX_INVITES_AT_ONCE + 1):
        with pytest.raises(ValueError):
            await h.admin.create_invites(bad, ADMIN.id)


async def test_node_load_counts_everything_but_deleted(h: Harness) -> None:
    await make(h, 6, state="suspended")
    await make(h, 7, state="pending", key=None)
    await make(h, 8, state="deleted")
    [load] = await h.admin.nodes_overview()
    assert load.sold_gb == 150
    assert load.free_gb == 1000 - 150


async def test_maintenance_events(h: Harness) -> None:
    by_node = await h.admin.start_maintenance("node1", "  перезагрузка  ")
    everywhere = await h.admin.start_maintenance("all", "x" * 900)
    assert await h.admin.start_maintenance("nope", "текст") is None
    with pytest.raises(ValueError):
        await h.admin.start_maintenance("all", "   ")
    async with (
        h.db.read() as conn,
        conn.execute("SELECT id, node_id, text FROM service_events ORDER BY id") as cur,
    ):
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows[0] == (by_node, 1, "перезагрузка")
    assert rows[1][1] is None
    assert len(rows[1][2]) == 500
    assert await h.admin.end_maintenance(by_node) is True  # type: ignore[arg-type]
    assert await h.admin.end_maintenance(by_node) is False  # type: ignore[arg-type]
    assert everywhere is not None


# --- команды в боте ---


@pytest.mark.parametrize(
    "command", ["/stats", "/nodes", "/invite 3", "/client u1001", "/broadcast", "/maint all x"]
)
async def test_commands_are_silent_for_everyone_but_admins(h: Harness, command: str) -> None:
    await h.say(command, user=OTHER)
    await h.say(command)
    assert h.texts() == []
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 0


async def test_stats_command(h: Harness) -> None:
    await h.say("/stats", user=ADMIN)
    text = h.last().text
    assert text.startswith("Статистика")
    assert "Клиенты по тарифам:" in text
    assert "• 50 ГБ — 1" in text
    assert "node1" in text


async def test_nodes_command(h: Harness) -> None:
    await h.say("/nodes", user=ADMIN)
    assert "node1 — 192.0.2.10 → node1.example.org" in h.last().text
    assert ".env" in h.last().text


async def test_invite_command(h: Harness) -> None:
    await h.say("/invite 3", user=ADMIN)
    assert h.last().text.count("<code>") == 3
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 3
    for bad in ("/invite", "/invite 0", "/invite 51", "/invite abc", "/invite 2 3"):
        await h.say(bad, user=ADMIN)
        assert "Использование: /invite N" in h.last().text
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 3


async def test_client_command(h: Harness) -> None:
    await h.say("/client", user=ADMIN)
    assert h.last().text.startswith("Использование: /client")
    await h.say("/client nobody", user=ADMIN)
    assert h.last().text.startswith("Клиент не найден")
    await h.say("/client u1001", user=ADMIN)
    assert h.last().text.startswith("Клиент <code>u1001</code>")
    assert [d for _, d, _ in h.last_buttons()] == ["ac:ask:s:1", "ac:ask:d:1"]
    await h.say("/client 5", user=ADMIN)
    assert h.last().text.startswith("Клиент <code>u1001</code>")


async def test_suspend_needs_confirmation_and_tells_the_client(h: Harness) -> None:
    await h.press("ac:ask:s:1", user=ADMIN)
    assert h.last().text == "Заблокировать: клиент u1001? Подтвердите."
    assert [d for _, d, _ in h.last_buttons()] == ["ac:do:s:1", "ac:no"]
    assert (await client(h)).state is State.ACTIVE  # пока ничего не изменилось

    await h.press("ac:do:s:1", user=ADMIN)
    assert (await client(h)).state is State.SUSPENDED
    assert h.messenger.sent[-1] == (USER.id, admin_texts.CLIENT_SUSPENDED)
    assert h.last().text == "Готово: заблокировать, u1001."
    assert h.kicks == [1]

    await h.press("ac:do:s:1", user=ADMIN)
    assert h.last().text.startswith("Сейчас это действие недоступно")


async def test_resume_and_delete_notify_the_client(h: Harness) -> None:
    await h.press("ac:do:s:1", user=ADMIN)
    await h.press("ac:do:r:1", user=ADMIN)
    assert (await client(h)).state is State.ACTIVE
    assert h.messenger.sent[-1] == (USER.id, admin_texts.CLIENT_RESUMED)
    await h.press("ac:do:d:1", user=ADMIN)
    assert (await client(h)).state is State.DELETED
    assert h.messenger.sent[-1] == (USER.id, admin_texts.CLIENT_DELETED)


async def test_admin_buttons_ignore_everyone_else(h: Harness) -> None:
    await h.press("ac:do:d:1")
    await h.press("ac:do:d:1", user=OTHER)
    assert (await client(h)).state is State.ACTIVE
    assert h.messenger.sent == []


async def test_cancel_and_garbage_callbacks(h: Harness) -> None:
    await h.press("ac:no", user=ADMIN)
    assert h.callback_answers()[-1].text == "Отменено."
    await h.press("ac:do:x:1", user=ADMIN)
    await h.press("ac:ask:d:999", user=ADMIN)
    assert (await client(h)).state is State.ACTIVE


async def test_requeue_button(h: Harness) -> None:
    async with h.db.transaction() as conn:
        failed = await jobs_repo.enqueue(conn, 1, Op.ADD, NOW)
        await jobs_repo.mark_failed(conn, failed, NOW, "x")
    await h.press(f"ac:rq:{failed}", user=ADMIN)
    assert h.callback_answers()[-1].text == "Задача возвращена в очередь."
    assert h.kicks == [1]
    await h.press("ac:rq:999", user=ADMIN)
    assert h.callback_answers()[-1].text == "Задача не найдена."


async def test_broadcast_flow(h: Harness) -> None:
    await add_user(h.db, OTHER.id)
    await add_user(h.db, 7)
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE users SET bot_blocked = 1 WHERE tg_id = 7")

    await h.say("/broadcast", user=ADMIN)
    assert "Пришлите текст рассылки" in h.last().text
    assert await h.fsm_state(ADMIN) is not None

    await h.say("Привет <всем>", user=ADMIN)
    assert h.last().text == "Рассылка получит 3 чел. Текст:\n\nПривет &lt;всем&gt;"
    assert [d for _, d, _ in h.last_buttons()] == ["bc:send", "bc:cancel"]

    await h.press("bc:send", user=ADMIN)
    await h.drain_tasks()
    delivered = [(tg, text) for tg, text in h.messenger.sent if text == "Привет &lt;всем&gt;"]
    assert sorted(tg for tg, _ in delivered) == [USER.id, OTHER.id, ADMIN.id]
    assert h.texts()[-1] == "Рассылка №1 завершена: доставлено 3, не доставлено 0."
    assert await h.fsm_state(ADMIN) is None


async def test_broadcast_can_be_cancelled_or_rejected(h: Harness) -> None:
    await h.say("/broadcast", user=ADMIN)
    await h.say("x" * 4000, user=ADMIN)
    assert "от 1 до 3500" in h.last().text
    await h.say("нормальный текст", user=ADMIN)
    await h.press("bc:cancel", user=ADMIN)
    assert await h.fsm_state(ADMIN) is None
    await h.press("bc:send", user=ADMIN)  # состояния уже нет, рассылки тоже
    assert h.messenger.sent == []

    await h.say("/broadcast", user=ADMIN)
    await h.say("/cancel", user=ADMIN)
    assert await h.fsm_state(ADMIN) is None


async def test_maintenance_commands(h: Harness) -> None:
    await h.say("/maint", user=ADMIN)
    assert h.last().text.startswith("Использование: /maint")
    await h.say("/maint nope текст", user=ADMIN)
    assert h.last().text.startswith("Такой ноды нет")
    await h.say("/maint node1 Работы с 03:00", user=ADMIN)
    assert h.last().text.startswith("Событие №1 создано")
    await h.say("/maint_end 1", user=ADMIN)
    assert h.last().text == "Работы отмечены завершёнными."
    await h.say("/maint_end 1", user=ADMIN)
    assert h.last().text == "Событие не найдено."
