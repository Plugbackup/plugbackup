"""Эффекты сообщений (message_effect_id): конфетти при оплате и выдаче доступа.

Эффект работает только в личных чатах и только для новых сообщений, не для правок. Если Telegram
эффект отклонит, сообщение уходит без него, а эффекты отключаются до перезапуска.
"""

import logging

log = logging.getLogger(__name__)

# 🎉 из стандартных эффектов Telegram. id взят из публичных списков эффектов; если Telegram его
# не примет, сообщение просто придёт без эффекта.
EFFECT_CONFETTI = "5046509860389126442"


class Effects:
    def __init__(self, *, enabled: bool = True, effect_id: str = EFFECT_CONFETTI) -> None:
        self._enabled = enabled
        self._effect_id = effect_id
        self._degraded_reason: str | None = None

    @property
    def active(self) -> bool:
        return self._enabled and self._degraded_reason is None

    def pick(self, wanted: bool) -> str | None:
        """id эффекта для сообщения или None, если эффект не нужен или недоступен."""
        return self._effect_id if wanted and self.active else None

    def degrade(self, reason: str) -> bool:
        """Отключает эффекты. True, если это произошло сейчас, а не раньше."""
        first = self._degraded_reason is None
        self._degraded_reason = reason
        return first
