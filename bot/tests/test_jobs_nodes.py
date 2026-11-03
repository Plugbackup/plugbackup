from helpers import NOW, add_client, add_node, add_user
from plugbackup_bot.config import NodeConfig
from plugbackup_bot.db import Database
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.jobs import JobStatus, Op
from plugbackup_bot.db.nodes import get_node, list_nodes, sync_nodes


def node(name: str = "node1", **extra: object) -> NodeConfig:
    values: dict[str, object] = {
        "name": name,
        "addr": "192.0.2.10",
        "public_host": f"{name}.example.org",
        "capacity_gb": 1000,
    }
    values.update(extra)
    return NodeConfig(**values)  # type: ignore[arg-type]


async def test_sync_inserts_updates_and_disables(db: Database) -> None:
    async with db.transaction() as conn:
        await sync_nodes(conn, [node("node1"), node("node2", capacity_gb=500)])
    async with db.read() as conn:
        nodes = await list_nodes(conn)
    assert [(n.name, n.capacity_gb, n.enabled) for n in nodes] == [
        ("node1", 1000, True),
        ("node2", 500, True),
    ]

    async with db.transaction() as conn:
        await sync_nodes(conn, [node("node1", capacity_gb=2000, oversell=1.5)])
    async with db.read() as conn:
        nodes = await list_nodes(conn)
    assert [(n.name, n.capacity_gb, n.limit_gb, n.enabled) for n in nodes] == [
        ("node1", 2000, 3000, True),
        ("node2", 500, 500, False),
    ]

    async with db.transaction() as conn:
        await sync_nodes(conn, [node("node1"), node("node2")])
    async with db.read() as conn:
        assert all(n.enabled for n in await list_nodes(conn))
        assert (await get_node(conn, nodes[0].id)) == (await list_nodes(conn))[0]
        assert await get_node(conn, 999) is None


async def test_sync_keeps_node_ids_stable(db: Database) -> None:
    async with db.transaction() as conn:
        await sync_nodes(conn, [node()])
    async with db.read() as conn:
        first = (await list_nodes(conn))[0].id
    async with db.transaction() as conn:
        await sync_nodes(conn, [node(capacity_gb=1)])
    async with db.read() as conn:
        assert (await list_nodes(conn))[0].id == first


async def prepared(db: Database) -> tuple[int, int]:
    node_id = await add_node(db)
    await add_user(db, 1)
    await add_user(db, 2)
    return await add_client(db, 1, node_id), await add_client(db, 2, node_id)


async def test_enqueue_roundtrip(db: Database) -> None:
    client_id, _ = await prepared(db)
    async with db.transaction() as conn:
        job_id = await jobs_repo.enqueue(conn, client_id, Op.RESIZE, NOW, {"why": "upgrade"})
    async with db.read() as conn:
        job = await jobs_repo.get_job(conn, job_id)
        assert job is not None
        assert (job.op, job.params, job.status, job.attempts) == (
            Op.RESIZE,
            {"why": "upgrade"},
            JobStatus.PENDING,
            0,
        )
        assert [j.id for j in await jobs_repo.list_for_client(conn, client_id)] == [job_id]
        assert await jobs_repo.get_job(conn, 999) is None


async def test_due_respects_time_order_and_limit(db: Database) -> None:
    a, b = await prepared(db)
    async with db.transaction() as conn:
        first = await jobs_repo.enqueue(conn, a, Op.SUSPEND, NOW)
        later = await jobs_repo.enqueue(conn, b, Op.SUSPEND, NOW + 100)
        third = await jobs_repo.enqueue(conn, b, Op.RESUME, NOW)
    async with db.read() as conn:
        assert [j.id for j in await jobs_repo.due(conn, NOW)] == [first]
        assert [j.id for j in await jobs_repo.due(conn, NOW + 100)] == [first, later]
        assert [j.id for j in await jobs_repo.due(conn, NOW + 100, limit=1)] == [first]
        assert third not in [j.id for j in await jobs_repo.due(conn, NOW + 100)]


async def test_counters_and_retry_bookkeeping(db: Database) -> None:
    a, _ = await prepared(db)
    async with db.transaction() as conn:
        retry = await jobs_repo.enqueue(conn, a, Op.ADD, NOW)
        failed = await jobs_repo.enqueue(conn, a, Op.RESIZE, NOW)
        done = await jobs_repo.enqueue(conn, a, Op.SUSPEND, NOW)
        assert await jobs_repo.mark_retry(conn, retry, NOW, 60, "x") == 1
        assert await jobs_repo.mark_retry(conn, retry, NOW, 60, "x") == 2
        await jobs_repo.mark_failed(conn, failed, NOW, "no")
        await jobs_repo.mark_done(conn, done, NOW, None)
        assert await jobs_repo.count_open(conn) == (1, 1)
        await jobs_repo.requeue(conn, done, NOW)  # не упавшую не трогаем
        await jobs_repo.requeue(conn, failed, NOW)
        assert await jobs_repo.count_open(conn) == (2, 0)
