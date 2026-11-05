"""Пак эмодзи: загрузка из Telegram, сопоставление с именами и чтение файлов.

Запускается скриптом scripts/scan_emoji.py. Результат: emoji_pack.json (весь пак) и
emoji_map.json (имя → id, правится вручную).
"""

import argparse
import asyncio
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot
from pydantic import ValidationError

from plugbackup_bot.emoji import CANDIDATES, GLYPHS, NAMES, plain_key
from plugbackup_bot.tool_settings import ToolSettings

log = logging.getLogger(__name__)

MAP_NOTE = (
    "Имя -> custom_emoji_id из emoji_pack.json. Чтобы заменить эмодзи, впишите другой id "
    "и перезапустите бота. null: подходящего эмодзи в паке нет, будет обычный. "
    "Посмотреть пак и текущее сопоставление можно командой /emoji_preview."
)


class EmojiToolError(Exception):
    """Понятная пользователю причина сбоя."""


@dataclass(frozen=True, slots=True)
class PackItem:
    emoji: str
    custom_emoji_id: str


@dataclass(frozen=True, slots=True)
class Pack:
    set_name: str
    title: str
    scanned_at: str
    items: tuple[PackItem, ...]


# --- пак ---


async def scan_pack(bot: Bot, set_name: str, *, now: datetime | None = None) -> Pack:
    """Читает набор через getStickerSet. Берутся только кастомные эмодзи."""
    sticker_set = await bot.get_sticker_set(set_name)
    items = tuple(
        PackItem(sticker.emoji or "", sticker.custom_emoji_id)
        for sticker in sticker_set.stickers
        if sticker.custom_emoji_id
    )
    if not items:
        raise EmojiToolError(f"в наборе {set_name} нет кастомных эмодзи")
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Pack(sticker_set.name, sticker_set.title, stamp, items)


def pack_to_json(pack: Pack) -> dict[str, object]:
    return {
        "set_name": pack.set_name,
        "title": pack.title,
        "scanned_at": pack.scanned_at,
        "count": len(pack.items),
        "emoji": [
            {"index": index, "emoji": item.emoji, "custom_emoji_id": item.custom_emoji_id}
            for index, item in enumerate(pack.items, start=1)
        ],
    }


def pack_from_json(data: object) -> Pack:
    broken = EmojiToolError("файл пака повреждён, запустите scripts/scan_emoji.py")
    if not isinstance(data, dict):
        raise broken
    try:
        items = tuple(
            PackItem(str(row["emoji"]), str(row["custom_emoji_id"])) for row in data["emoji"]
        )
        return Pack(str(data["set_name"]), str(data["title"]), str(data["scanned_at"]), items)
    except (KeyError, TypeError) as error:
        raise broken from error


def read_pack(path: Path) -> Pack:
    try:
        return pack_from_json(json.loads(path.read_text(encoding="utf-8")))
    except OSError as error:
        raise EmojiToolError(f"нет файла пака {path}, запустите scripts/scan_emoji.py") from error
    except ValueError as error:
        raise EmojiToolError(f"файл пака {path} не читается как JSON") from error


# --- сопоставление ---


def build_map(
    pack: Pack, existing: Mapping[str, str | None] | None = None, *, remap: bool = False
) -> dict[str, str | None]:
    """Имя → id. Что уже записано в existing (правки вручную), сохраняется, если не remap."""
    by_emoji: dict[str, str] = {}
    for item in pack.items:
        by_emoji.setdefault(plain_key(item.emoji), item.custom_emoji_id)
    result: dict[str, str | None] = {}
    for name in NAMES:
        if not remap and existing is not None and existing.get(name):
            result[name] = existing[name]
            continue
        result[name] = next(
            (by_emoji[key] for key in map(plain_key, CANDIDATES[name]) if key in by_emoji), None
        )
    return result


def read_map(path: Path) -> dict[str, str | None]:
    """Сопоставление из файла. Нет файла или он битый: пустое, бот работает с обычными эмодзи."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("emoji map %s not found, plain emoji are used", path)
        return {}
    except (OSError, ValueError):
        log.warning("emoji map %s is unreadable, plain emoji are used", path, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        log.warning("emoji map %s is not an object, plain emoji are used", path)
        return {}
    result: dict[str, str | None] = {}
    for name, value in raw.items():
        if name.startswith("_"):
            continue
        if name not in GLYPHS:
            log.warning("emoji map: unknown name %r ignored", name)
            continue
        result[name] = None if value is None else str(value)
    return result


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def map_to_json(mapping: Mapping[str, str | None]) -> dict[str, object]:
    return {"_note": MAP_NOTE, **{name: mapping.get(name) for name in NAMES}}


def describe_map(mapping: Mapping[str, str | None]) -> list[str]:
    return [
        f"  {name:<9}{GLYPHS[name]}  "
        + (mapping[name] if mapping.get(name) else "нет в паке, будет обычный эмодзи")
        for name in NAMES
    ]


# --- командная строка ---


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scan_emoji.py",
        description="Сохраняет пак эмодзи в emoji_pack.json и сопоставляет его с именами.",
    )
    parser.add_argument("--set", dest="set_name", help="имя набора (по умолчанию из EMOJI_SET)")
    parser.add_argument(
        "--remap",
        action="store_true",
        help="заново подобрать все эмодзи, затирая правки в emoji_map.json",
    )
    return parser


async def run(
    argv: Sequence[str] | None = None,
    *,
    bot_factory: Callable[[str], Bot] = Bot,
    out: Callable[[str], object] = print,
    now: datetime | None = None,
) -> int:
    """0 — файлы записаны, 1 — Telegram недоступен или набора нет, 2 — не хватает настроек."""
    args = _parser().parse_args(argv)
    try:
        settings = ToolSettings()  # type: ignore[call-arg]  # значения из окружения и .env
    except ValidationError as error:
        names = sorted({str(item["loc"][0]).upper() for item in error.errors() if item["loc"]})
        out(f"В .env не хватает или неверно: {', '.join(names)}.")
        return 2
    set_name = args.set_name or settings.emoji_set

    bot = bot_factory(settings.bot_token.get_secret_value())
    try:
        pack = await scan_pack(bot, set_name, now=now)
    except EmojiToolError as error:
        out(str(error))
        return 1
    except Exception as error:
        # Текст сетевых ошибок может содержать адрес запроса с токеном: печатаем только тип.
        out(f"Не удалось получить набор {set_name} ({type(error).__name__}). Проверьте имя набора.")
        return 1
    finally:
        await bot.session.close()

    write_json(settings.emoji_pack_file, pack_to_json(pack))
    existing = read_map(settings.emoji_map_file)
    mapping = build_map(pack, existing, remap=args.remap)
    write_json(settings.emoji_map_file, map_to_json(mapping))

    out(f"Набор {pack.set_name} «{pack.title}»: {len(pack.items)} эмодзи")
    out(f"Пак -> {settings.emoji_pack_file}")
    out(f"Сопоставление -> {settings.emoji_map_file}:")
    for line in describe_map(mapping):
        out(line)
    missing = [name for name in NAMES if not mapping[name]]
    if missing:
        out(f"\nНе нашлось в паке: {', '.join(missing)}. Впишите id вручную или оставьте обычные.")
    out("\nПроверьте внешний вид в Telegram: админ-команда /emoji_preview.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(argv))
