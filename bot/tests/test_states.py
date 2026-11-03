import sqlite3
from pathlib import Path

import pytest

from helpers import NOW, add_client, add_node, add_user
from plugbackup_bot.db import Database
from plugbackup_bot.db.clients import (
    create_client,
    get_client,
    get_client_by_login,
    get_live_client,
    list_by_state,
    set_fields,
    transition,
)
from plugbackup_bot.domain.plans import Plan
from plugbackup_bot.domain.states import (
    ALLOWED,
    InvalidTransition,
    StaleState,
    State,
    admin_actor,
    is_allowed,
)

PLAN = Plan(id="gb50", name="50 ГБ", gb=50, price_rub=79, stars=50)

EXPECTED = {
    (State.PENDING, State.ACTIVE),
    (State.PENDING, State.DELETED),
    (State.ACTIVE, State.EXPIRING),
    (State.ACTIVE, State.SUSPENDED),
    (State.EXPIRING, State.ACTIVE),
    (State.EXPIRING, State.SUSPENDED),
    (State.SUSPENDED, State.ACTIVE),
    (State.SUSPENDED, State.DELETED),
}


async def make(db: Database, state: State, tg_id: int = 1) -> int:
    await add_user(db, tg_id)
    node_id = await add_node(db, name=f"n{tg_id}-{state.value}")
    return await add_client(db, tg_id, node_id, state=state.value)


def test_rules_table_matches_the_plan() -> None:
    assert {(src, dst) for src, targets in ALLOWED.items() for dst in targets} == EXPECTED


@pytest.mark.parametrize("src", list(State))
@pytest.mark.parametrize("dst", list(State))
async def test_transition_matrix(db: Database, src: State, dst: State) -> None:
    client_id = await make(db, src)
    if (src, dst) in EXPECTED:
        async with db.transaction() as conn:
            previous = await transition(conn, client_id, dst, reason="t", actor="system", now=NOW)
        assert previous is src
        async with db.read() as conn:
            client = await get_client(conn, client_id)
        assert client is not None
        assert client.state is dst
    else:
        with pytest.raises(InvalidTransition):
            async with db.transaction() as conn:
                await transition(conn, client_id, dst, reason="t", actor="system", now=NOW)
        async with db.read() as conn:
            client = await get_client(conn, client_id)
        assert client is not None
        assert client.state is src


async def test_transition_is_logged(db: Database) -> None:
    client_id = await make(db, State.ACTIVE)
    async with db.transaction() as conn:
        await transition(
            conn, client_id, State.EXPIRING, reason="renewal_off", actor="system", now=NOW + 5
        )
    async with db.read() as conn:
        client = await get_client(conn, client_id)
        async with conn.execute(
            "SELECT from_state, to_state, reason, actor, at FROM state_log WHERE client_id = ?",
            (client_id,),
        ) as cur:
            rows = [tuple(row) for row in await cur.fetchall()]
    assert rows == [("active", "expiring", "renewal_off", "system", NOW + 5)]
    assert client is not None
    assert client.state_since == NOW + 5


@pytest.mark.parametrize("src", [State.PENDING, State.ACTIVE, State.EXPIRING, State.SUSPENDED])
async def test_admin_can_delete_from_any_live_state(db: Database, src: State) -> None:
    client_id = await make(db, src)
    async with db.transaction() as conn:
        await transition(
            conn, client_id, State.DELETED, reason="admin", actor=admin_actor(7), now=NOW
        )
    async with db.read() as conn:
        client = await get_client(conn, client_id)
    assert client is not None
    assert client.state is State.DELETED


@pytest.mark.parametrize("src", [State.ACTIVE, State.EXPIRING])
async def test_system_cannot_skip_suspension_before_delete(db: Database, src: State) -> None:
    client_id = await make(db, src)
    with pytest.raises(InvalidTransition):
        async with db.transaction() as conn:
            await transition(conn, client_id, State.DELETED, reason="t", actor="system", now=NOW)


def test_deleted_is_final_even_for_admin() -> None:
    admin = admin_actor(1)
    assert not any(is_allowed(State.DELETED, dst, admin) for dst in State)


async def test_stale_expectation_is_refused(db: Database) -> None:
    client_id = await make(db, State.EXPIRING)
    with pytest.raises(StaleState):
        async with db.transaction() as conn:
            await transition(
                conn,
                client_id,
                State.EXPIRING,
                reason="t",
                actor="system",
                now=NOW,
                expected_from=State.ACTIVE,
            )
    assert issubclass(StaleState, InvalidTransition)
    async with db.read() as conn, conn.execute("SELECT COUNT(*) FROM state_log") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


async def test_unknown_client(db: Database) -> None:
    with pytest.raises(LookupError):
        async with db.transaction() as conn:
            await transition(conn, 999, State.ACTIVE, reason="t", actor="system", now=NOW)


async def test_create_client(db: Database) -> None:
    node_id = await add_node(db)
    async with db.transaction() as conn:
        client = await create_client(
            conn, tg_id=5, node_id=node_id, plan=PLAN, now=NOW, reason="payment", actor="system"
        )
    assert client.login == "u1001"
    assert client.state is State.PENDING
    assert (client.plan_id, client.quota_gb, client.renewal_on, client.admin_hold) == (
        "gb50",
        50,
        True,
        False,
    )
    async with db.read() as conn, conn.execute("SELECT * FROM state_log") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert (rows[0]["from_state"], rows[0]["to_state"], rows[0]["reason"]) == (
        None,
        "pending",
        "payment",
    )


async def test_second_live_client_for_one_account_is_refused(db: Database) -> None:
    node_id = await add_node(db)
    async with db.transaction() as conn:
        first = await create_client(
            conn, tg_id=5, node_id=node_id, plan=PLAN, now=NOW, reason="t", actor="system"
        )
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction() as conn:
            await create_client(
                conn, tg_id=5, node_id=node_id, plan=PLAN, now=NOW, reason="t", actor="system"
            )
    async with db.transaction() as conn:
        await transition(conn, first.id, State.DELETED, reason="t", actor=admin_actor(1), now=NOW)
        again = await create_client(
            conn, tg_id=5, node_id=node_id, plan=PLAN, now=NOW, reason="t", actor="system"
        )
    assert again.login == "u1002"


async def test_lookups(db: Database) -> None:
    node_id = await add_node(db)
    async with db.transaction() as conn:
        a = await create_client(
            conn, tg_id=1, node_id=node_id, plan=PLAN, now=NOW, reason="t", actor="system"
        )
        b = await create_client(
            conn, tg_id=2, node_id=node_id, plan=PLAN, now=NOW, reason="t", actor="system"
        )
        await transition(conn, b.id, State.ACTIVE, reason="t", actor="system", now=NOW)
    async with db.read() as conn:
        assert (await get_client_by_login(conn, "u1002")) == await get_client(conn, b.id)
        assert await get_client_by_login(conn, "u1002;ls") is None
        assert await get_client_by_login(conn, "u5000") is None
        live = await get_live_client(conn, 1)
        assert live is not None
        assert live.id == a.id
        assert await get_live_client(conn, 99) is None
        assert [c.id for c in await list_by_state(conn, State.ACTIVE)] == [b.id]
        assert [c.id for c in await list_by_state(conn, State.PENDING, State.ACTIVE)] == [
            a.id,
            b.id,
        ]


async def test_set_fields_updates_whitelisted_columns(db: Database) -> None:
    client_id = await make(db, State.ACTIVE)
    async with db.transaction() as conn:
        await set_fields(conn, client_id, expires_at=123, renewal_on=False, sub_charge_id="c-1")
    async with db.read() as conn:
        client = await get_client(conn, client_id)
    assert client is not None
    assert (client.expires_at, client.renewal_on, client.sub_charge_id) == (123, False, "c-1")


@pytest.mark.parametrize("field", ["state", "id", "tg_id", "state_since", "x = 1; --"])
async def test_set_fields_refuses_other_columns(db: Database, field: str) -> None:
    client_id = await make(db, State.ACTIVE)
    with pytest.raises(ValueError):
        async with db.transaction() as conn:
            await set_fields(conn, client_id, **{field: "deleted"})


async def test_set_fields_unknown_client(db: Database) -> None:
    with pytest.raises(LookupError):
        async with db.transaction() as conn:
            await set_fields(conn, 999, expires_at=1)


def test_only_clients_module_writes_state() -> None:
    """Состояние меняется в одном месте: db/clients.py."""
    root = Path(__file__).resolve().parents[1] / "src" / "plugbackup_bot"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if path.name != "clients.py" and "SET state " in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
