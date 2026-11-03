"""/start, /help, /rules, тарифы и «Как это работает»."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.services.access import AccessService
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.tg.handlers.common import reply


async def cmd_start(message: Message, state: FSMContext, access: AccessService) -> None:
    await state.clear()
    client = await access.live_client(message.chat.id)
    await message.answer(
        texts.start(client is not None), reply_markup=keyboards.main_menu(client is not None)
    )


async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


async def cmd_rules(message: Message) -> None:
    await message.answer(texts.rules())


async def show_tariffs(
    event: Message | CallbackQuery, access: AccessService, catalog: PlanCatalog
) -> None:
    client = await access.live_client(event.from_user.id)  # type: ignore[union-attr]
    current = client.plan_id if client else None
    await reply(
        event,
        texts.tariffs(catalog.paid, client),
        keyboards.tariffs(catalog.paid, current),
    )
    if isinstance(event, CallbackQuery):
        await event.answer()


async def cb_how(callback: CallbackQuery) -> None:
    await reply(callback, texts.HOW_IT_WORKS)
    await callback.answer()


def build_router() -> Router:
    router = Router(name="start")
    router.message.filter(F.chat.type == "private")
    router.message.register(cmd_start, Command("start"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_rules, Command("rules"))
    router.message.register(show_tariffs, Command("upgrade"))
    router.callback_query.register(show_tariffs, F.data == keyboards.MENU_TARIFFS)
    router.callback_query.register(cb_how, F.data == keyboards.MENU_HOW)
    return router
