"""Очередь операций storectl (таблица provision_jobs).

Состояние клиента в базе меняется сразу, а нода догоняет его через очередь. Задачи одного
клиента выполняются строго по порядку: пока предыдущая не завершилась успешно (ждёт повтора
или упала насовсем), следующие не берутся.
"""

import json
from dataclasses import dataclass
from enum import StrEnum

import aiosqlite


class Op(StrEnum):
    ADD = "add"
    RESIZE = "resize"
    SUSPEND = "suspend"
    RESUME = "resume"
    DELETE = "delete"


class JobStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    client_id: int
    op: Op
    params: dict[str, object]
    status: JobStatus
    attempts: int
    next_attempt_at: int
    last_error: str | None
    created_at: int

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Job":
        return cls(
            id=row["id"],
            client_id=row["client_id"],
            op=Op(row["op"]),
            params=json.loads(row["params_json"]),
            status=JobStatus(row["status"]),
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
        )


async def enqueue(
    conn: aiosqlite.Connection,
    client_id: int,
    op: Op,
    now: int,
    params: dict[str, object] | None = None,
) -> int:
    cur = await conn.execute(
        "INSERT INTO provision_jobs (client_id, op, params_json, next_attempt_at, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (client_id, op.value, json.dumps(params or {}), now, now),
    )
    if cur.lastrowid is None:
        raise RuntimeError("INSERT не вернул id задачи")
    return cur.lastrowid


async def get_job(conn: aiosqlite.Connection, job_id: int) -> Job | None:
    async with conn.execute("SELECT * FROM provision_jobs WHERE id = ?", (job_id,)) as cur:
        row = await cur.fetchone()
    return Job.from_row(row) if row else None


async def list_for_client(conn: aiosqlite.Connection, client_id: int) -> list[Job]:
    async with conn.execute(
        "SELECT * FROM provision_jobs WHERE client_id = ? ORDER BY id", (client_id,)
    ) as cur:
        return [Job.from_row(row) for row in await cur.fetchall()]


async def due(conn: aiosqlite.Connection, now: int, limit: int = 20) -> list[Job]:
    """Задачи, которым пора выполняться и перед которыми у клиента нет незавершённых."""
    async with conn.execute(
        "SELECT * FROM provision_jobs AS j"
        " WHERE j.status = 'pending' AND j.next_attempt_at <= ?"
        " AND NOT EXISTS (SELECT 1 FROM provision_jobs AS p"
        "   WHERE p.client_id = j.client_id AND p.id < j.id"
        "   AND p.status IN ('pending', 'failed'))"
        " ORDER BY j.id LIMIT ?",
        (now, limit),
    ) as cur:
        return [Job.from_row(row) for row in await cur.fetchall()]


async def count_open(conn: aiosqlite.Connection) -> tuple[int, int]:
    """Сколько задач ждёт выполнения и сколько упало насовсем."""
    async with conn.execute(
        "SELECT COALESCE(SUM(status = 'pending'), 0), COALESCE(SUM(status = 'failed'), 0)"
        " FROM provision_jobs"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("COUNT вернул пустой ответ")
    return int(row[0]), int(row[1])


async def mark_done(conn: aiosqlite.Connection, job_id: int, now: int, note: str | None) -> None:
    await conn.execute(
        "UPDATE provision_jobs SET status = 'done', attempts = attempts + 1, done_at = ?,"
        " last_error = ? WHERE id = ?",
        (now, note, job_id),
    )


async def mark_retry(
    conn: aiosqlite.Connection, job_id: int, now: int, delay_s: int, error: str
) -> int:
    """Откладывает задачу. Возвращает число попыток, включая только что неудачную."""
    await conn.execute(
        "UPDATE provision_jobs SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?"
        " WHERE id = ?",
        (now + delay_s, error, job_id),
    )
    async with conn.execute("SELECT attempts FROM provision_jobs WHERE id = ?", (job_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise LookupError(f"задача {job_id} не найдена")
    return int(row[0])


async def mark_failed(conn: aiosqlite.Connection, job_id: int, now: int, error: str) -> None:
    await conn.execute(
        "UPDATE provision_jobs SET status = 'failed', attempts = attempts + 1, done_at = ?,"
        " last_error = ? WHERE id = ?",
        (now, error, job_id),
    )


async def requeue(conn: aiosqlite.Connection, job_id: int, now: int) -> None:
    """Возвращает упавшую задачу в очередь (действие админа)."""
    await conn.execute(
        "UPDATE provision_jobs SET status = 'pending', next_attempt_at = ?, done_at = NULL"
        " WHERE id = ? AND status = 'failed'",
        (now, job_id),
    )


async def has_open(conn: aiosqlite.Connection, client_id: int, op: Op) -> bool:
    """Есть ли у клиента задача op, которая ещё не выполнена (ждёт повтора или упала)."""
    async with conn.execute(
        "SELECT 1 FROM provision_jobs WHERE client_id = ? AND op = ?"
        " AND status IN ('pending', 'failed') LIMIT 1",
        (client_id, op.value),
    ) as cur:
        return await cur.fetchone() is not None
