"""Настройки скриптов из scripts/. Читаются из того же .env, что и бот, но нужна только часть."""

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from plugbackup_bot.config import EMOJI_SET_RE, split_ids


class ToolSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = Field(min_length=1)
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    plans_file: Path = Path("../design/data/plans.json")
    assets_dir: Path = Path("../design/graphics/png")
    emoji_set: str = "RestrictedEmoji"
    emoji_pack_file: Path = Path("emoji_pack.json")
    emoji_map_file: Path = Path("emoji_map.json")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        return split_ids(value)

    @field_validator("emoji_set")
    @classmethod
    def _check_emoji_set(cls, value: str) -> str:
        if not EMOJI_SET_RE.fullmatch(value):
            raise ValueError("имя набора: латиница, цифры и «_»")
        return value
