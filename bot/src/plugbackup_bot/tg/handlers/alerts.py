"""/alerts: настройки алертов безопасности."""

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from plugbackup_bot import texts_ru as T
from plugbackup_bot.services.alert_settings import AlertSettingsService, ToggleKind
from plugbackup_bot.tg import alert_ui as screens
from plugbackup_bot.tg import keyboards
from plugbackup_bot.ui.screen import Screen, Ui

log = logging.getLogger(__name__)

NO_STORAGE = T.NO_STORAGE
ALERT_OFF_DONE = T.ALERT_OFF_DONE


async def _main(alert_settings: AlertSettingsService, tg_id: int) -> Screen:
    view = await alert_settings.view(tg_id)
    if view is None:
        return Screen(NO_STORAGE, keyboards.only_home())
    return screens.main_screen(view)


async def cmd_alerts(message: Message, alert_settings: AlertSettingsService, ui: Ui) -> None:
    await ui.show(message, await _main(alert_settings, message.chat.id))


async def cb_back(callback: CallbackQuery, alert_settings: AlertSettingsService, ui: Ui) -> None:
    await callback.answer()
    await ui.show(callback, await _main(alert_settings, callback.from_user.id))


async def cb_toggle(callback: CallbackQuery, alert_settings: AlertSettingsService, ui: Ui) -> None:
    key = (callback.data or "").removeprefix(screens.TOGGLE)
    result = await alert_settings.toggle(callback.from_user.id, key)
    if result is ToggleKind.NEEDS_CONSENT:
        await callback.answer()
        await ui.show(callback, screens.consent_screen())
    elif result in (ToggleKind.ENABLED, ToggleKind.DISABLED):
        await cb_back(callback, alert_settings, ui)
    else:
        await callback.answer()


async def cb_consent(callback: CallbackQuery, alert_settings: AlertSettingsService, ui: Ui) -> None:
    await alert_settings.enable_new_ip(callback.from_user.id)
    await cb_back(callback, alert_settings, ui)


async def cb_threshold_menu(
    callback: CallbackQuery, alert_settings: AlertSettingsService, ui: Ui
) -> None:
    key = (callback.data or "").removeprefix(screens.THRESHOLD)
    view = await alert_settings.view(callback.from_user.id)
    await callback.answer()
    if view is None or key not in view.settings or not screens.BY_KEY[key].options:
        return
    await ui.show(callback, screens.threshold_screen(key, view.settings[key].threshold))


async def cb_set_threshold(
    callback: CallbackQuery, alert_settings: AlertSettingsService, ui: Ui
) -> None:
    key, _, raw = (callback.data or "").removeprefix(screens.SET_THRESHOLD).partition(":")
    try:
        value = int(raw)
    except ValueError:
        await callback.answer()
        return
    await alert_settings.set_threshold(callback.from_user.id, key, value)
    await cb_back(callback, alert_settings, ui)


async def cb_menu(callback: CallbackQuery, ui: Ui) -> None:
    builders = {
        screens.QUIET: screens.quiet_screen,
        screens.TZ: screens.timezone_screen,
        screens.COOLDOWN: screens.cooldown_screen,
    }
    await callback.answer()
    await ui.show(callback, builders[callback.data or ""]())


async def cb_set_quiet(
    callback: CallbackQuery, alert_settings: AlertSettingsService, ui: Ui
) -> None:
    raw = (callback.data or "").removeprefix(screens.SET_QUIET)
    if raw.isdigit():
        await alert_settings.set_quiet(callback.from_user.id, int(raw))
    await cb_back(callback, alert_settings, ui)


async def cb_set_timezone(
    callback: CallbackQuery, alert_settings: AlertSettingsService, ui: Ui
) -> None:
    raw = (callback.data or "").removeprefix(screens.SET_TZ)
    if raw.isdigit():
        await alert_settings.set_timezone(callback.from_user.id, int(raw))
    await cb_back(callback, alert_settings, ui)


async def cb_set_cooldown(
    callback: CallbackQuery, alert_settings: AlertSettingsService, ui: Ui
) -> None:
    raw = (callback.data or "").removeprefix(screens.SET_COOLDOWN)
    if raw.isdigit():
        await alert_settings.set_cooldown(callback.from_user.id, int(raw))
    await cb_back(callback, alert_settings, ui)


async def cb_alert_off(callback: CallbackQuery, alert_settings: AlertSettingsService) -> None:
    """Кнопка «Отключить этот алерт» под самим сообщением алерта."""
    key = (callback.data or "").removeprefix(screens.OFF)
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
    router.callback_query.register(cb_back, F.data == screens.BACK)
    router.callback_query.register(cb_toggle, F.data.startswith(screens.TOGGLE))
    router.callback_query.register(cb_consent, F.data == screens.CONSENT_YES)
    router.callback_query.register(cb_threshold_menu, F.data.startswith(screens.THRESHOLD))
    router.callback_query.register(cb_set_threshold, F.data.startswith(screens.SET_THRESHOLD))
    router.callback_query.register(
        cb_menu, F.data.in_({screens.QUIET, screens.TZ, screens.COOLDOWN})
    )
    router.callback_query.register(cb_set_quiet, F.data.startswith(screens.SET_QUIET))
    router.callback_query.register(cb_set_timezone, F.data.startswith(screens.SET_TZ))
    router.callback_query.register(cb_set_cooldown, F.data.startswith(screens.SET_COOLDOWN))
    router.callback_query.register(cb_alert_off, F.data.startswith(screens.OFF))
    return router
