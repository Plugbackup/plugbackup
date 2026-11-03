"""Простой планировщик: каждая задача живёт в своём цикле со своим интервалом.

Задачи должны быть идемпотентными и опираться на время из базы. Тогда пропущенный запуск
(например, после перезапуска бота) ничего не ломает: следующий всё догонит.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Task:
    name: str
    interval_s: float
    func: Callable[[], Awaitable[object]]
    # Пауза перед первым запуском: после старта даём догнаться накопившимся платежам.
    first_delay_s: float = 0


class Scheduler:
    def __init__(self, tasks: list[Task], *, sleep: Sleep = asyncio.sleep) -> None:
        names = [task.name for task in tasks]
        if len(set(names)) != len(names):
            raise ValueError("имена задач должны быть уникальны")
        self._tasks = tasks
        self._sleep = sleep
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        """Работает, пока не вызван stop() или не отменена задача."""
        runners = [asyncio.create_task(self._loop(task), name=task.name) for task in self._tasks]
        try:
            await self._stopped.wait()
        finally:
            for runner in runners:
                runner.cancel()
            await asyncio.gather(*runners, return_exceptions=True)

    async def run_once(self, name: str) -> None:
        """Выполняет задачу немедленно, например при ручном запуске."""
        task = next(task for task in self._tasks if task.name == name)
        await self._execute(task)

    async def _loop(self, task: Task) -> None:
        if task.first_delay_s > 0:
            await self._sleep(task.first_delay_s)
        while not self._stopped.is_set():
            await self._execute(task)
            await self._sleep(task.interval_s)

    @staticmethod
    async def _execute(task: Task) -> None:
        try:
            await task.func()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Сбой одной задачи не должен останавливать ни её саму, ни остальные.
            log.exception("task %s failed", task.name)
