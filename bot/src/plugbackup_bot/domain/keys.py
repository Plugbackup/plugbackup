"""Проверка публичного ключа ssh-ed25519.

Тело ключа — единственный пользовательский текст, который попадает в команду storectl.
Поэтому кроме регулярки из ТЗ тело декодируется и сверяется с точным форматом ключа.
"""

import base64
import binascii
import hashlib
import re
from enum import StrEnum

KEY_BODY_RE = re.compile(r"^AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/=]+$")

# Формат blob по RFC 4253: строка "ssh-ed25519", затем 32 байта ключа.
_BLOB_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
_BLOB_LEN = len(_BLOB_PREFIX) + 32
_RSA_BODY_PREFIX = "AAAAB3NzaC1yc2E"
MAX_INPUT_LEN = 600


class KeyProblem(StrEnum):
    EMPTY = "empty"
    TOO_LONG = "too_long"
    PRIVATE_KEY = "private_key"
    NOT_ED25519 = "not_ed25519"
    BAD_FORMAT = "bad_format"


class InvalidKey(ValueError):
    def __init__(self, problem: KeyProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


def parse_public_key(text: str) -> str:
    """Принимает строку из id_ed25519.pub или голое тело ключа, возвращает тело.

    Комментарий после ключа отбрасывается и дальше нигде не используется.
    """
    text = text.strip()
    if not text:
        raise InvalidKey(KeyProblem.EMPTY)
    if len(text) > MAX_INPUT_LEN:
        raise InvalidKey(KeyProblem.TOO_LONG)
    if "PRIVATE KEY" in text:
        raise InvalidKey(KeyProblem.PRIVATE_KEY)
    if "\n" in text or "\r" in text:
        raise InvalidKey(KeyProblem.BAD_FORMAT)

    parts = text.split()
    if len(parts) == 1:
        body = parts[0]
    elif parts[0] == "ssh-ed25519":
        body = parts[1]
    elif parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise InvalidKey(KeyProblem.NOT_ED25519)
    else:
        raise InvalidKey(KeyProblem.BAD_FORMAT)

    if body.startswith(_RSA_BODY_PREFIX):
        raise InvalidKey(KeyProblem.NOT_ED25519)
    if not KEY_BODY_RE.fullmatch(body):
        raise InvalidKey(KeyProblem.BAD_FORMAT)
    try:
        blob = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidKey(KeyProblem.BAD_FORMAT) from exc
    canonical = base64.b64encode(blob).decode("ascii") == body
    if len(blob) != _BLOB_LEN or not blob.startswith(_BLOB_PREFIX) or not canonical:
        raise InvalidKey(KeyProblem.BAD_FORMAT)
    return body


def fingerprint(body: str) -> str:
    """Отпечаток SHA256 в том виде, как его показывает `ssh-keygen -l`."""
    digest = hashlib.sha256(base64.b64decode(body)).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
