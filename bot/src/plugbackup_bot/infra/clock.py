"""Источник времени. В тестах подменяется, чтобы не ждать сутки."""

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> int:
        """Текущее время, unix-секунды UTC."""
        ...


class SystemClock:
    def now(self) -> int:
        return int(time.time())
