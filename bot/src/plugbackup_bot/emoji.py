"""Эмодзи интерфейса.

Тексты и кнопки обращаются к эмодзи по смыслу (`E.ok`, `E.lock`), а не по картинке. Какой именно
эмодзи из пака RestrictedEmoji стоит за именем, записано в emoji_map.json, его можно править
руками. Если кастомные эмодзи выключены флагом USE_CUSTOM_EMOJI или Telegram их отклонил (у
владельца бота нет Premium), везде показываются обычные эмодзи.
"""

import logging
from collections.abc import Mapping

from aiogram.types import InlineKeyboardButton

log = logging.getLogger(__name__)

# (имя, обычный эмодзи для запасного вида, обычные эмодзи в порядке предпочтения для поиска в паке)
_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ok", "✅", ("✅", "☑️", "✔️", "👍")),
    ("error", "❌", ("❌", "⛔", "🚫", "✖️")),
    ("warning", "⚠️", ("⚠️", "❗", "‼️")),
    ("lock", "🔒", ("🔒", "🔐", "🔓")),
    ("shield", "🛡", ("🛡",)),
    ("key", "🔑", ("🔑", "🗝")),
    ("disk", "💾", ("💾", "💽", "🗄", "📀", "🗂", "📁", "📂")),
    ("upload", "⬆️", ("⬆️", "📤", "🔼")),
    ("download", "⬇️", ("⬇️", "📥", "🔽")),
    ("payment", "💳", ("💳", "💰", "💵", "🧾")),
    ("star", "⭐", ("⭐", "🌟", "✨")),
    ("bell", "🔔", ("🔔", "🔕")),
    ("settings", "⚙️", ("⚙️", "🛠", "🔧", "🧰")),
    ("back", "◀️", ("◀️", "⬅️", "↩️", "🔙")),
    ("home", "🏠", ("🏠", "🏡", "🏘")),
    ("chart", "📊", ("📊", "📈", "📉")),
    ("clock", "🕒", ("🕒", "🕐", "⏰", "⏱", "⌛", "⏳")),
    ("user", "👤", ("👤", "👥", "🙂", "🧑")),
    ("admin", "👑", ("👑", "🛂", "🔱")),
    ("trash", "🗑", ("🗑", "🚮")),
    ("refresh", "🔄", ("🔄", "🔁", "🔃", "♻️")),
    ("invite", "🎟", ("🎟", "✉️", "📨", "🔗")),
    ("support", "💬", ("💬", "🆘", "🎧", "🛟", "☎️", "❓")),
)

NAMES: tuple[str, ...] = tuple(name for name, _, _ in _SPECS)
GLYPHS: dict[str, str] = {name: glyph for name, glyph, _ in _SPECS}
CANDIDATES: dict[str, tuple[str, ...]] = {name: found for name, _, found in _SPECS}

_VARIATION_SELECTORS = ("️", "︎")


def plain_key(emoji: str) -> str:
    """Эмодзи без селекторов вариантов: «⚠» и «⚠️» считаются одним знаком."""
    for selector in _VARIATION_SELECTORS:
        emoji = emoji.replace(selector, "")
    return emoji


class Icon:
    """Один смысловой эмодзи. В строку превращается в тот вид, который сейчас разрешён."""

    __slots__ = ("_registry", "glyph", "name")

    def __init__(self, registry: "EmojiRegistry", name: str, glyph: str) -> None:
        self._registry = registry
        self.name = name
        self.glyph = glyph

    @property
    def custom_id(self) -> str | None:
        """id кастомного эмодзи или None, если сейчас нужен обычный."""
        return self._registry.custom_id(self.name)

    def __str__(self) -> str:
        custom = self.custom_id
        if custom is None:
            return self.glyph
        return f'<tg-emoji emoji-id="{custom}">{self.glyph}</tg-emoji>'

    def __repr__(self) -> str:
        return f"Icon({self.name!r})"


class EmojiRegistry:
    """Сопоставление имён и id, флаг включения и состояние «Telegram отклонил кастомные»."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}
        self._enabled = False
        self._degraded_reason: str | None = None
        for name, glyph, _ in _SPECS:
            setattr(self, name, Icon(self, name, glyph))

    def __getattr__(self, name: str) -> Icon:  # только для подсказки об опечатке
        raise AttributeError(f"нет эмодзи с именем {name!r}")

    def icon(self, name: str) -> Icon:
        icon = self.__dict__.get(name)
        if not isinstance(icon, Icon):
            raise KeyError(name)
        return icon

    # --- настройка ---

    def configure(self, ids: Mapping[str, str | None], *, enabled: bool) -> None:
        """Задаёт сопоставление. Строки, которые не похожи на id, отбрасываются."""
        clean: dict[str, str] = {}
        for name, value in ids.items():
            if name not in GLYPHS or value is None:
                continue
            if not (isinstance(value, str) and value.isascii() and value.isdecimal()):
                log.warning("emoji %r: %r is not a custom emoji id, ignored", name, value)
                continue
            clean[name] = value
        self._ids = clean
        self._enabled = enabled
        self._degraded_reason = None

    def reset(self) -> None:
        self.configure({}, enabled=False)

    def degrade(self, reason: str) -> bool:
        """Переходит на обычные эмодзи. True, если это случилось сейчас (а не уже было раньше)."""
        first = self._degraded_reason is None
        self._degraded_reason = reason
        return first

    def retry(self) -> None:
        """Снова пробует кастомные эмодзи, например после покупки Premium."""
        self._degraded_reason = None

    # --- состояние ---

    @property
    def enabled(self) -> bool:
        """Флаг USE_CUSTOM_EMOJI."""
        return self._enabled

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    @property
    def active(self) -> bool:
        return self._enabled and self._degraded_reason is None

    def mapping(self) -> dict[str, str | None]:
        """Имя → id для всех имён; None, если для имени id не задан."""
        return {name: self._ids.get(name) for name in NAMES}

    def custom_id(self, name: str) -> str | None:
        return self._ids.get(name) if self.active else None

    def glyph_for_id(self, custom_id: str) -> str | None:
        """Обычный эмодзи, которым заменяется кастомный id (для кнопок в запасном виде)."""
        for name, known in self._ids.items():
            if known == custom_id:
                return GLYPHS[name]
        return None

    # --- кнопки ---

    def button(
        self, text: str, *, icon: Icon | None = None, **fields: object
    ) -> InlineKeyboardButton:
        """Кнопка с иконкой. Кастомный эмодзи идёт отдельным полем и не дублируется в тексте;
        в запасном виде обычный эмодзи ставится перед текстом."""
        custom = icon.custom_id if icon is not None else None
        if custom is not None:
            return InlineKeyboardButton(text=text, icon_custom_emoji_id=custom, **fields)  # type: ignore[arg-type]
        label = f"{icon.glyph} {text}" if icon is not None else text
        return InlineKeyboardButton(text=label, **fields)  # type: ignore[arg-type]


E = EmojiRegistry()
