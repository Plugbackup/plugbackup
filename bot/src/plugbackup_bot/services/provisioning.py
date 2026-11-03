"""Исполнитель очереди provision_jobs: вызывает storectl и разбирает результат.

Вызов storectl идёт вне транзакции: замок базы во время ssh не держится.
"""

import asyncio
import logging
from collections.abc import Awaitable
from typing import Protocol

from plugbackup_bot.db import Database
from plugbackup_bot.db import jobs as jobs_repo
from plugbackup_bot.db.clients import Client, get_client, transition
from plugbackup_bot.db.jobs import Job, Op
from plugbackup_bot.db.nodes import Node, get_node
from plugbackup_bot.domain.states import State
from plugbackup_bot.infra.clock import Clock
from plugbackup_bot.infra.storectl import ErrorKind, Storectl, StorectlError

log = logging.getLogger(__name__)

# Паузы между повторами в секундах. После последней повторяем раз в час.
RETRY_DELAYS = (60, 120, 300, 900, 1800, 3600)
BATCH_SIZE = 20
_ERROR_LIMIT = 300


def retry_delay(attempts: int) -> int:
    """Пауза перед следующей попыткой после `attempts` неудачных."""
    return RETRY_DELAYS[min(attempts, len(RETRY_DELAYS)) - 1]


def should_alert(attempts: int) -> bool:
    """Админу пишем о первой неудаче, о шестой и затем примерно раз в сутки."""
    return attempts in (1, 6) or (attempts > 6 and (attempts - 6) % 24 == 0)


class ProvisionHooks(Protocol):
    async def job_done(self, job: Job, client: Client) -> None:
        """Задача выполнена. Для ADD клиент уже переведён в active."""
        ...

    async def job_problem(
        self, job: Job, client: Client | None, error: StorectlError, *, final: bool
    ) -> None:
        """Неудача. final=True: повторов не будет, нужен админ."""
        ...


class Provisioner:
    def __init__(
        self, db: Database, storectl: Storectl, clock: Clock, hooks: ProvisionHooks
    ) -> None:
        self._db = db
        self._storectl = storectl
        self._clock = clock
        self._hooks = hooks
        self._busy = asyncio.Lock()

    async def run_due(self) -> int:
        """Один проход по очереди. Возвращает число обработанных задач."""
        if self._busy.locked():
            return 0
        async with self._busy:
            async with self._db.read() as conn:
                batch = await jobs_repo.due(conn, self._clock.now(), BATCH_SIZE)
            for job in batch:
                await self._run_job(job)
            return len(batch)

    async def _run_job(self, job: Job) -> None:
        async with self._db.read() as conn:
            client = await get_client(conn, job.client_id)
            node = await get_node(conn, client.node_id) if client else None
        if client is None or node is None:
            error = StorectlError(ErrorKind.BAD_REQUEST, "клиент или нода не найдены")
            await self._fail(job, client, error)
            return
        if client.state is State.DELETED and job.op is not Op.DELETE:
            await self._succeed(job, client, note="пропущено: клиент удалён")
            return
        try:
            await self._execute(job, client, node)
        except StorectlError as error:
            if job.op is Op.DELETE and error.kind is ErrorKind.NOT_FOUND:
                await self._succeed(job, client, note="клиента на ноде уже нет")
            elif error.retryable:
                await self._retry(job, client, error)
            else:
                await self._fail(job, client, error)
        except Exception as exc:
            log.exception("unexpected error in job %s (%s)", job.id, job.op)
            await self._retry(
                job, client, StorectlError(ErrorKind.INTERNAL, f"сбой бота: {type(exc).__name__}")
            )
        else:
            await self._succeed(job, client)

    async def _execute(self, job: Job, client: Client, node: Node) -> None:
        match job.op:
            case Op.ADD:
                if client.ssh_key_body is None:
                    raise StorectlError(ErrorKind.BAD_REQUEST, "у клиента нет ключа")
                await self._storectl.add(
                    node.addr, client.login, client.quota_gb, client.ssh_key_body
                )
            case Op.RESIZE:
                await self._storectl.resize(node.addr, client.login, client.quota_gb)
            case Op.SUSPEND:
                await self._storectl.suspend(node.addr, client.login)
            case Op.RESUME:
                await self._storectl.resume(node.addr, client.login)
            case Op.DELETE:
                await self._storectl.delete(node.addr, client.login)

    async def _succeed(self, job: Job, client: Client, note: str | None = None) -> None:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            await jobs_repo.mark_done(conn, job.id, now, note)
            if job.op is Op.ADD and note is None:
                fresh = await get_client(conn, client.id)
                if fresh is not None and fresh.state is State.PENDING:
                    await transition(
                        conn,
                        client.id,
                        State.ACTIVE,
                        reason="provisioned",
                        actor="system",
                        now=now,
                        expected_from=State.PENDING,
                    )
            fresh = await get_client(conn, client.id)
        if note is None and fresh is not None:
            await self._notify(self._hooks.job_done(job, fresh))

    async def _retry(self, job: Job, client: Client, error: StorectlError) -> None:
        now = self._clock.now()
        async with self._db.transaction() as conn:
            attempts = await jobs_repo.mark_retry(
                conn, job.id, now, retry_delay(job.attempts + 1), _error_text(error)
            )
        if should_alert(attempts):
            await self._notify(self._hooks.job_problem(job, client, error, final=False))

    async def _fail(self, job: Job, client: Client | None, error: StorectlError) -> None:
        async with self._db.transaction() as conn:
            await jobs_repo.mark_failed(conn, job.id, self._clock.now(), _error_text(error))
        await self._notify(self._hooks.job_problem(job, client, error, final=True))

    @staticmethod
    async def _notify(call: Awaitable[None]) -> None:
        """Сбой уведомления не должен ломать очередь."""
        try:
            await call
        except Exception:
            log.exception("provision hook failed")


def _error_text(error: StorectlError) -> str:
    return str(error)[:_ERROR_LIMIT]
