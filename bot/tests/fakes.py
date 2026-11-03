"""Подмены для тестов."""

from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import CreateInvoiceLink, SendMessage, TelegramMethod
from aiogram.types import Chat, InlineKeyboardMarkup, Message

from plugbackup_bot.db.clients import Client
from plugbackup_bot.db.jobs import Job
from plugbackup_bot.infra.storectl import LoginEvent, StorectlError, Usage


class FakeClock:
    def __init__(self, start: int = 1_760_000_000) -> None:
        self._now = start

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now += seconds

    def set(self, ts: int) -> None:
        self._now = ts


class FakeStorectl:
    """Записывает вызовы и по команде падает с заданными ошибками."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.usage_by_login: dict[str, Usage] = {}
        self.logins_by_login: dict[str, list[LoginEvent]] = {}
        self.on_call: Callable[[str], Awaitable[None]] | None = None
        self._failures: dict[str, list[Exception]] = {}

    def fail(self, op: str, *errors: Exception) -> None:
        """Следующие вызовы операции op по очереди падают с этими ошибками."""
        self._failures.setdefault(op, []).extend(errors)

    async def _call(self, op: str, *args: object) -> None:
        self.calls.append((op, *args))
        if self.on_call is not None:
            await self.on_call(op)
        queue = self._failures.get(op)
        if queue:
            raise queue.pop(0)

    async def add(self, addr: str, login: str, quota_gb: int, key_body: str) -> None:
        await self._call("add", addr, login, quota_gb, key_body)

    async def resize(self, addr: str, login: str, quota_gb: int) -> None:
        await self._call("resize", addr, login, quota_gb)

    async def suspend(self, addr: str, login: str) -> None:
        await self._call("suspend", addr, login)

    async def resume(self, addr: str, login: str) -> None:
        await self._call("resume", addr, login)

    async def delete(self, addr: str, login: str) -> None:
        await self._call("delete", addr, login)

    async def usage(self, addr: str, login: str) -> Usage:
        await self._call("usage", addr, login)
        return self.usage_by_login[login]

    async def logins(self, addr: str, login: str, since: int) -> list[LoginEvent]:
        await self._call("logins", addr, login, since)
        return [e for e in self.logins_by_login.get(login, []) if e.ts >= since]


class RecordingHooks:
    def __init__(self) -> None:
        self.done: list[tuple[Job, Client]] = []
        self.problems: list[tuple[Job, StorectlError, bool]] = []

    async def job_done(self, job: Job, client: Client) -> None:
        self.done.append((job, client))

    async def job_problem(
        self, job: Job, client: Client | None, error: StorectlError, *, final: bool
    ) -> None:
        self.problems.append((job, error, final))


FAKE_TOKEN = "123456789:" + "x" * 35


class FakeSession(BaseSession):
    """Сессия aiogram без сети: записывает методы и возвращает заготовленные ответы."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.errors: list[Exception] = []

    async def close(self) -> None:
        return None

    async def make_request(  # type: ignore[no-untyped-def,override]
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ):
        self.requests.append(method)
        if self.errors:
            raise self.errors.pop(0)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, CreateInvoiceLink):
            return "https://t.me/$fake-invoice"
        return True

    async def stream_content(  # type: ignore[override]
        self,
        url: str,
        headers=None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    def of(self, method_type: type[TelegramMethod[Any]]) -> list[Any]:
        return [m for m in self.requests if isinstance(m, method_type)]

    def texts(self) -> list[str]:
        return [m.text for m in self.of(SendMessage)]


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot(FAKE_TOKEN, session=session), session


class FakeStars:
    """Шлюз Stars: записывает отмены подписок, при необходимости падает."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []
        self.fail = False

    async def set_canceled(self, tg_id: int, charge_id: str, canceled: bool) -> None:
        self.calls.append((tg_id, charge_id, canceled))
        if self.fail:
            raise RuntimeError("Bad Request: subscription not found")


class RecordingMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.markups: list[InlineKeyboardMarkup | None] = []

    async def send(
        self, tg_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> bool:
        self.sent.append((tg_id, text))
        self.markups.append(reply_markup)
        return True


class RecordingAlerter:
    def __init__(self) -> None:
        self.alerts: list[str] = []

    async def alert(self, text: str) -> None:
        self.alerts.append(text)
