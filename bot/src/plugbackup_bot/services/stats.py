"""Сводка для администратора: клиенты, проданное место, выручка."""

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from plugbackup_bot.db import Database
from plugbackup_bot.db.invites import count_unused
from plugbackup_bot.db.payments import revenue_stars
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.services.admin import AdminService, NodeLoad


@dataclass(frozen=True, slots=True)
class Stats:
    by_plan: dict[str, int]
    by_state: dict[str, int]
    nodes: list[NodeLoad]
    revenue_stars: int
    month_start: int
    paying: int
    pending_jobs: int
    failed_jobs: int
    open_restores: int
    unused_invites: int
    # Новые клиенты за последние 7 суток.
    new_week: int = 0


def month_bounds(now: int, tz: str) -> tuple[int, int]:
    """Начало текущего месяца и начало следующего по времени `tz`, unix-секунды."""
    zone = ZoneInfo(tz)
    moment = datetime.fromtimestamp(now, zone)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    following = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return int(start.timestamp()), int(following.timestamp())


async def collect(
    db: Database, clock: Clock, admin: AdminService, tz: str, beta_plan_id: str
) -> Stats:
    now = clock.now()
    start, end = month_bounds(now, tz)
    nodes = await admin.nodes_overview()
    async with db.read() as conn:
        async with conn.execute(
            "SELECT plan_id, COUNT(*) FROM clients WHERE state <> 'deleted' GROUP BY plan_id"
        ) as cur:
            by_plan = {row[0]: int(row[1]) for row in await cur.fetchall()}
        async with conn.execute(
            "SELECT state, COUNT(*) FROM clients WHERE state <> 'deleted' GROUP BY state"
        ) as cur:
            by_state = {row[0]: int(row[1]) for row in await cur.fetchall()}
        async with conn.execute(
            "SELECT COUNT(*) FROM clients WHERE plan_id <> ? AND state IN ('active', 'expiring')",
            (beta_plan_id,),
        ) as cur:
            paying = int((await cur.fetchone())[0])  # type: ignore[index]
        async with conn.execute(
            "SELECT COALESCE(SUM(status = 'pending'), 0), COALESCE(SUM(status = 'failed'), 0)"
            " FROM provision_jobs"
        ) as cur:
            jobs = await cur.fetchone()
        async with conn.execute(
            "SELECT COUNT(*) FROM restore_requests WHERE status = 'new'"
        ) as cur:
            restores = int((await cur.fetchone())[0])  # type: ignore[index]
        invites = await count_unused(conn, now)
        revenue = await revenue_stars(conn, start, end)
        async with conn.execute(
            "SELECT COUNT(*) FROM clients WHERE created_at >= ?", (now - 7 * 24 * 3600,)
        ) as cur:
            new_week = int((await cur.fetchone())[0])  # type: ignore[index]
    return Stats(
        by_plan=by_plan,
        by_state=by_state,
        nodes=nodes,
        revenue_stars=revenue,
        month_start=start,
        paying=paying,
        pending_jobs=int(jobs[0]) if jobs else 0,
        failed_jobs=int(jobs[1]) if jobs else 0,
        open_restores=restores,
        unused_invites=invites,
        new_week=new_week,
    )
