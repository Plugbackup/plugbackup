"""Баннеры из design/graphics/png и кэш их file_id.

Картинку загружают в Telegram один раз, дальше отправляют по file_id. Кэш привязан к хешу
содержимого файла: заменили картинку в design/ и она загрузится заново. Нет файла: экран
показывается без баннера, это не ошибка.
"""

import hashlib
import logging
import re
from pathlib import Path

from aiogram.types import BufferedInputFile

from plugbackup_bot.db import Database
from plugbackup_bot.infra.clock import Clock

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Ключи баннеров на экранах. Для оплаты ключ собирается из тарифа: banner-plan-<id тарифа>.
WELCOME = "banner-announce"
TARIFFS = "banner-update"


def plan_banner(plan_id: str) -> str:
    return f"banner-plan-{plan_id.replace('_', '-')}"


class BannerStore:
    def __init__(self, db: Database, assets_dir: Path, clock: Clock) -> None:
        self._db = db
        self._dir = assets_dir
        self._clock = clock
        self._hashes: dict[Path, tuple[int, int, str]] = {}
        self._missing: set[str] = set()

    def find(self, key: str) -> Path | None:
        """Файл баннера или None. О каждом отсутствующем файле пишем в журнал один раз."""
        path = self._dir / f"{key}.png" if _KEY_RE.fullmatch(key) else None
        if path is not None and path.is_file():
            return path
        if key not in self._missing:
            self._missing.add(key)
            log.warning("banner %r not found in %s, the screen is shown without it", key, self._dir)
        return None

    def _digest(self, path: Path) -> str:
        stat = path.stat()
        known = self._hashes.get(path)
        if known is not None and known[:2] == (stat.st_mtime_ns, stat.st_size):
            return known[2]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self._hashes[path] = (stat.st_mtime_ns, stat.st_size, digest)
        return digest

    async def media(self, key: str) -> tuple[str, str | BufferedInputFile] | None:
        """(хеш файла, file_id из кэша или сам файл для загрузки). None, если баннера нет."""
        path = self.find(key)
        if path is None:
            return None
        digest = self._digest(path)
        async with (
            self._db.read() as conn,
            conn.execute("SELECT file_id FROM media_cache WHERE sha256 = ?", (digest,)) as cur,
        ):
            row = await cur.fetchone()
        if row is not None:
            return digest, row["file_id"]
        return digest, BufferedInputFile(path.read_bytes(), filename=path.name)

    async def remember(self, digest: str, file_id: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO media_cache (sha256, file_id, cached_at) VALUES (?, ?, ?)",
                (digest, file_id, self._clock.now()),
            )

    async def forget(self, digest: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM media_cache WHERE sha256 = ?", (digest,))
