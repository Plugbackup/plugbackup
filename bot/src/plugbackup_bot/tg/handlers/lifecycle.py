"""Кнопка «Я ещё пользуюсь»."""

from aiogram import F, Router
from aiogram.types import CallbackQuery

from plugbackup_bot import texts_ru as T
from plugbackup_bot.services.lifecycle import AliveKind, LifecycleService
from plugbackup_bot.tg import keyboards
from plugbackup_bot.tg.format import fmt_date
from plugbackup_bot.ui.screen import Screen, Ui


async def cb_alive(
    callback: CallbackQuery, lifecycle: LifecycleService, default_tz: str, ui: Ui
) -> None:
    raw = (callback.data or "").removeprefix(keyboards.ALIVE_PREFIX)
    client_id = int(raw) if raw.isdigit() else 0
    result = await lifecycle.confirm_alive(callback.from_user.id, client_id)
    await callback.answer()
    match result.kind:
        case AliveKind.OK if result.client and result.client.confirm_due_at:
            date = fmt_date(result.client.confirm_due_at, default_tz)
            template = T.ALIVE_RESUMED if result.resumed else T.ALIVE_THANKS
            text = template.format(date=date)
        case AliveKind.BLOCKED:
            text = T.ALIVE_BLOCKED
        case _:
            text = T.ALIVE_NO_CLIENT
    await ui.show(callback, Screen(text, keyboards.only_home()))


def build_router() -> Router:
    router = Router(name="lifecycle")
    router.callback_query.register(cb_alive, F.data.startswith(keyboards.ALIVE_PREFIX))
    return router
