"""Кнопка «Я ещё пользуюсь»."""

from aiogram import F, Router
from aiogram.types import CallbackQuery

from plugbackup_bot.services.lifecycle import AliveKind, LifecycleService
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.format import fmt_date
from plugbackup_bot.tg.handlers.common import reply


async def cb_alive(callback: CallbackQuery, lifecycle: LifecycleService, default_tz: str) -> None:
    raw = (callback.data or "").removeprefix(keyboards.ALIVE_PREFIX)
    client_id = int(raw) if raw.isdigit() else 0
    result = await lifecycle.confirm_alive(callback.from_user.id, client_id)
    await callback.answer()
    match result.kind:
        case AliveKind.OK if result.client and result.client.confirm_due_at:
            date = fmt_date(result.client.confirm_due_at, default_tz)
            template = texts.ALIVE_RESUMED if result.resumed else texts.ALIVE_THANKS
            await reply(callback, template.format(date=date))
        case AliveKind.BLOCKED:
            await reply(callback, texts.ALIVE_BLOCKED)
        case _:
            await reply(callback, texts.ALIVE_NO_CLIENT)


def build_router() -> Router:
    router = Router(name="lifecycle")
    router.callback_query.register(cb_alive, F.data.startswith(keyboards.ALIVE_PREFIX))
    return router
