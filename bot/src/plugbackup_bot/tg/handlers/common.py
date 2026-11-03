"""Общие помощники handlers."""

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def reply(
    event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Отвечает в тот же чат, откуда пришло сообщение или нажатие кнопки."""
    if isinstance(event, Message):
        await event.answer(text, reply_markup=markup)
        return
    bot: Bot = event.bot  # type: ignore[assignment]
    await bot.send_message(event.from_user.id, text, reply_markup=markup)
