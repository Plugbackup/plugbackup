"""Подмены для тестов."""

import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.default import Default, DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    CreateInvoiceLink,
    DeleteMessage,
    EditMessageCaption,
    EditMessageMedia,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    GetStickerSet,
    SendMessage,
    SendPhoto,
    SetMyCommands,
    SetMyDescription,
    SetMyName,
    SetMyProfilePhoto,
    SetMyShortDescription,
    TelegramMethod,
)
from aiogram.types import Chat, InlineKeyboardMarkup, Message, PhotoSize, StickerSet, User

from plugbackup_bot.db.clients import Client
from plugbackup_bot.db.jobs import Job
from plugbackup_bot.infra.storectl import LoginEvent, StorectlError, Usage
from plugbackup_bot.services.notify import Delivery


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


_ALLOWED_TAGS = (
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
    "tg-spoiler", "tg-emoji", "blockquote",
)  # fmt: skip
_TAG_RE = re.compile(r"<(/?)(" + "|".join(_ALLOWED_TAGS) + r")(\s[^<>]*)?>")


def check_telegram_html(text: str) -> None:
    """Повторяет главное правило разбора HTML в Telegram: каждый «<» открывает или закрывает
    разрешённый тег, иначе Telegram отвечает «can't parse entities». ValueError при нарушении."""
    stack: list[str] = []
    pos = 0
    while (pos := text.find("<", pos)) != -1:
        match = _TAG_RE.match(text, pos)
        if match is None:
            raise ValueError(f"Unsupported start tag at offset {pos}: {text[pos : pos + 24]!r}")
        closing, name = match.group(1) == "/", match.group(2)
        if not closing:
            stack.append(name)
        elif not stack or stack.pop() != name:
            raise ValueError(f"Unexpected end tag {name!r} at offset {pos}")
        pos = match.end()
    if stack:
        raise ValueError(f"Unclosed tag {stack[-1]!r}")


MAX_MESSAGE_LENGTH = 4096
# Больше сущностей (теги форматирования, кастомные эмодзи) в одном сообщении Telegram не берёт.
MAX_ENTITIES = 100
BUTTON_STYLES = ("danger", "success", "primary")


def _actions(button: Any) -> int:
    """Сколько действий задано у кнопки: Telegram требует ровно одно."""
    fields = (
        button.callback_data,
        button.url,
        button.copy_text,
        button.web_app,
        button.login_url,
        button.switch_inline_query,
        button.switch_inline_query_current_chat,
        button.switch_inline_query_chosen_chat,
        button.callback_game,
    )
    return sum(value is not None for value in fields) + (1 if button.pay else 0)


MAX_CALLBACK_ANSWER = 200
MAX_CAPTION_LENGTH = 1024
FAKE_TOKEN = "123456789:" + "x" * 35


@dataclass(frozen=True)
class Output:
    """Всё, что бот показал пользователю: новое сообщение или правка."""

    text: str
    reply_markup: InlineKeyboardMarkup | None
    edited: bool
    photo: bool
    chat_id: int | str | None = None


class FakeSession(BaseSession):
    """Сессия aiogram без сети: записывает методы, повторяет ограничения Telegram и отвечает."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        # Ошибки по очереди на следующие запросы; None пропускает запрос без ошибки.
        self.errors: list[Exception | None] = []
        # False: владелец бота без Premium, Telegram отклоняет кастомные эмодзи.
        self.custom_emoji_allowed = True
        # False: Telegram не знает id эффекта сообщения.
        self.effects_allowed = True
        self.sticker_sets: dict[str, StickerSet] = {}
        # (чат, id сообщения) -> "text" или "photo": текст нельзя править как фото и наоборот.
        self.message_kinds: dict[tuple[int, int], str] = {}
        # Последнее сообщение бота в чате: (id, вид). По нему тестовый стенд «нажимает» кнопки.
        self.last_message: dict[int, tuple[int, str]] = {}
        self._next_id = 5000

    async def close(self) -> None:
        return None

    def _new_message(self, chat_id: int, kind: str) -> int:
        self._next_id += 1
        self.message_kinds[(chat_id, self._next_id)] = kind
        self.last_message[chat_id] = (self._next_id, kind)
        return self._next_id

    async def make_request(  # type: ignore[no-untyped-def,override]
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ):
        self.requests.append(method)
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        self._validate_message(bot, method)
        if isinstance(method, AnswerCallbackQuery) and len(method.text or "") > MAX_CALLBACK_ANSWER:
            raise TelegramBadRequest(method=method, message="Bad Request: MESSAGE_TOO_LONG")
        self._validate_profile(method)
        self._validate_custom_emoji(method)
        if isinstance(method, GetStickerSet):
            if method.name not in self.sticker_sets:
                raise TelegramBadRequest(method=method, message="Bad Request: STICKERSET_INVALID")
            return self.sticker_sets[method.name]
        if isinstance(method, GetMe):
            return User(id=1000, is_bot=True, first_name="PlugBackup", username="PlugBackupBot")
        if isinstance(method, SendMessage):
            chat_id = int(method.chat_id)
            return Message(
                message_id=self._new_message(chat_id, "text"),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, SendPhoto):
            chat_id = int(method.chat_id)
            return self._photo_message(chat_id, self._new_message(chat_id, "photo"), method.caption)
        if isinstance(method, EditMessageMedia) and method.chat_id is not None:
            chat_id = int(method.chat_id)
            return self._photo_message(chat_id, int(method.message_id or 0), method.media.caption)
        if isinstance(method, DeleteMessage):
            self.message_kinds.pop((int(method.chat_id), method.message_id), None)
            return True
        if isinstance(method, CreateInvoiceLink):
            return "https://t.me/$fake-invoice"
        return True

    def _photo_message(self, chat_id: int, message_id: int, caption: str | None) -> Message:
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private"),
            photo=[
                PhotoSize(
                    file_id=f"fid-{message_id}", file_unique_id=f"u-{message_id}", width=1280,
                    height=720,
                )
            ],
            caption=caption,
        )  # fmt: skip

    # --- проверки, как у настоящего Telegram ---

    def _validate_message(self, bot: Bot, method: TelegramMethod[Any]) -> None:
        """Текст, подпись, кнопки, эффект и вид правимого сообщения."""
        text: str | None = None
        limit = MAX_MESSAGE_LENGTH
        parse_mode: Any = getattr(method, "parse_mode", None)
        photo_ref: Any = None
        if isinstance(method, (SendMessage, EditMessageText)):
            text = method.text
        elif isinstance(method, (SendPhoto, EditMessageCaption)):
            text, limit = method.caption or "", MAX_CAPTION_LENGTH
            photo_ref = getattr(method, "photo", None)
        elif isinstance(method, EditMessageMedia):
            text, limit = method.media.caption or "", MAX_CAPTION_LENGTH
            parse_mode = getattr(method.media, "parse_mode", None)
            photo_ref = method.media.media
        if isinstance(photo_ref, str) and photo_ref.startswith("bad:"):
            raise TelegramBadRequest(
                method=method, message="Bad Request: wrong file identifier/HTTP URL specified"
            )
        if text is not None:
            self._validate_text(bot, method, text, limit, parse_mode)
        if isinstance(
            method,
            (SendMessage, SendPhoto, EditMessageText, EditMessageCaption, EditMessageMedia,
             EditMessageReplyMarkup),
        ):  # fmt: skip
            self._validate_markup(method)
        if getattr(method, "message_effect_id", None) and not self.effects_allowed:
            raise TelegramBadRequest(method=method, message="Bad Request: EFFECT_ID_INVALID")
        self._validate_edit_kind(method)

    def _validate_edit_kind(self, method: TelegramMethod[Any]) -> None:
        if not isinstance(method, (EditMessageText, EditMessageCaption, EditMessageMedia)):
            return
        if method.chat_id is None or method.message_id is None:
            return
        kind = self.message_kinds.get((int(method.chat_id), method.message_id))
        problem = None
        if isinstance(method, EditMessageText) and kind == "photo":
            problem = "there is no text in the message to edit"
        elif isinstance(method, (EditMessageCaption, EditMessageMedia)) and kind == "text":
            problem = "there is no media in the message to edit"
        if problem:
            raise TelegramBadRequest(method=method, message=f"Bad Request: {problem}")

    def _validate_custom_emoji(self, method: TelegramMethod[Any]) -> None:
        if self.custom_emoji_allowed:
            return
        markup = getattr(method, "reply_markup", None)
        in_buttons = isinstance(markup, InlineKeyboardMarkup) and any(
            button.icon_custom_emoji_id for row in markup.inline_keyboard for button in row
        )
        in_text = any(
            "<tg-emoji" in (getattr(method, field, None) or "") for field in ("text", "caption")
        ) or "<tg-emoji" in (getattr(getattr(method, "media", None), "caption", None) or "")
        if in_buttons or in_text:
            raise TelegramBadRequest(method=method, message="Bad Request: DOCUMENT_INVALID")

    @staticmethod
    def _validate_profile(method: TelegramMethod[Any]) -> None:
        """Лимиты профиля бота из Bot API."""
        problem = None
        if isinstance(method, SetMyName) and len(method.name or "") > 64:
            problem = "name is too long"
        elif (
            isinstance(method, SetMyShortDescription) and len(method.short_description or "") > 120
        ):
            problem = "short description is too long"
        elif isinstance(method, SetMyDescription) and len(method.description or "") > 512:
            problem = "description is too long"
        elif isinstance(method, SetMyCommands):
            if len(method.commands) > 100:
                problem = "too many commands"
            for command in method.commands:
                if not re.fullmatch(r"[a-z0-9_]{1,32}", command.command):
                    problem = f"BOT_COMMAND_INVALID: {command.command}"
                elif not 3 <= len(command.description) <= 256:
                    problem = f"BOT_COMMAND_DESCRIPTION_INVALID: {command.command}"
        elif isinstance(method, SetMyProfilePhoto):
            data = getattr(getattr(method.photo, "photo", None), "data", b"")
            if not bytes(data).startswith(bytes([0xFF, 0xD8, 0xFF])):
                problem = "PHOTO_INVALID: the profile photo must be a JPG"
        if problem:
            raise TelegramBadRequest(method=method, message=f"Bad Request: {problem}")

    @staticmethod
    def _validate_markup(method: Any) -> None:
        markup = method.reply_markup
        if not isinstance(markup, InlineKeyboardMarkup):
            return
        for row in markup.inline_keyboard:
            for button in row:
                problem = None
                if not button.text:
                    problem = "BUTTON_TEXT_EMPTY"
                elif button.callback_data is not None and len(button.callback_data.encode()) > 64:
                    problem = "BUTTON_DATA_INVALID: callback_data longer than 64 bytes"
                elif _actions(button) != 1:
                    problem = "BUTTON_TYPE_INVALID: a button needs exactly one action"
                elif button.style is not None and button.style not in BUTTON_STYLES:
                    problem = f"BUTTON_STYLE_INVALID: {button.style}"
                elif button.copy_text is not None and not 1 <= len(button.copy_text.text) <= 256:
                    problem = "BUTTON_COPY_TEXT_INVALID: from 1 to 256 characters"
                elif button.url is not None and not button.url.startswith(
                    ("https://", "http://", "tg://")
                ):
                    problem = "BUTTON_URL_INVALID"
                if problem:
                    raise TelegramBadRequest(method=method, message=f"Bad Request: {problem}")

    @staticmethod
    def _validate_text(
        bot: Bot, method: TelegramMethod[Any], text: str, limit: int, mode: Any
    ) -> None:
        if len(text) > limit:
            what = "message" if limit == MAX_MESSAGE_LENGTH else "message caption"
            raise TelegramBadRequest(method=method, message=f"Bad Request: {what} is too long")
        if isinstance(mode, Default):
            mode = bot.default.parse_mode
        if mode != ParseMode.HTML:
            return
        try:
            check_telegram_html(text)
        except ValueError as error:
            raise TelegramBadRequest(
                method=method, message=f"Bad Request: can't parse entities: {error}"
            ) from error
        if len(re.findall(r"<[a-z]", text)) > MAX_ENTITIES:
            raise TelegramBadRequest(method=method, message="Bad Request: too many entities")

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

    def outputs(self) -> list[Output]:
        """Всё показанное пользователю по порядку: сообщения, фото с подписью и правки."""
        shown: list[Output] = []
        for method in self.requests:
            if isinstance(method, SendMessage):
                shown.append(Output(method.text, method.reply_markup, False, False, method.chat_id))  # type: ignore[arg-type]
            elif isinstance(method, EditMessageText):
                shown.append(Output(method.text, method.reply_markup, True, False, method.chat_id))  # type: ignore[arg-type]
            elif isinstance(method, SendPhoto):
                shown.append(
                    Output(method.caption or "", method.reply_markup, False, True, method.chat_id)
                )  # type: ignore[arg-type]
            elif isinstance(method, EditMessageCaption):
                shown.append(
                    Output(method.caption or "", method.reply_markup, True, True, method.chat_id)
                )  # type: ignore[arg-type]
            elif isinstance(method, EditMessageMedia):
                shown.append(
                    Output(
                        method.media.caption or "", method.reply_markup, True, True, method.chat_id
                    )
                )  # type: ignore[arg-type]
        return shown


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    # Как в боевом запуске: сообщения по умолчанию идут в режиме HTML.
    bot = Bot(FAKE_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, session


class FakeStars:
    """Шлюз Stars: записывает отмены подписок, при необходимости падает."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, bool]] = []
        self.fail = False
        self.refunds: list[tuple[int, str]] = []
        # Что ответит Telegram на возврат: None, если всё хорошо, иначе текст ошибки.
        self.refund_error: str | None = None

    async def set_canceled(self, tg_id: int, charge_id: str, canceled: bool) -> None:
        self.calls.append((tg_id, charge_id, canceled))
        if self.fail:
            raise RuntimeError("Bad Request: subscription not found")

    async def refund(self, tg_id: int, charge_id: str) -> None:
        self.refunds.append((tg_id, charge_id))
        if self.refund_error is not None:
            raise RuntimeError(self.refund_error)


class RecordingMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.markups: list[InlineKeyboardMarkup | None] = []
        self.effects: list[bool] = []
        self.undeliverable: set[int] = set()
        self.blocked: set[int] = set()
        self.failing: set[int] = set()

    async def send(
        self,
        tg_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        effect: bool = False,
    ) -> bool:
        self.sent.append((tg_id, text))
        self.markups.append(reply_markup)
        self.effects.append(effect)
        return tg_id not in self.undeliverable

    async def deliver(
        self, tg_id: int, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> Delivery:
        if tg_id in self.blocked:
            return Delivery.BLOCKED
        if tg_id in self.failing:
            return Delivery.FAILED
        await self.send(tg_id, text, reply_markup=reply_markup)
        return Delivery.SENT


class RecordingAlerter:
    def __init__(self) -> None:
        self.alerts: list[str] = []

    async def alert(self, text: str) -> None:
        self.alerts.append(text)
