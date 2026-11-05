"""Настройки бота. Читаются из переменных окружения или файла .env."""

import ipaddress
import re
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")
NODE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Короткое имя набора стикеров или эмодзи в Telegram.
EMOJI_SET_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# Верхний предел цены подписки в Telegram Stars из Bot API.
MAX_PRICE_STARS = 10_000


def split_ids(value: object) -> object:
    """«1,2,3» из .env превращает в список чисел; остальное отдаёт pydantic как есть."""
    if isinstance(value, str):
        return [int(part) for part in value.split(",") if part.strip()]
    return value


def is_hostname(value: str) -> bool:
    return HOSTNAME_RE.fullmatch(value) is not None


def is_ip(value: str) -> bool:
    if "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def is_valid_host(value: str) -> bool:
    """Годится для аргумента ssh: IP-адрес или имя хоста, без пробелов и ведущего «-»."""
    return is_ip(value) or is_hostname(value)


class NodeConfig(BaseModel):
    """Нода хранения. Адрес попадает в аргументы ssh, поэтому проверяется строго."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    addr: str
    public_host: str
    capacity_gb: int = Field(ge=0)
    oversell: float = Field(default=1.0, gt=0, le=10)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not NODE_NAME_RE.fullmatch(value):
            raise ValueError("имя ноды: латиница в нижнем регистре, цифры и дефис")
        return value

    @field_validator("addr")
    @classmethod
    def _check_addr(cls, value: str) -> str:
        if is_valid_host(value):
            return value
        raise ValueError("addr должен быть IP-адресом или именем хоста")

    @field_validator("public_host")
    @classmethod
    def _check_public_host(cls, value: str) -> str:
        if not is_hostname(value):
            raise ValueError("public_host должен быть именем хоста")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = Field(min_length=1)
    admin_ids: Annotated[list[int], NoDecode] = Field(min_length=1)
    admin_chat_id: int

    db_path: Path = Path("data/bot.db")
    plans_file: Path = Path("../design/data/plans.json")
    prices_stars: dict[str, int]
    nodes: list[NodeConfig] = Field(min_length=1)

    ssh_user: str = Field(default="provision", pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_key_path: Path
    ssh_known_hosts_path: Path
    storectl_timeout_s: int = Field(default=20, ge=1, le=300)

    # Кастомные эмодзи из пака. Telegram принимает их только если у владельца бота есть Premium;
    # при отказе бот сам переходит на обычные эмодзи.
    use_custom_emoji: bool = True
    # Баннеры экранов (приветствие, тарифы, оплата) и эффект сообщения при оплате и выдаче доступа.
    assets_dir: Path = Path("../design/graphics/png")
    message_effects: bool = True
    emoji_set: str = "RestrictedEmoji"
    emoji_pack_file: Path = Path("emoji_pack.json")
    emoji_map_file: Path = Path("emoji_map.json")

    default_tz: str = "Europe/Moscow"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        return split_ids(value)

    @field_validator("prices_stars")
    @classmethod
    def _check_prices(cls, value: dict[str, int]) -> dict[str, int]:
        for plan_id, price in value.items():
            if not 1 <= price <= MAX_PRICE_STARS:
                raise ValueError(f"цена {plan_id}: от 1 до {MAX_PRICE_STARS} Stars")
        return value

    @field_validator("emoji_set")
    @classmethod
    def _check_emoji_set(cls, value: str) -> str:
        if not EMOJI_SET_RE.fullmatch(value):
            raise ValueError("имя набора: латиница, цифры и «_»")
        return value

    @field_validator("default_tz")
    @classmethod
    def _check_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестный часовой пояс: {value}") from exc
        return value


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # значения берутся из окружения
