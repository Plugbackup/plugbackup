import pytest
import pytest_asyncio

from fakes import FakeClock
from harness import ADMIN, SUPPORT, USER, Harness
from helpers import NOW, add_client, add_user, fake_key_body
from plugbackup_bot.db import Database
from plugbackup_bot.db import audit as audit_repo
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.clients import get_client
from plugbackup_bot.db.jobs import JobStatus, Op
from plugbackup_bot.db.usage import record_sample
from plugbackup_bot.domain.plans import GIB
from plugbackup_bot.domain.roles import Actor, Role
from plugbackup_bot.domain.states import State
from plugbackup_bot.services.admin import MAX_INVITES_AT_ONCE, ActionKind

KEY = fake_key_body(0)
OWNER = Actor(ADMIN.id, Role.OWNER)
HELPER = Actor(SUPPORT.id, Role.SUPPORT)


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


# --- блокировка, возврат, удаление ---


async def test_suspend_and_resume(h: Harness) -> None:
    result = await h.admin.suspend(1, OWNER)
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

    assert (await h.admin.suspend(1, OWNER)).kind is ActionKind.WRONG_STATE

    resumed = await h.admin.resume(1, OWNER)
    assert resumed.kind is ActionKind.OK
    fresh = await client(h)
    assert (fresh.state, fresh.admin_hold) == (State.ACTIVE, False)
    assert [op for op, _ in await jobs(h)] == [Op.SUSPEND, Op.RESUME]
    assert (await h.admin.resume(1, OWNER)).kind is ActionKind.WRONG_STATE


async def test_actions_on_unknown_or_pending_clients(h: Harness) -> None:
    assert (await h.admin.suspend(99, OWNER)).kind is ActionKind.NOT_FOUND
    assert (await h.admin.resume(99, OWNER)).kind is ActionKind.NOT_FOUND
    assert (await h.admin.delete(99, OWNER)).kind is ActionKind.NOT_FOUND
    pending = await make(h, 7, state="pending", key=None)
    assert (await h.admin.suspend(pending, OWNER)).kind is ActionKind.WRONG_STATE


async def test_delete_stops_the_subscription_and_queues_the_node_cleanup(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute(
            "UPDATE clients SET sub_charge_id = 'charge-1', renewal_on = 1 WHERE id = 1"
        )
    assert (await h.admin.delete(1, OWNER)).kind is ActionKind.OK
    assert (await client(h)).state is State.DELETED
    assert await jobs(h) == [(Op.DELETE, JobStatus.PENDING)]
    assert h.stars.calls == [(5, "charge-1", True)]
    assert (await h.admin.delete(1, OWNER)).kind is ActionKind.WRONG_STATE


async def test_delete_without_subscription_or_key(h: Harness) -> None:
    pending = await make(h, 7, state="pending", key=None)
    await h.admin.delete(pending, OWNER)
    assert await jobs(h, pending) == []  # на ноде клиента не было
    assert h.stars.calls == []
    keyed = await make(h, 8, state="suspended", renewal_on=0, sub_charge_id="c")
    await h.admin.delete(keyed, OWNER)
    assert await jobs(h, keyed) == [(Op.DELETE, JobStatus.PENDING)]
    assert h.stars.calls == []  # продление уже выключено


async def test_requeue(h: Harness) -> None:
    async with h.db.transaction() as conn:
        failed = await jobs_repo.enqueue(conn, 1, Op.ADD, NOW)
        pending = await jobs_repo.enqueue(conn, 1, Op.RESIZE, NOW)
        await jobs_repo.mark_failed(conn, failed, NOW, "x")
    assert await h.admin.requeue_job(failed, OWNER) is True
    assert await h.admin.requeue_job(pending, OWNER) is False
    assert await h.admin.requeue_job(99, OWNER) is False
    assert await jobs(h) == [(Op.ADD, JobStatus.PENDING), (Op.RESIZE, JobStatus.PENDING)]


# --- инвайты, ноды, работы ---


async def test_invites(h: Harness) -> None:
    codes = await h.admin.create_invites(3, OWNER)
    assert len(set(codes)) == 3
    async with h.db.read() as conn:
        assert await invites_repo.count_unused(conn) == 3
    for bad in (0, -1, MAX_INVITES_AT_ONCE + 1):
        with pytest.raises(ValueError):
            await h.admin.create_invites(bad, OWNER)


async def test_node_load_counts_everything_but_deleted(h: Harness) -> None:
    await make(h, 6, state="suspended")
    await make(h, 7, state="pending", key=None)
    await make(h, 8, state="deleted")
    [load] = await h.admin.nodes_overview()
    assert load.sold_gb == 150
    assert load.free_gb == 1000 - 150


async def test_maintenance_events(h: Harness) -> None:
    by_node = await h.admin.start_maintenance("node1", "  перезагрузка  ", OWNER)
    everywhere = await h.admin.start_maintenance("all", "x" * 900, OWNER)
    assert await h.admin.start_maintenance("nope", "текст", OWNER) is None
    with pytest.raises(ValueError):
        await h.admin.start_maintenance("all", "   ", OWNER)
    async with (
        h.db.read() as conn,
        conn.execute("SELECT id, node_id, text FROM service_events ORDER BY id") as cur,
    ):
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows[0] == (by_node, 1, "перезагрузка")
    assert rows[1][1] is None
    assert len(rows[1][2]) == 500
    assert await h.admin.end_maintenance(by_node, OWNER) is True  # type: ignore[arg-type]
    assert await h.admin.end_maintenance(by_node, OWNER) is False  # type: ignore[arg-type]
    assert everywhere is not None


# --- поиск по @username и список ---

DAY = 24 * 3600


async def audit_log(h: Harness) -> list[tuple[str, str | None, dict]]:  # type: ignore[type-arg]
    """Журнал действий от старых к новым: (действие, цель, детали)."""
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=100, offset=0)
    return [(e.action, e.target_id, e.details) for e in reversed(entries)]


async def test_find_client_by_username(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE users SET username = 'anna_k' WHERE tg_id = 5")
    for query in ("@anna_k", "@Anna_K", "anna_k"):
        assert (await h.admin.find_client(query)).id == 1  # type: ignore[union-attr]
    assert await h.admin.find_client("@nobody") is None
    assert await h.admin.find_client("@") is None


async def test_the_username_is_remembered_when_a_user_writes(h: Harness) -> None:
    from aiogram.types import User

    await h.say("/help", user=User(id=5, is_bot=False, first_name="Т", username="Anna_K"))
    assert (await h.admin.find_client("@anna_k")).id == 1  # type: ignore[union-attr]
    # человек убрал username: поиск по старому больше не находит
    await h.say("/help", user=User(id=5, is_bot=False, first_name="Т"))
    assert await h.admin.find_client("@anna_k") is None


async def test_the_client_list_is_newest_first_and_deleted_last(h: Harness) -> None:
    second = await make(h, 6)
    gone = await make(h, 7, state="deleted")
    third = await make(h, 8)
    rows, total = await h.admin.list_clients(limit=10, offset=0)
    assert total == 4
    assert [row.id for row in rows] == [third, second, 1, gone]
    assert rows[0].login == f"u{1000 + third}"
    page, _ = await h.admin.list_clients(limit=2, offset=2)
    assert [row.id for row in page] == [1, gone]


# --- заметка ---


async def test_notes_are_saved_cleared_and_limited(h: Harness) -> None:
    saved = await h.admin.set_note(1, OWNER, "  платит вручную  ")
    assert saved.kind is ActionKind.OK and saved.client.admin_note == "платит вручную"  # type: ignore[union-attr]
    assert (await h.admin.set_note(1, OWNER, "")).client.admin_note is None  # type: ignore[union-attr]
    assert (await h.admin.set_note(1, OWNER, "x" * 501)).kind is ActionKind.INVALID
    assert (await h.admin.set_note(99, OWNER, "x")).kind is ActionKind.NOT_FOUND
    log = await audit_log(h)
    assert [a for a, _, _ in log] == ["client.note", "client.note"]
    assert "платит" not in str(log)  # текст заметки в журнал не попадает


# --- продление ---


async def test_extend_a_paid_client_adds_days_to_the_period(h: Harness) -> None:
    await make(h, 6, expires_at=NOW + 5 * DAY, renewal_on=1)
    result = await h.admin.extend(2, OWNER, 7)
    assert result.kind is ActionKind.OK
    assert result.client.expires_at == NOW + 12 * DAY  # type: ignore[union-attr]
    assert (await audit_log(h))[-1] == ("client.extend", "2", {"days": 7, "until": NOW + 12 * DAY})


async def test_extend_an_expiring_client_counts_from_now_and_returns_it_to_work(h: Harness) -> None:
    expiring = await make(h, 6, state="expiring", expires_at=NOW - 2 * DAY)
    result = await h.admin.extend(expiring, OWNER, 30)
    fresh = await client(h, expiring)
    assert result.kind is ActionKind.OK
    assert (fresh.state, fresh.expires_at) == (State.ACTIVE, NOW + 30 * DAY)
    assert await jobs(h, expiring) == []  # на ноде ничего менять не нужно
    async with (
        h.db.read() as conn,
        conn.execute(
            "SELECT actor, reason FROM state_log WHERE client_id = ? ORDER BY id DESC LIMIT 1",
            (expiring,),
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None and (row["actor"], row["reason"]) == ("admin:900", "admin_extend")


async def test_extend_wakes_up_a_lapsed_suspended_client(h: Harness) -> None:
    lapsed = await make(h, 6, state="suspended", expires_at=NOW - 20 * DAY)
    assert (await h.admin.extend(lapsed, OWNER, 30)).kind is ActionKind.OK
    assert (await client(h, lapsed)).state is State.ACTIVE
    assert await jobs(h, lapsed) == [(Op.RESUME, JobStatus.PENDING)]


async def test_extend_refuses_manually_blocked_pending_and_unknown_clients(h: Harness) -> None:
    held = await make(h, 6, state="suspended", admin_hold=1)
    pending = await make(h, 7, state="pending", key=None)
    assert (await h.admin.extend(held, OWNER, 7)).kind is ActionKind.WRONG_STATE
    assert (await h.admin.extend(pending, OWNER, 7)).kind is ActionKind.WRONG_STATE
    assert (await h.admin.extend(99, OWNER, 7)).kind is ActionKind.NOT_FOUND
    assert not await audit_log(h)


@pytest.mark.parametrize("days", [0, -3, 366])
async def test_extend_rejects_silly_numbers(h: Harness, days: int) -> None:
    assert (await h.admin.extend(1, OWNER, days)).kind is ActionKind.INVALID


async def test_extend_a_beta_client_moves_the_next_question(h: Harness) -> None:
    beta = await make(h, 6, plan_id="beta", confirm_due_at=NOW + 2 * DAY)
    assert (await h.admin.extend(beta, OWNER, 10)).kind is ActionKind.OK
    assert (await client(h, beta)).confirm_due_at == NOW + 12 * DAY


# --- смена тарифа ---


async def test_change_plan_resizes_and_keeps_the_bonus(h: Harness) -> None:
    async with h.db.transaction() as conn:
        await conn.execute("UPDATE clients SET bonus_gb = 10, quota_gb = 60 WHERE id = 1")
    result = await h.admin.change_plan(1, OWNER, "gb100")
    assert result.kind is ActionKind.OK
    fresh = await client(h)
    assert (fresh.plan_id, fresh.quota_gb, fresh.bonus_gb) == ("gb100", 110, 10)
    assert await jobs(h) == [(Op.RESIZE, JobStatus.PENDING)]
    assert (await audit_log(h))[-1] == ("client.plan", "1", {"before": "gb50", "after": "gb100"})


async def test_change_plan_refusals(h: Harness) -> None:
    beta = await make(h, 6, plan_id="beta", quota_gb=10)
    pending = await make(h, 7, state="pending", key=None)
    assert (await h.admin.change_plan(1, OWNER, "gb50")).kind is ActionKind.SAME_PLAN
    assert (await h.admin.change_plan(1, OWNER, "beta")).kind is ActionKind.INVALID
    assert (await h.admin.change_plan(1, OWNER, "nope")).kind is ActionKind.INVALID
    assert (await h.admin.change_plan(beta, OWNER, "gb50")).kind is ActionKind.WRONG_STATE
    assert (await h.admin.change_plan(pending, OWNER, "gb100")).kind is ActionKind.WRONG_STATE
    assert (await h.admin.change_plan(99, OWNER, "gb100")).kind is ActionKind.NOT_FOUND
    assert await jobs(h) == []


async def test_change_plan_needs_free_space_and_room_for_the_data(
    db: Database, clock: FakeClock
) -> None:
    small = await Harness.build(db, clock, node_capacity_gb=60)
    await make(small, 5)  # занято 50 из 60
    assert (await small.admin.change_plan(1, OWNER, "gb100")).kind is ActionKind.NO_CAPACITY

    async with db.transaction() as conn:
        await conn.execute("UPDATE clients SET plan_id = 'gb100', quota_gb = 100 WHERE id = 1")
        await conn.execute("UPDATE nodes SET capacity_gb = 1000")
        await record_sample(conn, 1, NOW, 70 * GIB, 100 * GIB)
    assert (await small.admin.change_plan(1, OWNER, "gb50")).kind is ActionKind.TOO_MUCH_DATA
    assert (await client(small)).plan_id == "gb100"  # отказ ничего не меняет


# --- бонусные ГБ ---


async def test_bonus_gb_raises_and_lowers_the_quota(h: Harness) -> None:
    added = await h.admin.grant_bonus(1, OWNER, 10)
    assert added.kind is ActionKind.OK
    fresh = await client(h)
    assert (fresh.bonus_gb, fresh.quota_gb) == (10, 60)
    assert await jobs(h) == [(Op.RESIZE, JobStatus.PENDING)]

    assert (await h.admin.grant_bonus(1, OWNER, -4)).client.quota_gb == 56  # type: ignore[union-attr]
    assert (await h.admin.grant_bonus(1, OWNER, -7)).kind is ActionKind.INVALID  # ниже нуля нельзя
    assert (await client(h)).bonus_gb == 6
    assert (await audit_log(h))[0] == ("client.bonus", "1", {"delta": 10, "total": 10})


@pytest.mark.parametrize("delta", [0, 1001, -1001])
async def test_bonus_gb_rejects_silly_numbers(h: Harness, delta: int) -> None:
    assert (await h.admin.grant_bonus(1, OWNER, delta)).kind is ActionKind.INVALID


async def test_bonus_gb_respects_capacity_and_usage(db: Database, clock: FakeClock) -> None:
    small = await Harness.build(db, clock, node_capacity_gb=60)
    await make(small, 5)
    assert (await small.admin.grant_bonus(1, OWNER, 20)).kind is ActionKind.NO_CAPACITY
    assert (await small.admin.grant_bonus(1, OWNER, 10)).kind is ActionKind.OK
    async with db.transaction() as conn:
        await record_sample(conn, 1, NOW, 58 * GIB, 60 * GIB)
    assert (await small.admin.grant_bonus(1, OWNER, -5)).kind is ActionKind.TOO_MUCH_DATA
    assert (await client(small)).bonus_gb == 10


async def test_bonus_gb_on_a_deleted_client_is_refused(h: Harness) -> None:
    gone = await make(h, 6, state="deleted")
    assert (await h.admin.grant_bonus(gone, OWNER, 5)).kind is ActionKind.WRONG_STATE
    assert (await h.admin.grant_bonus(99, OWNER, 5)).kind is ActionKind.NOT_FOUND


# --- журнал ---


async def test_every_change_is_written_to_the_audit_log_with_the_role(h: Harness) -> None:
    await h.admin.suspend(1, HELPER)
    await h.admin.resume(1, HELPER)
    await h.admin.create_invites(2, OWNER)
    await h.admin.start_maintenance("all", "работы", OWNER)
    log = await audit_log(h)
    assert [a for a, _, _ in log] == [
        "client.block",
        "client.unblock",
        "invite.create",
        "maintenance.start",
    ]
    async with h.db.read() as conn:
        entries = await audit_repo.page(conn, limit=10, offset=0)
    assert [(e.actor_id, e.actor_role) for e in entries[-2:]] == [
        (901, "support"),
        (901, "support"),
    ]
    assert entries[0].actor_role == "owner" and entries[0].at == NOW


async def test_a_failed_action_leaves_no_audit_entry(h: Harness) -> None:
    await h.admin.suspend(99, OWNER)
    await h.admin.delete(99, OWNER)
    assert await audit_log(h) == []
