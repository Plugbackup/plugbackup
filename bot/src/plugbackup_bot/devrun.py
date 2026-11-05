"""Локальный запуск для проверки в настоящем Telegram. ТОЛЬКО ДЛЯ РАЗРАБОТКИ.

    python -m plugbackup_bot.devrun

Всё работает как в боевом запуске, кроме одного: вместо SSH к ноде подставляется хранилище
в памяти. Выдача доступа, блокировка и удаление «выполняются», но никакой ноды нет, а после
перезапуска «клиенты на ноде» забываются (в базе бота они остаются). Для боевого запуска
используйте `python -m plugbackup_bot`.
"""

import asyncio
import logging
import sys

from plugbackup_bot.__main__ import read_settings, run
from plugbackup_bot.db.nodes import Node
from plugbackup_bot.infra.storectl import ErrorKind, LoginEvent, StorectlError, Usage
from plugbackup_bot.logging_setup import setup_logging

log = logging.getLogger("plugbackup_bot.devrun")

GIB = 1024**3
# Сколько квоты «занято» у каждого клиента: достаточно, чтобы /status было что показать.
USED_SHARE = 0.25


class InMemoryStorectl:
    """Повторяет поведение storectl на ноде: те же ошибки, но без SSH и ZFS."""

    def __init__(self) -> None:
        self.clients: dict[str, dict[str, object]] = {}

    def _get(self, login: str) -> dict[str, object]:
        client = self.clients.get(login)
        if client is None:
            raise StorectlError(ErrorKind.NOT_FOUND, f"клиент {login} не найден")
        return client

    async def add(self, addr: str, login: str, quota_gb: int, key_body: str) -> None:
        current = self.clients.get(login)
        if current is not None and current["used"] > quota_gb * GIB:  # type: ignore[operator]
            raise StorectlError(ErrorKind.QUOTA_BELOW_USAGE, "занято больше новой квоты")
        suspended = bool(current and current["suspended"])
        used = int(current["used"]) if current else int(quota_gb * GIB * USED_SHARE)  # type: ignore[call-overload]
        self.clients[login] = {
            "quota_gb": quota_gb,
            "key": key_body,
            "suspended": suspended,
            "used": used,
        }
        log.info("dev storectl: add %s %sG", login, quota_gb)

    async def resize(self, addr: str, login: str, quota_gb: int) -> None:
        client = self._get(login)
        if client["used"] > quota_gb * GIB:  # type: ignore[operator]
            raise StorectlError(ErrorKind.QUOTA_BELOW_USAGE, "занято больше новой квоты")
        client["quota_gb"] = quota_gb
        log.info("dev storectl: resize %s %sG", login, quota_gb)

    async def suspend(self, addr: str, login: str) -> None:
        self._get(login)["suspended"] = True
        log.info("dev storectl: suspend %s", login)

    async def resume(self, addr: str, login: str) -> None:
        self._get(login)["suspended"] = False
        log.info("dev storectl: resume %s", login)

    async def delete(self, addr: str, login: str) -> None:
        self.clients.pop(login, None)
        log.info("dev storectl: delete %s", login)

    async def usage(self, addr: str, login: str) -> Usage:
        client = self._get(login)
        return Usage(int(client["used"]), int(client["quota_gb"]) * GIB)  # type: ignore[call-overload]

    async def logins(self, addr: str, login: str, since: int) -> list[LoginEvent]:
        self._get(login)
        return []


async def always_up(node: Node) -> bool:
    return True


def main() -> int:
    settings = read_settings()
    if settings is None:
        return 2
    setup_logging(settings.log_level)
    log.warning("DEV MODE: storectl is in memory, no node is contacted")
    try:
        asyncio.run(run(settings, storectl=InMemoryStorectl(), probe=always_up))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
