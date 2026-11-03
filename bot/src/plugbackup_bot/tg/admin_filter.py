"""Доступ к командам администратора только для admin_ids из конфига."""

from collections.abc import Collection

from aiogram.filters import Filter
from aiogram.types import TelegramObject


class IsAdmin(Filter):
    def __init__(self, admin_ids: Collection[int]) -> None:
        self._ids = frozenset(admin_ids)

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in self._ids
