"""Логирование с вычищением токенов, ключей и секретов из итогового текста."""

import logging
import re

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Токен бота, в том числе внутри URL вида .../bot<токен>/getUpdates.
    (re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"), "[bot-token]"),
    (re.compile(r"ssh-ed25519\s+[A-Za-z0-9+/=]+"), "ssh-ed25519 [ssh-key]"),
    (re.compile(r"AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/=]+"), "[ssh-key]"),
    # Длинные hex-строки: секреты и хеши.
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[hex]"),
    (
        re.compile(r"(?i)\b(secret|token|password|passwd|api[_-]?key)(\s*[=:]\s*)\S+"),
        r"\1\2[redacted]",
    ),
)

_handler: logging.Handler | None = None


def redact(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Применяет вычищение ко всей строке, включая traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(level: str = "INFO") -> None:
    """Вывод в stderr (его забирает journald). Повторный вызов не дублирует обработчик."""
    global _handler
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    _handler = logging.StreamHandler()
    _handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(_handler)
    root.setLevel(level)
