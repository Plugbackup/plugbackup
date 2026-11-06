"""Плашка режима техработ над ответами бота."""

from plugbackup_bot import texts_ru as T
from plugbackup_bot.emoji import E
from plugbackup_bot.services.maintenance_mode import MaintenanceMode
from plugbackup_bot.ui import cards
from plugbackup_bot.ui.markup import esc


class MaintenanceBanner:
    """Вызывается из Ui для каждого экрана: пусто, пока режим выключен или пишет админ."""

    def __init__(self, mode: MaintenanceMode) -> None:
        self._mode = mode

    async def __call__(self, tg_id: int) -> str | None:
        text = await self._mode.text_for(tg_id)
        if text is None:
            return None
        return cards.lines(cards.title(T.MAINT_BANNER_TITLE, E.warning), esc(text))
