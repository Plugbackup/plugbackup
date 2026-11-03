"""Настройки алертов клиента для экрана /alerts."""

from dataclasses import dataclass
from enum import StrEnum

from plugbackup_bot.db import Database
from plugbackup_bot.db import alerts as alerts_repo
from plugbackup_bot.db.alerts import Prefs, Setting
from plugbackup_bot.db.clients import get_live_client
from plugbackup_bot.domain.alerts import BY_KEY, COOLDOWN_OPTIONS, QUIET_OPTIONS, TIMEZONES
from plugbackup_bot.infra.clock import Clock


class ToggleKind(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    NEEDS_CONSENT = "needs_consent"
    NO_CLIENT = "no_client"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SettingsView:
    settings: dict[str, Setting]
    prefs: Prefs


class AlertSettingsService:
    def __init__(self, db: Database, clock: Clock, default_tz: str) -> None:
        self._db = db
        self._clock = clock
        self._default_tz = default_tz

    async def view(self, tg_id: int) -> SettingsView | None:
        """None, если у клиента нет хранилища: настраивать нечего."""
        async with self._db.read() as conn:
            if await get_live_client(conn, tg_id) is None:
                return None
            return SettingsView(
                await alerts_repo.get_settings(conn, tg_id),
                await alerts_repo.get_prefs(conn, tg_id, self._default_tz),
            )

    async def toggle(self, tg_id: int, key: str) -> ToggleKind:
        """Переключает алерт. Алерт нового IP при включении сначала требует согласия."""
        if key not in BY_KEY:
            return ToggleKind.UNKNOWN
        async with self._db.transaction() as conn:
            if await get_live_client(conn, tg_id) is None:
                return ToggleKind.NO_CLIENT
            current = (await alerts_repo.get_settings(conn, tg_id))[key]
            if key == "new_ip" and not current.enabled:
                return ToggleKind.NEEDS_CONSENT
            await alerts_repo.set_enabled(conn, tg_id, key, not current.enabled, self._clock.now())
        return ToggleKind.DISABLED if current.enabled else ToggleKind.ENABLED

    async def enable_new_ip(self, tg_id: int) -> bool:
        """Включение алерта нового IP после явного согласия на экране с объяснением."""
        async with self._db.transaction() as conn:
            if await get_live_client(conn, tg_id) is None:
                return False
            await alerts_repo.set_enabled(
                conn, tg_id, "new_ip", True, self._clock.now(), consent=True
            )
        return True

    async def disable(self, tg_id: int, key: str) -> bool:
        """Кнопка «Отключить этот алерт» в самом сообщении."""
        if key not in BY_KEY:
            return False
        async with self._db.transaction() as conn:
            await alerts_repo.set_enabled(conn, tg_id, key, False, self._clock.now())
        return True

    async def set_threshold(self, tg_id: int, key: str, value: int) -> bool:
        try:
            async with self._db.transaction() as conn:
                await alerts_repo.set_threshold(conn, tg_id, key, value, self._clock.now())
        except ValueError:
            return False
        return True

    async def set_quiet(self, tg_id: int, index: int) -> bool:
        if not 0 <= index < len(QUIET_OPTIONS):
            return False
        await self._save(tg_id, quiet=QUIET_OPTIONS[index])
        return True

    async def set_timezone(self, tg_id: int, index: int) -> bool:
        if not 0 <= index < len(TIMEZONES):
            return False
        await self._save(tg_id, tz=TIMEZONES[index][1])
        return True

    async def set_cooldown(self, tg_id: int, hours: int) -> bool:
        if hours not in COOLDOWN_OPTIONS:
            return False
        await self._save(tg_id, cooldown_hours=hours)
        return True

    async def _save(self, tg_id: int, **changes: object) -> None:
        async with self._db.transaction() as conn:
            await alerts_repo.set_prefs(
                conn,
                tg_id,
                self._clock.now(),
                default_tz=self._default_tz,
                **changes,  # type: ignore[arg-type]
            )
