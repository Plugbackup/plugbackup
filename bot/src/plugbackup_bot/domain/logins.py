"""Логин клиента на ноде: u + число, id клиента плюс 1000 (первый клиент — u1001)."""

import re

LOGIN_RE = re.compile(r"^u[0-9]{4,9}$")
LOGIN_OFFSET = 1000


def make_login(client_id: int) -> str:
    if client_id < 1:
        raise ValueError("id клиента начинается с 1")
    return f"u{LOGIN_OFFSET + client_id}"


def parse_login(login: str) -> int | None:
    """id клиента по логину или None, если строка не похожа на логин."""
    if not LOGIN_RE.fullmatch(login):
        return None
    client_id = int(login[1:]) - LOGIN_OFFSET
    return client_id if client_id >= 1 else None
