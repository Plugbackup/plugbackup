"""Одно окно: переходы по меню редактируют текущее сообщение, а не шлют новые.

Правка сообщения не может превратить текст в фото и обратно, поэтому при смене вида экрана
(с баннером и без) новое сообщение отправляется, а старое удаляется. Эффект сообщения ставится
только на новых сообщениях: правке он недоступен.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from plugbackup_bot.ui.banners import BannerStore
from plugbackup_bot.ui.effects import Effects

log = logging.getLogger(__name__)

# Сообщения, которых уже нет или которые нельзя править: вместо правки шлём новое.
_GONE = (
    "message to edit not found",
    "message can't be edited",
    "message to delete not found",
)
_BAD_FILE_ID = ("wrong file identifier", "wrong remote file identifier", "file identifier")

Media = tuple[str, str | BufferedInputFile]
# Плашка для пользователя (режим техработ): HTML-текст над экраном или None.
Notice = Callable[[int], Awaitable[str | None]]

# Пределы Telegram: подпись к фото и обычное сообщение.
_CAPTION_LIMIT = 1024
_TEXT_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class Screen:
    """Что показать: текст (HTML), кнопки, при необходимости баннер и эффект."""

    text: str
    markup: InlineKeyboardMarkup | None = None
    banner: str | None = None
    effect: bool = False


def _is_bad_file_id(error: TelegramBadRequest) -> bool:
    return any(text in error.message.lower() for text in _BAD_FILE_ID)


class Ui:
    def __init__(
        self,
        banners: BannerStore | None = None,
        effects: Effects | None = None,
        notice: Notice | None = None,
    ) -> None:
        self._banners = banners
        self._effects = effects
        self._notice = notice

    async def _with_notice(self, chat_id: int, screen: Screen) -> Screen:
        """Плашка над экраном. Только в личных чатах. Не влезла в подпись к баннеру: баннер
        убираем, текст важнее картинки."""
        if self._notice is None or chat_id <= 0:
            return screen
        notice = await self._notice(chat_id)
        if not notice:
            return screen
        text = f"{notice}\n\n{screen.text}"
        if screen.banner is not None and len(text) > _CAPTION_LIMIT:
            screen = replace(screen, banner=None)
        if len(text) > _TEXT_LIMIT:
            return screen
        return replace(screen, text=text)

    # --- показать экран ---

    async def show(
        self, event: Message | CallbackQuery, screen: Screen, *, new: bool = False
    ) -> None:
        """На нажатие кнопки отвечает правкой её сообщения, на команду и текст новым сообщением.
        new=True всегда шлёт новое сообщение."""
        bot: Bot = event.bot  # type: ignore[assignment]
        if isinstance(event, CallbackQuery):
            message = event.message
            chat_id = message.chat.id if message is not None else event.from_user.id
            screen = await self._with_notice(chat_id, screen)
            if isinstance(message, Message) and not new and await self._edit(bot, message, screen):
                return
        else:
            chat_id = event.chat.id
            screen = await self._with_notice(chat_id, screen)
        await self._send(bot, chat_id, screen)

    async def send(self, bot: Bot, chat_id: int, screen: Screen) -> Message:
        """Новое сообщение: фото с подписью, если у экрана есть баннер, иначе текст."""
        return await self._send(bot, chat_id, await self._with_notice(chat_id, screen))

    async def _send(self, bot: Bot, chat_id: int, screen: Screen) -> Message:
        effect = self._effects.pick(screen.effect) if self._effects else None
        media = await self._media(screen)
        if media is None:
            return await bot.send_message(
                chat_id, screen.text, reply_markup=screen.markup, message_effect_id=effect
            )
        digest, source = media
        try:
            sent = await bot.send_photo(
                chat_id, source, caption=screen.text, reply_markup=screen.markup,
                message_effect_id=effect,
            )  # fmt: skip
        except TelegramBadRequest as error:
            if not isinstance(source, str) or not _is_bad_file_id(error):
                raise
            # Telegram забыл файл: загружаем заново.
            await self._forget(digest)
            return await self._send(bot, chat_id, screen)
        if not isinstance(source, str):
            await self._remember(digest, sent)
        return sent

    # --- правка ---

    async def _edit(self, bot: Bot, message: Message, screen: Screen) -> bool:
        """True, если экран показан. False: сообщение править нельзя, нужно новое."""
        media = await self._media(screen)
        is_photo = bool(message.photo)
        try:
            if media is not None and is_photo:
                digest, source = media
                edited = await bot.edit_message_media(
                    InputMediaPhoto(media=source, caption=screen.text),
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=screen.markup,
                )
                if not isinstance(source, str) and isinstance(edited, Message):
                    await self._remember(digest, edited)
                return True
            if media is None and not is_photo:
                await bot.edit_message_text(
                    screen.text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_markup=screen.markup,
                )
                return True
        except TelegramBadRequest as error:
            reason = error.message.lower()
            if "message is not modified" in reason:
                return True
            if media is not None and isinstance(media[1], str) and _is_bad_file_id(error):
                await self._forget(media[0])
                return await self._edit(bot, message, screen)
            if any(text in reason for text in _GONE):
                return False
            raise
        # Вид сообщения не совпал с видом экрана: новое сообщение, старое убираем.
        await self._send(bot, message.chat.id, screen)
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            log.debug("could not delete the previous screen", exc_info=True)
        return True

    # --- баннеры ---

    async def _media(self, screen: Screen) -> Media | None:
        if screen.banner is None or self._banners is None:
            return None
        return await self._banners.media(screen.banner)

    async def _remember(self, digest: str, sent: Message) -> None:
        if self._banners is not None and sent.photo:
            await self._banners.remember(digest, sent.photo[-1].file_id)

    async def _forget(self, digest: str) -> None:
        if self._banners is not None:
            await self._banners.forget(digest)
