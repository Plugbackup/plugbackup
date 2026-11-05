"""/status и переключатель автопродления."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.services.access import AccessService
from plugbackup_bot.services.billing import BillingService
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.ui.screen import Screen, Ui


async def _status_screen(access: AccessService, tg_id: int, tz: str) -> Screen:
    view = await access.status(tg_id)
    if view is None:
        return Screen(T.NO_STORAGE, keyboards.only_home())
    return Screen(texts.status(view, tz), keyboards.status(view))


async def show_status(
    event: Message | CallbackQuery,
    state: FSMContext,
    access: AccessService,
    default_tz: str,
    ui: Ui,
) -> None:
    await state.clear()
    screen = await _status_screen(access, event.from_user.id, default_tz)  # type: ignore[union-attr]
    await ui.show(event, screen)
    if isinstance(event, CallbackQuery):
        await event.answer()


async def cb_renewal(
    callback: CallbackQuery,
    access: AccessService,
    billing: BillingService,
    default_tz: str,
    ui: Ui,
) -> None:
    enabled = (callback.data or "").removeprefix(keyboards.RENEW_PREFIX) == "on"
    client = await access.live_client(callback.from_user.id)
    if client is None or not await billing.set_renewal(client, enabled):
        await callback.answer(T.RENEWAL_CHANGE_FAILED, show_alert=True)
        return
    await callback.answer(T.RENEWAL_ENABLED if enabled else T.RENEWAL_DISABLED)
    await ui.show(callback, await _status_screen(access, callback.from_user.id, default_tz))


def build_router() -> Router:
    router = Router(name="status")
    router.message.filter(F.chat.type == "private")
    router.message.register(show_status, Command("status"))
    router.callback_query.register(show_status, F.data == keyboards.MENU_STATUS)
    router.callback_query.register(cb_renewal, F.data.startswith(keyboards.RENEW_PREFIX))
    return router
