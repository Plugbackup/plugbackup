import asyncio
from dataclasses import dataclass

import pytest
import pytest_asyncio

from fakes import FakeClock, FakeStorectl, RecordingAlerter
from helpers import NOW, add_client, add_node, add_user
from plugbackup_bot.db import Database
from plugbackup_bot.db import usage as usage_repo
from plugbackup_bot.db.usage import latest_sample
from plugbackup_bot.domain.plans import GIB
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.storectl import ErrorKind, StorectlError, Usage
from plugbackup_bot.scheduler import Scheduler, Task
from plugbackup_bot.services.usage import UsagePoller

HOUR = 3600
STATES = [State.ACTIVE, State.EXPIRING, State.SUSPENDED, State.PENDING, State.DELETED]


@dataclass
class Env:
    db: Database
    clock: FakeClock
    storectl: FakeStorectl
    alerter: RecordingAlerter
    poller: UsagePoller
    ids: dict[State, int]

    def logins(self) -> dict[State, str]:
        return {state: f"u{1000 + client_id}" for state, client_id in self.ids.items()}


@pytest_asyncio.fixture
async def env(db: Database, clock: FakeClock) -> Env:
    node_id = await add_node(db)
    ids = {}
    for tg_id, state in enumerate(STATES, start=1):
        await add_user(db, tg_id)
        ids[state] = await add_client(db, tg_id, node_id, state=state.value)
    storectl, alerter = FakeStorectl(), RecordingAlerter()
    for client_id in ids.values():
        storectl.usage_by_login[f"u{1000 + client_id}"] = Usage(5 * GIB, 50 * GIB)
    poller = UsagePoller(db, clock, storectl, alerter, concurrency=2)
    return Env(db, clock, storectl, alerter, poller, ids)


async def sample_rows(db: Database) -> list[tuple[int, int, int]]:
    async with (
        db.read() as conn,
        conn.execute(
            "SELECT client_id, hour_ts, used_bytes FROM usage_samples ORDER BY client_id, hour_ts"
        ) as cur,
    ):
        return [tuple(row) for row in await cur.fetchall()]


# --- опрос ---


async def test_only_running_clients_are_polled(env: Env) -> None:
    report = await env.poller.poll()
    expected = {env.ids[State.ACTIVE], env.ids[State.EXPIRING]}
    assert set(report.polled) == expected
    hour = NOW // HOUR * HOUR
    rows = await sample_rows(env.db)
    assert {row[0] for row in rows} == expected
    assert all(row[1] == hour for row in rows)
    async with env.db.read() as conn:
        sample = await latest_sample(conn, env.ids[State.ACTIVE])
    assert sample is not None
    assert (sample.used_bytes, sample.quota_bytes) == (5 * GIB, 50 * GIB)
    assert {call[2] for call in env.storectl.calls} == {
        env.logins()[State.ACTIVE],
        env.logins()[State.EXPIRING],
    }


async def test_a_second_poll_in_the_same_hour_replaces_the_sample(env: Env) -> None:
    await env.poller.poll()
    login = env.logins()[State.ACTIVE]
    env.storectl.usage_by_login[login] = Usage(9 * GIB, 50 * GIB)
    env.clock.advance(5 * 60)  # старт в 08:53, так что остаёмся в том же часе
    await env.poller.poll()
    active = [r for r in await sample_rows(env.db) if r[0] == env.ids[State.ACTIVE]]
    assert [r[2] for r in active] == [9 * GIB]

    env.clock.advance(HOUR)
    await env.poller.poll()
    active = [r for r in await sample_rows(env.db) if r[0] == env.ids[State.ACTIVE]]
    assert len(active) == 2


async def test_unreachable_node_is_counted_and_leaves_no_samples(env: Env) -> None:
    env.storectl.fail("usage", *[StorectlError(ErrorKind.UNREACHABLE, "timeout")] * 2)
    report = await env.poller.poll()
    assert report.polled == []
    assert sum(report.attempts.values()) == 2
    assert sum(report.unreachable.values()) == 2
    assert await sample_rows(env.db) == []
    assert env.alerter.alerts == []


async def test_one_failure_does_not_lose_the_other_sample(env: Env) -> None:
    env.storectl.fail("usage", StorectlError(ErrorKind.TIMEOUT, "slow"))
    report = await env.poller.poll()
    assert len(report.polled) == 1
    assert sum(report.unreachable.values()) == 1
    assert len(await sample_rows(env.db)) == 1


async def test_client_missing_on_the_node_alerts_once_a_day(env: Env) -> None:
    missing = StorectlError(ErrorKind.NOT_FOUND, "no such user")
    env.storectl.fail("usage", missing, missing)
    await env.poller.poll()
    assert len(env.alerter.alerts) == 2  # два клиента — два сообщения
    assert "не найден на ноде node1" in env.alerter.alerts[0]

    env.storectl.fail("usage", missing, missing)
    await env.poller.poll()
    assert len(env.alerter.alerts) == 2  # в тот же день повторов нет

    env.clock.advance(24 * HOUR)
    env.storectl.fail("usage", missing, missing)
    await env.poller.poll()
    assert len(env.alerter.alerts) == 4


async def test_odd_failures_do_not_break_the_poll(env: Env) -> None:
    env.storectl.fail("usage", StorectlError(ErrorKind.BAD_OUTPUT, "garbage"), RuntimeError("boom"))
    report = await env.poller.poll()
    assert report.polled == []
    assert env.alerter.alerts == []


async def test_concurrency_is_limited(db: Database, clock: FakeClock) -> None:
    node_id = await add_node(db)
    storectl = FakeStorectl()
    for tg_id in range(1, 7):
        await add_user(db, tg_id)
        client_id = await add_client(db, tg_id, node_id)
        storectl.usage_by_login[f"u{1000 + client_id}"] = Usage(GIB, 50 * GIB)
    running = peak = 0

    async def slow(op: str) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1

    storectl.on_call = slow
    report = await UsagePoller(db, clock, storectl, RecordingAlerter(), concurrency=2).poll()
    assert len(report.polled) == 6
    assert peak == 2


async def test_old_samples_are_pruned(env: Env) -> None:
    client_id = env.ids[State.ACTIVE]
    async with env.db.transaction() as conn:
        await usage_repo.record_sample(conn, client_id, NOW - 31 * 24 * HOUR, 1, 1)
        await usage_repo.record_sample(conn, client_id, NOW - 29 * 24 * HOUR, 2, 1)
        assert await usage_repo.prune(conn, NOW) == 1
    assert [row[2] for row in await sample_rows(env.db)] == [2]


# --- планировщик ---


class VirtualTime:
    """Подменяет sleep: время не тратится, порядок событий записывается."""

    def __init__(self) -> None:
        self.events: list[tuple[str, float | str]] = []

    async def sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))
        await asyncio.sleep(0)


async def run_until(scheduler: Scheduler, timeout: float = 2) -> None:
    await asyncio.wait_for(scheduler.run(), timeout)


async def test_task_runs_after_its_first_delay_then_on_interval() -> None:
    clock = VirtualTime()
    runs = 0
    holder: list[Scheduler] = []

    async def job() -> None:
        nonlocal runs
        runs += 1
        clock.events.append(("run", runs))
        if runs == 3:
            holder[0].stop()

    scheduler = Scheduler([Task("t", 60, job, first_delay_s=300)], sleep=clock.sleep)
    holder.append(scheduler)
    await run_until(scheduler)
    assert clock.events[:5] == [
        ("sleep", 300),
        ("run", 1),
        ("sleep", 60),
        ("run", 2),
        ("sleep", 60),
    ]
    assert runs == 3


async def test_a_failing_task_keeps_running() -> None:
    clock = VirtualTime()
    calls = 0
    holder: list[Scheduler] = []

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            holder[0].stop()
        if calls < 3:
            raise RuntimeError("boom")

    scheduler = Scheduler([Task("flaky", 1, flaky)], sleep=clock.sleep)
    holder.append(scheduler)
    await run_until(scheduler)
    assert calls == 3


async def test_tasks_are_independent() -> None:
    clock = VirtualTime()
    counts = {"a": 0, "b": 0}
    holder: list[Scheduler] = []

    async def broken() -> None:
        counts["a"] += 1
        raise RuntimeError("always")

    async def fine() -> None:
        counts["b"] += 1
        if counts["b"] >= 3 and counts["a"] >= 3:
            holder[0].stop()

    scheduler = Scheduler([Task("a", 1, broken), Task("b", 1, fine)], sleep=clock.sleep)
    holder.append(scheduler)
    await run_until(scheduler)
    assert counts["a"] >= 3
    assert counts["b"] >= 3


async def test_stop_interrupts_sleeping_tasks() -> None:
    never = asyncio.Event()

    async def hang(_: float) -> None:
        await never.wait()

    async def job() -> None:
        return None

    scheduler = Scheduler([Task("t", 3600, job)], sleep=hang)
    runner = asyncio.create_task(scheduler.run())
    await asyncio.sleep(0.01)
    scheduler.stop()
    await asyncio.wait_for(runner, 1)


async def test_run_once_executes_immediately_and_swallows_errors() -> None:
    ran: list[str] = []

    async def ok() -> None:
        ran.append("ok")

    async def bad() -> None:
        raise RuntimeError("boom")

    scheduler = Scheduler([Task("ok", 60, ok), Task("bad", 60, bad)])
    await scheduler.run_once("ok")
    await scheduler.run_once("bad")
    assert ran == ["ok"]


def test_duplicate_task_names_are_refused() -> None:
    async def job() -> None:
        return None

    with pytest.raises(ValueError):
        Scheduler([Task("x", 1, job), Task("x", 2, job)])
