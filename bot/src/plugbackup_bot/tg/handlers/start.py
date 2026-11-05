"""/start, /help, /rules, тарифы и «Как это работает»."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from plugbackup_bot.domain.plans import PlanCatalog
from plugbackup_bot.services.access import AccessService
from plugbackup_bot.tg import keyboards, texts
from plugbackup_bot.ui import banners
from plugbackup_bot.ui.screen import Screen, Ui


async def show_menu(
    event: Message | CallbackQuery,
    state: FSMContext,
    access: AccessService,
    catalog: PlanCatalog,
    ui: Ui,
) -> None:
    """Главное меню: приветствие с баннером. Сбрасывает незаконченный сценарий."""
    await state.clear()
    client = await access.live_client(event.from_user.id)  # type: ignore[union-attr]
    screen = Screen(
        texts.start(client is not None, catalog.beta.gb),
        keyboards.main_menu(client is not None),
        banner=banners.WELCOME,
    )
    await ui.show(event, screen)
    if isinstance(event, CallbackQuery):
        await event.answer()


async def cmd_help(message: Message, ui: Ui) -> None:
    await ui.show(message, Screen(texts.HELP, keyboards.only_home()))


async def cmd_rules(message: Message, ui: Ui) -> None:
    await ui.show(message, Screen(texts.rules(), keyboards.only_home()))


async def show_tariffs(
    event: Message | CallbackQuery,
    state: FSMContext,
    access: AccessService,
    catalog: PlanCatalog,
    ui: Ui,
) -> None:
    await state.clear()
    client = await access.live_client(event.from_user.id)  # type: ignore[union-attr]
    current = client.plan_id if client else None
    screen = Screen(
        texts.tariffs(catalog.paid, client),
        keyboards.tariffs(catalog.paid, current),
        banner=banners.TARIFFS,
    )
    await ui.show(event, screen)
    if isinstance(event, CallbackQuery):
        await event.answer()


async def cb_how(callback: CallbackQuery, state: FSMContext, ui: Ui) -> None:
    await state.clear()
    await ui.show(callback, Screen(texts.HOW_IT_WORKS, keyboards.only_home()))
    await callback.answer()


def build_router() -> Router:
    router = Router(name="start")
    router.message.filter(F.chat.type == "private")
    router.message.register(show_menu, Command("start"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_rules, Command("rules"))
    router.message.register(show_tariffs, Command("upgrade"))
    router.callback_query.register(show_menu, F.data == keyboards.MENU_HOME)
    router.callback_query.register(show_tariffs, F.data == keyboards.MENU_TARIFFS)
    router.callback_query.register(cb_how, F.data == keyboards.MENU_HOW)
    return router
