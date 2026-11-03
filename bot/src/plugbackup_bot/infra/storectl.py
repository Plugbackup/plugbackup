"""Вызов storectl на ноде по SSH.

На ноде ключ пользователя provision привязан через command=, поэтому строка команды
состоит только из аргументов: `add u1001 50G <тело ключа>`. Пользовательский текст сюда
не попадает, кроме тела ключа, прошедшего `parse_public_key`. Все аргументы проверяются
здесь ещё раз до запуска процесса.

Коды выхода storectl:
    0 успех, 2 неверные аргументы, 3 клиента нет, 4 конфликт,
    5 квота ниже занятого места, 6 внутренняя ошибка.
Код 255 выдаёт сам ssh, если до ноды не достучались.
"""

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from plugbackup_bot.config import is_valid_host
from plugbackup_bot.domain.keys import InvalidKey, parse_public_key
from plugbackup_bot.domain.logins import LOGIN_RE
from plugbackup_bot.logging_setup import redact

log = logging.getLogger(__name__)

MAX_QUOTA_GB = 100_000
MAX_OUTPUT_BYTES = 1_000_000
_DETAIL_LIMIT = 300

_USAGE_NUMBER_RE = re.compile(r"[0-9]{1,18}")
_LOGIN_EVENT_RE = re.compile(r"([0-9]{9,11}) (ok|fail) ([0-9a-f]{16,64})")


class ErrorKind(StrEnum):
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    QUOTA_BELOW_USAGE = "quota_below_usage"
    INTERNAL = "internal"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    BAD_OUTPUT = "bad_output"


# Повтор имеет смысл только там, где причина может исчезнуть сама.
RETRYABLE = frozenset({ErrorKind.INTERNAL, ErrorKind.UNREACHABLE, ErrorKind.TIMEOUT})

_EXIT_KINDS = {
    2: ErrorKind.BAD_REQUEST,
    3: ErrorKind.NOT_FOUND,
    4: ErrorKind.CONFLICT,
    5: ErrorKind.QUOTA_BELOW_USAGE,
    6: ErrorKind.INTERNAL,
    255: ErrorKind.UNREACHABLE,
}


class StorectlError(Exception):
    def __init__(self, kind: ErrorKind, detail: str = "", *, exit_code: int | None = None) -> None:
        super().__init__(f"{kind.value}: {detail}" if detail else kind.value)
        self.kind = kind
        self.detail = detail
        self.exit_code = exit_code

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE


@dataclass(frozen=True, slots=True)
class Usage:
    used_bytes: int
    quota_bytes: int


@dataclass(frozen=True, slots=True)
class LoginEvent:
    """Одна попытка входа. Вместо IP нода отдаёт HMAC-хеш."""

    ts: int
    ok: bool
    ip_hash: str


class Storectl(Protocol):
    async def add(self, addr: str, login: str, quota_gb: int, key_body: str) -> None:
        """Создаёт клиента или обновляет его ключ и квоту (повтор безопасен)."""
        ...

    async def resize(self, addr: str, login: str, quota_gb: int) -> None: ...
    async def suspend(self, addr: str, login: str) -> None: ...
    async def resume(self, addr: str, login: str) -> None: ...
    async def delete(self, addr: str, login: str) -> None: ...
    async def usage(self, addr: str, login: str) -> Usage: ...
    async def logins(self, addr: str, login: str, since: int) -> list[LoginEvent]: ...


def parse_usage(output: str) -> Usage:
    lines = [line.strip() for line in output.strip().splitlines()]
    if len(lines) != 2 or not all(_USAGE_NUMBER_RE.fullmatch(line) for line in lines):
        raise StorectlError(ErrorKind.BAD_OUTPUT, "usage: ожидались две строки с числами")
    return Usage(used_bytes=int(lines[0]), quota_bytes=int(lines[1]))


def parse_logins(output: str) -> list[LoginEvent]:
    """Строки вида `<unix_ts> <ok|fail> <hex-хеш>`. Всё остальное, например IP, отвергается."""
    events = []
    for line in output.strip().splitlines():
        match = _LOGIN_EVENT_RE.fullmatch(line.strip())
        if match is None:
            raise StorectlError(ErrorKind.BAD_OUTPUT, "logins: строка не в формате ts ok|fail hash")
        events.append(LoginEvent(int(match[1]), match[2] == "ok", match[3]))
    events.sort(key=lambda event: event.ts)
    return events


def _bad(detail: str) -> StorectlError:
    return StorectlError(ErrorKind.BAD_REQUEST, detail)


def _check_addr(addr: str) -> None:
    if not is_valid_host(addr):
        raise _bad("адрес ноды")


def _check_login(login: str) -> None:
    if not LOGIN_RE.fullmatch(login):
        raise _bad("логин")


def _check_quota(quota_gb: int) -> None:
    if type(quota_gb) is not int or not 1 <= quota_gb <= MAX_QUOTA_GB:
        raise _bad("квота")


def _check_key(key_body: str) -> None:
    try:
        ok = parse_public_key(key_body) == key_body
    except InvalidKey:
        ok = False
    if not ok:
        raise _bad("ключ")


def _check_since(since: int) -> None:
    if type(since) is not int or since < 0:
        raise _bad("since")


def _sanitize(raw: bytes) -> str:
    text = redact(raw.decode("utf-8", errors="replace"))
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[:_DETAIL_LIMIT]


class SshStorectl:
    def __init__(
        self,
        *,
        user: str,
        key_path: Path,
        known_hosts_path: Path,
        port: int = 22,
        timeout_s: float = 20,
        ssh_command: Sequence[str] = ("ssh",),
    ) -> None:
        self._user = user
        self._key_path = key_path
        self._known_hosts_path = known_hosts_path
        self._port = port
        self._timeout_s = timeout_s
        self._ssh_command = tuple(ssh_command)

    def build_command(self, addr: str, args: Sequence[str]) -> list[str]:
        connect_timeout = max(1, min(10, int(self._timeout_s)))
        return [
            *self._ssh_command,
            "-F", os.devnull,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self._known_hosts_path}",
            "-o", "IdentitiesOnly=yes",
            "-o", f"ConnectTimeout={connect_timeout}",
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=2",
            "-o", "ClearAllForwardings=yes",
            "-a", "-x", "-T",
            "-i", str(self._key_path),
            "-p", str(self._port),
            "-l", self._user,
            "--", addr, *args,
        ]  # fmt: skip

    async def add(self, addr: str, login: str, quota_gb: int, key_body: str) -> None:
        _check_login(login)
        _check_quota(quota_gb)
        _check_key(key_body)
        await self._run(addr, ["add", login, f"{quota_gb}G", key_body])

    async def resize(self, addr: str, login: str, quota_gb: int) -> None:
        _check_login(login)
        _check_quota(quota_gb)
        await self._run(addr, ["resize", login, f"{quota_gb}G"])

    async def suspend(self, addr: str, login: str) -> None:
        _check_login(login)
        await self._run(addr, ["suspend", login])

    async def resume(self, addr: str, login: str) -> None:
        _check_login(login)
        await self._run(addr, ["resume", login])

    async def delete(self, addr: str, login: str) -> None:
        _check_login(login)
        await self._run(addr, ["delete", login])

    async def usage(self, addr: str, login: str) -> Usage:
        _check_login(login)
        return parse_usage(await self._run(addr, ["usage", login]))

    async def logins(self, addr: str, login: str, since: int) -> list[LoginEvent]:
        _check_login(login)
        _check_since(since)
        return parse_logins(await self._run(addr, ["logins", login, str(since)]))

    async def _run(self, addr: str, args: list[str]) -> str:
        _check_addr(addr)
        command = self.build_command(addr, args)
        op, login = args[0], args[1]
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise StorectlError(ErrorKind.INTERNAL, f"не удалось запустить ssh: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), self._timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            log.warning("storectl %s %s on %s: timeout", op, login, addr)
            raise StorectlError(ErrorKind.TIMEOUT, f"нет ответа за {self._timeout_s:g} с") from None
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise

        elapsed = time.monotonic() - started
        if proc.returncode != 0:
            kind = _EXIT_KINDS.get(proc.returncode or 0, ErrorKind.INTERNAL)
            log.warning(
                "storectl %s %s on %s: %s (exit %s)", op, login, addr, kind.value, proc.returncode
            )
            raise StorectlError(kind, _sanitize(stderr), exit_code=proc.returncode)
        if len(stdout) > MAX_OUTPUT_BYTES:
            raise StorectlError(ErrorKind.BAD_OUTPUT, "ответ слишком большой")
        log.info("storectl %s %s on %s: ok in %.1fs", op, login, addr, elapsed)
        return stdout.decode("utf-8", errors="replace")
