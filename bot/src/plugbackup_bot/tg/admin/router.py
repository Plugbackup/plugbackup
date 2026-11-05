"""Сборка роутера админки: общий фильтр доступа и разделы."""

from aiogram import Router

from plugbackup_bot.services.admins import AdminDirectory
from plugbackup_bot.tg.admin import clients, dashboard, legacy, menu
from plugbackup_bot.tg.admin.core import AdminFilter


def build_router(directory: AdminDirectory) -> Router:
    """Роутер админки. Не администраторы фильтром отсекаются, их события идут дальше."""
    router = Router(name="admin")
    access = AdminFilter(directory)
    router.message.filter(access)
    router.callback_query.filter(access)
    menu.register(router)
    dashboard.register(router)
    clients.register(router)
    legacy.register(router)
    menu.register_fallback(router)  # последним
    return router
