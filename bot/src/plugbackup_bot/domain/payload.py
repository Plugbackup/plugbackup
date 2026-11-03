"""Payload счёта на подписку: `v1:<тариф>:<id клиента или 0>:<nonce>`.

id клиента равен 0 для нового доступа и совпадает с id клиента, если подписка привязывается
к существующему хранилищу (смена тарифа, повторная подписка). Nonce делает payload каждого
счёта уникальным, по нему продления находят именно свою подписку. Лимит Telegram — 128 байт.
"""

import re
import secrets
from dataclasses import dataclass

_PLAN_ID_RE = re.compile(r"[a-z0-9_]{1,16}")
_PAYLOAD_RE = re.compile(r"v1:([a-z0-9_]{1,16}):([0-9]{1,9}):([0-9a-f]{8})")


@dataclass(frozen=True, slots=True)
class InvoicePayload:
    plan_id: str
    client_id: int
    nonce: str


def make_payload(plan_id: str, client_id: int = 0) -> str:
    if not _PLAN_ID_RE.fullmatch(plan_id) or not 0 <= client_id <= 999_999_999:
        raise ValueError("недопустимые данные для payload")
    return f"v1:{plan_id}:{client_id}:{secrets.token_hex(4)}"


def parse_payload(text: str) -> InvoicePayload | None:
    match = _PAYLOAD_RE.fullmatch(text)
    if match is None:
        return None
    return InvoicePayload(plan_id=match[1], client_id=int(match[2]), nonce=match[3])
