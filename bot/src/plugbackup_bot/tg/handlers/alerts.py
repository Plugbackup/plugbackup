"""/alerts: настройки алертов безопасности."""

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from plugbackup_bot.services.alert_settings import AlertSettingsService, ToggleKind
from plugbackup_bot.tg import alert_ui as ui
from plugbackup_bot.tg import texts

log = logging.getLogger(__name__)

NO_STORAGE = texts.NO_STORAGE
ALERT_OFF_DONE = "Алерт отключён. Вернуть его можно в /alerts."


async def _show(callback: CallbackQuery, screen: ui.Screen) -> None:
    """Меняет текущее сообщение на экран. Если сообщение недоступно, присылает новое."""
    text, markup = screen
    message = callback.message
    if isinstance(message, Message):
        try:
            await message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as error:
            if "not modified" not in str(error):
                raise
    else:
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=markup)  # type: ignore[union-attr]


async def _main(alert_settings: AlertSettingsService, tg_id: int) -> ui.Screen | None:
    view = await alert_settings.view(tg_id)
    return ui.main_screen(view) if view else None


async def cmd_alerts(message: Message, alert_settings: AlertSettingsService) -> None:
    screen = await _main(alert_settings, message.chat.id)
    if screen is None:
        await message.answer(NO_STORAGE)
    else:
        await message.answer(screen[0], reply_markup=screen[1])


async def cb_back(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    screen = await _main(alert_settings, callback.from_user.id)
    await callback.answer()
    if screen is None:
        await callback.bot.send_message(callback.from_user.id, NO_STORAGE)  # type: ignore[union-attr]
    else:
        await _show(callback, screen)


async def cb_toggle(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    key = (callback.data or "").removeprefix(ui.TOGGLE)
    result = await alert_settings.toggle(callback.from_user.id, key)
    await callback.answer()
    if result is ToggleKind.NEEDS_CONSENT:
        await _show(callback, ui.consent_screen())
    elif result in (ToggleKind.ENABLED, ToggleKind.DISABLED):
        await cb_back(callback, alert_settings)


async def cb_consent(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    await alert_settings.enable_new_ip(callback.from_user.id)
    await cb_back(callback, alert_settings)


async def cb_threshold_menu(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    key = (callback.data or "").removeprefix(ui.THRESHOLD)
    view = await alert_settings.view(callback.from_user.id)
    await callback.answer()
    if view is None or key not in view.settings or not ui.BY_KEY[key].options:
        return
    await _show(callback, ui.threshold_screen(key, view.settings[key].threshold))


async def cb_set_threshold(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    key, _, raw = (callback.data or "").removeprefix(ui.SET_THRESHOLD).partition(":")
    try:
        value = int(raw)
    except ValueError:
        await callback.answer()
        return
    await alert_settings.set_threshold(callback.from_user.id, key, value)
    await cb_back(callback, alert_settings)


async def cb_menu(callback: CallbackQuery) -> None:
    screens = {
        ui.QUIET: ui.quiet_screen,
        ui.TZ: ui.timezone_screen,
        ui.COOLDOWN: ui.cooldown_screen,
    }
    await callback.answer()
    await _show(callback, screens[callback.data or ""]())


async def cb_set_quiet(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    raw = (callback.data or "").removeprefix(ui.SET_QUIET)
    if raw.isdigit():
        await alert_settings.set_quiet(callback.from_user.id, int(raw))
    await cb_back(callback, alert_settings)


async def cb_set_timezone(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    raw = (callback.data or "").removeprefix(ui.SET_TZ)
    if raw.isdigit():
        await alert_settings.set_timezone(callback.from_user.id, int(raw))
    await cb_back(callback, alert_settings)


async def cb_set_cooldown(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    raw = (callback.data or "").removeprefix(ui.SET_COOLDOWN)
    if raw.isdigit():
        await alert_settings.set_cooldown(callback.from_user.id, int(raw))
    await cb_back(callback, alert_settings)


async def cb_alert_off(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    """Кнопка «Отключить этот алерт» под самим сообщением алерта."""
    key = (callback.data or "").removeprefix(ui.OFF)
    done = await alert_settings.disable(callback.from_user.id, key)
    await callback.answer(ALERT_OFF_DONE if done else None)
    if done and isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            log.debug("could not remove the alert keyboard", exc_info=True)


def build_router() -> Router:
    router = Router(name="alerts")
    router.message.filter(F.chat.type == "private")
    router.message.register(cmd_alerts, Command("alerts"))
    router.callback_query.register(cb_back, F.data == ui.BACK)
    router.callback_query.register(cb_toggle, F.data.startswith(ui.TOGGLE))
    router.callback_query.register(cb_consent, F.data == ui.CONSENT_YES)
    router.callback_query.register(cb_threshold_menu, F.data.startswith(ui.THRESHOLD))
    router.callback_query.register(cb_set_threshold, F.data.startswith(ui.SET_THRESHOLD))
    router.callback_query.register(cb_menu, F.data.in_({ui.QUIET, ui.TZ, ui.COOLDOWN}))
    router.callback_query.register(cb_set_quiet, F.data.startswith(ui.SET_QUIET))
    router.callback_query.register(cb_set_timezone, F.data.startswith(ui.SET_TZ))
    router.callback_query.register(cb_set_cooldown, F.data.startswith(ui.SET_COOLDOWN))
    router.callback_query.register(cb_alert_off, F.data.startswith(ui.OFF))
    return router
