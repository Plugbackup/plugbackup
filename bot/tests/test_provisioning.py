import asyncio
from dataclasses import dataclass

import pytest
import pytest_asyncio

from fakes import FakeClock, FakeStorectl, RecordingHooks
from helpers import add_client, add_node, add_user, fake_key_body
from plugbackup_bot.db import Database
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.clients import get_client
from plugbackup_bot.db.jobs import JobStatus, Op
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.storectl import ErrorKind, StorectlError
from plugbackup_bot.services.provisioning import Provisioner, retry_delay, should_alert

KEY = fake_key_body(0)
ADDR = "192.0.2.10"


@dataclass
class Env:
    db: Database
    clock: FakeClock
    storectl: FakeStorectl
    hooks: RecordingHooks
    provisioner: Provisioner
    node_id: int

    async def client(
        self,
        tg_id: int = 1,
        state: State = State.PENDING,
        key: str | None = KEY,
        quota_gb: int = 50,
    ) -> int:
        await add_user(self.db, tg_id)
        return await add_client(
            self.db, tg_id, self.node_id, state=state.value, ssh_key_body=key, quota_gb=quota_gb
        )

    async def enqueue(self, client_id: int, op: Op) -> int:
        async with self.db.transaction() as conn:
            return await jobs_repo.enqueue(conn, client_id, op, self.clock.now())

    async def job(self, job_id: int) -> jobs_repo.Job:
        async with self.db.read() as conn:
            job = await jobs_repo.get_job(conn, job_id)
        assert job is not None
        return job

    async def state(self, client_id: int) -> State:
        async with self.db.read() as conn:
            client = await get_client(conn, client_id)
        assert client is not None
        return client.state


@pytest_asyncio.fixture
async def env(db: Database, clock: FakeClock) -> Env:
    storectl = FakeStorectl()
    hooks = RecordingHooks()
    node_id = await add_node(db)
    return Env(db, clock, storectl, hooks, Provisioner(db, storectl, clock, hooks), node_id)


def unreachable() -> StorectlError:
    return StorectlError(ErrorKind.UNREACHABLE, "ssh: connect refused", exit_code=255)


def test_retry_delay_grows_and_caps() -> None:
    assert [retry_delay(n) for n in (1, 2, 3, 4, 5, 6, 7, 100)] == [
        60, 120, 300, 900, 1800, 3600, 3600, 3600,
    ]  # fmt: skip


def test_admin_is_alerted_on_first_sixth_and_then_daily() -> None:
    assert [n for n in range(1, 60) if should_alert(n)] == [1, 6, 30, 54]


async def test_add_activates_the_client(env: Env) -> None:
    client_id = await env.client()
    job_id = await env.enqueue(client_id, Op.ADD)

    assert await env.provisioner.run_due() == 1

    assert env.storectl.calls == [("add", ADDR, "u1001", 50, KEY)]
    assert await env.state(client_id) is State.ACTIVE
    job = await env.job(job_id)
    assert (job.status, job.attempts) == (JobStatus.DONE, 1)
    assert [client.state for _, client in env.hooks.done] == [State.ACTIVE]
    async with (
        env.db.read() as conn,
        conn.execute(
            "SELECT from_state, to_state, reason FROM state_log WHERE client_id = ?", (client_id,)
        ) as cur,
    ):
        assert [tuple(row) for row in await cur.fetchall()] == [
            ("pending", "active", "provisioned")
        ]


async def test_add_for_active_client_keeps_state(env: Env) -> None:
    """Замена ключа: повторный add для живого клиента ничего не переводит."""
    client_id = await env.client(state=State.ACTIVE)
    await env.enqueue(client_id, Op.ADD)
    await env.provisioner.run_due()
    assert await env.state(client_id) is State.ACTIVE
    assert len(env.hooks.done) == 1


async def test_add_without_key_fails_for_good(env: Env) -> None:
    client_id = await env.client(key=None)
    job_id = await env.enqueue(client_id, Op.ADD)
    await env.provisioner.run_due()
    assert env.storectl.calls == []
    assert (await env.job(job_id)).status is JobStatus.FAILED
    assert await env.state(client_id) is State.PENDING
    assert [(err.kind, final) for _, err, final in env.hooks.problems] == [
        (ErrorKind.BAD_REQUEST, True)
    ]


async def test_retryable_error_is_retried_with_backoff(env: Env) -> None:
    client_id = await env.client()
    job_id = await env.enqueue(client_id, Op.ADD)
    env.storectl.fail("add", unreachable(), unreachable())
    start = env.clock.now()

    await env.provisioner.run_due()
    job = await env.job(job_id)
    assert (job.status, job.attempts, job.next_attempt_at) == (JobStatus.PENDING, 1, start + 60)
    assert "unreachable" in (job.last_error or "")
    assert await env.state(client_id) is State.PENDING
    assert [final for _, _, final in env.hooks.problems] == [False]

    assert await env.provisioner.run_due() == 0
    env.clock.advance(59)
    assert await env.provisioner.run_due() == 0
    env.clock.advance(1)
    assert await env.provisioner.run_due() == 1
    job = await env.job(job_id)
    assert (job.attempts, job.next_attempt_at) == (2, env.clock.now() + 120)
    assert len(env.hooks.problems) == 1  # вторая неудача админа не тревожит

    env.clock.advance(120)
    await env.provisioner.run_due()
    assert await env.state(client_id) is State.ACTIVE
    assert (await env.job(job_id)).status is JobStatus.DONE
    assert len(env.hooks.problems) == 1


async def test_permanent_error_fails_and_blocks_later_jobs(env: Env) -> None:
    blocked = await env.client(tg_id=1, state=State.ACTIVE)
    other = await env.client(tg_id=2, state=State.ACTIVE)
    resize = await env.enqueue(blocked, Op.RESIZE)
    suspend = await env.enqueue(blocked, Op.SUSPEND)
    other_job = await env.enqueue(other, Op.SUSPEND)
    env.storectl.fail("resize", StorectlError(ErrorKind.QUOTA_BELOW_USAGE, "занято больше"))

    await env.provisioner.run_due()

    assert (await env.job(resize)).status is JobStatus.FAILED
    assert (await env.job(suspend)).status is JobStatus.PENDING
    assert (await env.job(other_job)).status is JobStatus.DONE
    assert [(err.kind, final) for _, err, final in env.hooks.problems] == [
        (ErrorKind.QUOTA_BELOW_USAGE, True)
    ]
    assert await env.provisioner.run_due() == 0

    async with env.db.transaction() as conn:
        await jobs_repo.requeue(conn, resize, env.clock.now())
    assert await env.provisioner.run_due() == 1  # resize
    assert await env.provisioner.run_due() == 1  # suspend
    ops = [call[0] for call in env.storectl.calls if call[2] == "u1001"]
    assert ops == ["resize", "resize", "suspend"]


async def test_retrying_job_blocks_later_jobs_of_the_same_client(env: Env) -> None:
    client_id = await env.client(state=State.ACTIVE)
    first = await env.enqueue(client_id, Op.RESIZE)
    second = await env.enqueue(client_id, Op.SUSPEND)
    env.storectl.fail("resize", unreachable())

    await env.provisioner.run_due()
    assert await env.provisioner.run_due() == 0
    assert (await env.job(second)).status is JobStatus.PENDING

    env.clock.advance(60)
    await env.provisioner.run_due()
    await env.provisioner.run_due()
    assert (await env.job(first)).status is JobStatus.DONE
    assert (await env.job(second)).status is JobStatus.DONE
    assert [call[0] for call in env.storectl.calls] == ["resize", "resize", "suspend"]


async def test_jobs_for_deleted_clients_are_skipped(env: Env) -> None:
    client_id = await env.client(state=State.DELETED)
    job_id = await env.enqueue(client_id, Op.ADD)
    await env.provisioner.run_due()
    assert env.storectl.calls == []
    job = await env.job(job_id)
    assert job.status is JobStatus.DONE
    assert "удалён" in (job.last_error or "")
    assert env.hooks.done == []


async def test_delete_runs_for_deleted_client_and_tolerates_missing_login(env: Env) -> None:
    client_id = await env.client(state=State.DELETED)
    job_id = await env.enqueue(client_id, Op.DELETE)
    env.storectl.fail("delete", StorectlError(ErrorKind.NOT_FOUND, "нет такого"))
    await env.provisioner.run_due()
    assert [call[0] for call in env.storectl.calls] == ["delete"]
    assert (await env.job(job_id)).status is JobStatus.DONE
    assert env.hooks.problems == []


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        (Op.RESIZE, ("resize", ADDR, "u1001", 100)),
        (Op.SUSPEND, ("suspend", ADDR, "u1001")),
        (Op.RESUME, ("resume", ADDR, "u1001")),
        (Op.DELETE, ("delete", ADDR, "u1001")),
    ],
)
async def test_each_operation_is_dispatched(env: Env, op: Op, expected: tuple[object, ...]) -> None:
    client_id = await env.client(state=State.ACTIVE, quota_gb=100)
    await env.enqueue(client_id, op)
    await env.provisioner.run_due()
    assert env.storectl.calls == [expected]


async def test_unexpected_exception_is_retried_not_raised(env: Env) -> None:
    client_id = await env.client()
    job_id = await env.enqueue(client_id, Op.ADD)
    env.storectl.fail("add", RuntimeError("boom"))
    await env.provisioner.run_due()
    job = await env.job(job_id)
    assert job.status is JobStatus.PENDING
    assert "RuntimeError" in (job.last_error or "")
    assert "boom" not in (job.last_error or "")


async def test_database_is_not_locked_during_storectl_call(env: Env) -> None:
    client_id = await env.client()
    await env.enqueue(client_id, Op.ADD)
    reached = False

    async def probe(op: str) -> None:
        nonlocal reached
        async with asyncio.timeout(1), env.db.read():
            reached = True

    env.storectl.on_call = probe
    await env.provisioner.run_due()
    assert reached


async def test_overlapping_passes_do_not_run_twice(env: Env) -> None:
    client_id = await env.client()
    await env.enqueue(client_id, Op.ADD)
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def hold(op: str) -> None:
        entered.set()
        await gate.wait()

    env.storectl.on_call = hold
    first = asyncio.create_task(env.provisioner.run_due())
    await entered.wait()
    assert await env.provisioner.run_due() == 0
    gate.set()
    assert await first == 1
    assert len(env.storectl.calls) == 1


async def test_failing_hook_does_not_break_the_queue(env: Env) -> None:
    class Broken(RecordingHooks):
        async def job_done(self, job, client) -> None:  # type: ignore[no-untyped-def,override]
            raise RuntimeError("telegram is down")

    provisioner = Provisioner(env.db, env.storectl, env.clock, Broken())
    client_id = await env.client()
    job_id = await env.enqueue(client_id, Op.ADD)
    await provisioner.run_due()
    assert (await env.job(job_id)).status is JobStatus.DONE
    assert await env.state(client_id) is State.ACTIVE
