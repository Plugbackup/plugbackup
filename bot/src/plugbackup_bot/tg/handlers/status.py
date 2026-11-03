"""/status и переключатель автопродления."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from plugbackup_bot.services.access import AccessService
from plugbackup_bot.services.billing import BillingService
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.handlers.common import reply


async def show_status(
    event: Message | CallbackQuery, access: AccessService, default_tz: str
) -> None:
    view = await access.status(event.from_user.id)  # type: ignore[union-attr]
    if view is None:
        await reply(event, texts.NO_STORAGE)
    else:
        client = view.client
        # Переключатель нужен тем, у кого есть подписка Stars (бета и инвайты без неё).
        can_toggle = client.sub_charge_id is not None
        markup = keyboards.renewal(client.renewal_on) if can_toggle else None
        await reply(event, texts.status(view, default_tz), markup)
    if isinstance(event, CallbackQuery):
        await event.answer()


async def cb_renewal(
    callback: CallbackQuery, access: AccessService, billing: BillingService
) -> None:
    enabled = (callback.data or "").removeprefix(keyboards.RENEW_PREFIX) == "on"
    client = await access.live_client(callback.from_user.id)
    if client is None or not await billing.set_renewal(client, enabled):
        await callback.answer(texts.RENEWAL_CHANGE_FAILED, show_alert=True)
        return
    await callback.answer(texts.RENEWAL_ENABLED if enabled else "Автопродление отключено.")
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=keyboards.renewal(enabled))


def build_router() -> Router:
    router = Router(name="status")
    router.message.filter(F.chat.type == "private")
    router.message.register(show_status, Command("status"))
    router.callback_query.register(show_status, F.data == keyboards.MENU_STATUS)
    router.callback_query.register(cb_renewal, F.data.startswith(keyboards.RENEW_PREFIX))
    return router
