import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from fakes import FakeClock, RecordingAlerter, RecordingMessenger
from harness import Harness
from helpers import NOW, add_client, add_node, add_user, plain
from plugbackup_bot.db import Database
from plugbackup_bot.db import invites as invites_repo
from plugbackup_bot.db.nodes import Node, list_nodes
from plugbackup_bot.services.broadcast import MAX_TEXT, BroadcastService
from plugbackup_bot.services.health import HealthMonitor, tcp_probe
from plugbackup_bot.services.stats import collect, month_bounds
from plugbackup_bot.tg.admin import views

MIN = 60
HOUR = 3600


# --- проверка нод ---


class Probe:
    def __init__(self) -> None:
        self.up: dict[str, bool] = {}
        self.crash: set[str] = set()

    async def __call__(self, node: Node) -> bool:
        if node.name in self.crash:
            raise RuntimeError("probe bug")
        return self.up.get(node.name, True)


@pytest_asyncio.fixture
async def health(db: Database, clock: FakeClock):  # type: ignore[no-untyped-def]
    await add_node(db, "node1")
    probe, alerter = Probe(), RecordingAlerter()
    return HealthMonitor(db, clock, alerter, probe), probe, alerter, db, clock


async def node_state(db: Database, name: str = "node1") -> Node:
    async with db.read() as conn:
        return next(n for n in await list_nodes(conn) if n.name == name)


async def events(db: Database) -> list[tuple[str, int | None, int | None]]:
    async with (
        db.read() as conn,
        conn.execute("SELECT kind, starts_at, ends_at FROM service_events ORDER BY id") as cur,
    ):
        return [tuple(row) for row in await cur.fetchall()]


async def test_a_healthy_node_is_left_alone(health) -> None:  # type: ignore[no-untyped-def]
    monitor, _, alerter, db, _ = health
    await monitor.check()
    assert (await node_state(db)).down_since is None
    assert alerter.alerts == []


async def test_outage_timeline(health) -> None:  # type: ignore[no-untyped-def]
    monitor, probe, alerter, db, clock = health
    probe.up["node1"] = False
    start = clock.now()

    await monitor.check()
    assert (await node_state(db)).down_since == start
    assert alerter.alerts == []  # одна неудача — ещё не повод

    clock.advance(4 * MIN)
    await monitor.check()
    assert alerter.alerts == []

    clock.advance(1 * MIN)  # 5 минут
    await monitor.check()
    assert alerter.alerts == ["Нода node1 недоступна уже 5 мин."]
    assert await events(db) == []  # клиентам пока не пишем

    clock.advance(5 * MIN)
    await monitor.check()
    assert len(alerter.alerts) == 1  # без повторов
    assert await events(db) == []

    clock.advance(5 * MIN)  # 15 минут
    await monitor.check()
    assert await events(db) == [("node_down", clock.now(), None)]
    await monitor.check()
    assert len(await events(db)) == 1  # событие одно

    clock.advance(10 * MIN)
    probe.up["node1"] = True
    await monitor.check()
    assert (await node_state(db)).down_since is None
    [(kind, _, ended)] = await events(db)
    assert (kind, ended) == ("node_down", clock.now())
    assert alerter.alerts[-1] == "Нода node1 снова доступна, простой 25 мин."


async def test_short_blip_is_silent(health) -> None:  # type: ignore[no-untyped-def]
    monitor, probe, alerter, db, clock = health
    probe.up["node1"] = False
    await monitor.check()
    clock.advance(2 * MIN)
    probe.up["node1"] = True
    await monitor.check()
    assert (await node_state(db)).down_since is None
    assert alerter.alerts == []
    assert await events(db) == []


async def test_crashing_probe_counts_as_down(health) -> None:  # type: ignore[no-untyped-def]
    monitor, probe, _, db, _ = health
    probe.crash.add("node1")
    await monitor.check()
    assert (await node_state(db)).down_since is not None


async def test_nodes_are_independent_and_disabled_ones_skipped(health) -> None:  # type: ignore[no-untyped-def]
    monitor, probe, alerter, db, clock = health
    await add_node(db, "node2")
    async with db.transaction() as conn:
        await conn.execute("UPDATE nodes SET enabled = 0 WHERE name = 'node2'")
    probe.up.update({"node1": True, "node2": False})
    await monitor.check()
    clock.advance(20 * MIN)
    await monitor.check()
    assert (await node_state(db, "node2")).down_since is None
    assert alerter.alerts == []


async def test_tcp_probe_against_a_real_socket() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    node = Node(1, "n", "127.0.0.1", "h.example.org", 1, 1.0, True, None)
    assert await tcp_probe(port, timeout_s=2)(node) is True
    server.close()
    await server.wait_closed()
    assert await tcp_probe(port, timeout_s=2)(node) is False


# --- рассылка ---


class Messenger(RecordingMessenger):
    def __init__(self, refuse: set[int], crash: set[int]) -> None:
        super().__init__()
        self.refuse, self.crash = refuse, crash

    async def send(self, tg_id, text, *, reply_markup=None) -> bool:  # type: ignore[no-untyped-def,override]
        if tg_id in self.crash:
            raise RuntimeError("telegram")
        if tg_id in self.refuse:
            return False
        return await super().send(tg_id, text, reply_markup=reply_markup)


async def test_broadcast_counts_and_pacing(db: Database, clock: FakeClock) -> None:
    for tg_id in (1, 2, 3, 4):
        await add_user(db, tg_id)
    async with db.transaction() as conn:
        await conn.execute("UPDATE users SET bot_blocked = 1 WHERE tg_id = 4")
    messenger = Messenger(refuse={2}, crash={3})
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    service = BroadcastService(db, clock, messenger, rate_per_s=20, sleep=sleep)
    assert await service.recipients() == [1, 2, 3]
    broadcast_id = await service.create(9, "Привет <всем> & каждому")

    assert await service.run(broadcast_id) == (1, 2)
    assert messenger.sent == [(1, "Привет &lt;всем&gt; &amp; каждому")]
    assert delays == [0.05, 0.05, 0.05]
    async with db.read() as conn, conn.execute("SELECT sent, failed FROM broadcasts") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert (row[0], row[1]) == (1, 2)


async def test_broadcast_validation(db: Database, clock: FakeClock) -> None:
    service = BroadcastService(db, clock, RecordingMessenger())
    with pytest.raises(ValueError):
        await service.create(1, "   ")
    with pytest.raises(ValueError):
        await service.create(1, "x" * (MAX_TEXT + 1))
    with pytest.raises(LookupError):
        await service.run(99)


# --- статистика ---


def test_month_bounds_follow_the_timezone() -> None:
    zone = ZoneInfo("Europe/Moscow")
    start, end = month_bounds(1_793_620_800, "Europe/Moscow")  # 2 ноября 2026
    assert start == int(datetime(2026, 11, 1, tzinfo=zone).timestamp())
    assert end == int(datetime(2026, 12, 1, tzinfo=zone).timestamp())


def test_month_bounds_roll_over_the_year() -> None:
    december = int(datetime(2026, 12, 15, tzinfo=ZoneInfo("UTC")).timestamp())
    start, end = month_bounds(december, "UTC")
    assert start == int(datetime(2026, 12, 1, tzinfo=ZoneInfo("UTC")).timestamp())
    assert end == int(datetime(2027, 1, 1, tzinfo=ZoneInfo("UTC")).timestamp())


def test_month_depends_on_the_timezone_at_the_boundary() -> None:
    moment = int(datetime(2026, 10, 31, 22, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    moscow_start, _ = month_bounds(moment, "Europe/Moscow")  # там уже 1 ноября
    utc_start, _ = month_bounds(moment, "UTC")
    assert moscow_start > utc_start


async def test_stats_collection_and_text(db: Database, clock: FakeClock) -> None:
    h = await Harness.build(db, clock, node_capacity_gb=300)
    rows = [
        (1, "beta", 10, "active"),
        (2, "gb50", 50, "active"),
        (3, "gb50", 50, "suspended"),
        (4, "gb100", 100, "expiring"),
        (5, "gb50", 50, "pending"),
        (6, "gb200", 200, "deleted"),
    ]
    for tg_id, plan, quota, state in rows:
        await add_user(db, tg_id)
        await add_client(db, tg_id, 1, state=state, plan_id=plan, quota_gb=quota)

    month_start, _ = month_bounds(NOW, "Europe/Moscow")
    async with db.transaction() as conn:
        for number, (stars, at, status) in enumerate(
            [
                (50, month_start + 100, "paid"),
                (100, month_start + 200, "paid"),
                (170, month_start + 300, "refunded"),
                (999, month_start - 100, "paid"),
            ]
        ):
            await conn.execute(
                "INSERT INTO payments (tg_id, plan_id, stars, telegram_payment_charge_id,"
                " status, at) VALUES (2, 'gb50', ?, ?, ?, ?)",
                (stars, f"c{number}", status, at),
            )
        await conn.execute(
            "INSERT INTO provision_jobs (client_id, op, status, next_attempt_at, created_at)"
            " VALUES (2, 'resize', 'pending', 0, 0), (3, 'add', 'failed', 0, 0)"
        )
        await conn.execute(
            "INSERT INTO restore_requests (client_id, snapshot_date, created_at)"
            " VALUES (2, '2025-10-08', 0)"
        )
        await invites_repo.create_invites(conn, 2, 1, NOW)

    stats = await collect(db, clock, h.admin, "Europe/Moscow", "beta")
    assert stats.by_plan == {"beta": 1, "gb50": 3, "gb100": 1}
    assert stats.by_state == {"active": 2, "suspended": 1, "expiring": 1, "pending": 1}
    assert stats.paying == 2  # gb50 active и gb100 expiring
    assert stats.revenue_stars == 150
    assert (stats.pending_jobs, stats.failed_jobs) == (1, 1)
    assert (stats.open_restores, stats.unused_invites) == (1, 2)
    [load] = stats.nodes
    assert load.sold_gb == 10 + 50 + 50 + 100 + 50
    assert load.free_gb == 300 - 260

    assert stats.new_week == 6  # все созданы «сейчас» по часам теста, удалённые тоже

    text = plain(views.dashboard(stats, h.access._catalog, "Europe/Moscow"))
    assert "50 ГБ — 3" in text
    assert "node1: онлайн, продано 260 из 300 ГБ" in text
    assert "Выручка с 1 октября 2025: 150 Stars" in text
    assert "Продано: 260 ГБ из 300 ГБ (87%)" in text
    assert "Задачи на нодах: ждут 1, упали 1" in text
    assert "Неиспользованных инвайтов: 2" in text
