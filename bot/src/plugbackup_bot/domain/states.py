"""Состояния клиента и допустимые переходы между ними.

Это только правила. Менять состояние в базе можно единственным способом:
`plugbackup_bot.db.clients.transition`, он же пишет журнал переходов.

    pending   оплата или инвайт есть, `storectl add` ещё не выполнен
    active    доступ включён, продление ожидается
    expiring  доступ включён, но продолжение не гарантировано
    suspended на ноде выполнен suspend, данные лежат
    deleted   конечное состояние, логин больше не используется
"""

from enum import StrEnum


class State(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    EXPIRING = "expiring"
    SUSPENDED = "suspended"
    DELETED = "deleted"


ALLOWED: dict[State, frozenset[State]] = {
    State.PENDING: frozenset({State.ACTIVE, State.DELETED}),
    State.ACTIVE: frozenset({State.EXPIRING, State.SUSPENDED}),
    State.EXPIRING: frozenset({State.ACTIVE, State.SUSPENDED}),
    State.SUSPENDED: frozenset({State.ACTIVE, State.DELETED}),
    State.DELETED: frozenset(),
}

# Состояния, в которых на ноде есть рабочий датасет клиента.
LIVE = frozenset({State.ACTIVE, State.EXPIRING, State.SUSPENDED})

ADMIN_PREFIX = "admin:"


def admin_actor(tg_id: int) -> str:
    return f"{ADMIN_PREFIX}{tg_id}"


class InvalidTransition(Exception):
    def __init__(self, client_id: int, src: State, dst: State) -> None:
        super().__init__(f"клиент {client_id}: переход {src} -> {dst} недопустим")
        self.client_id = client_id
        self.src = src
        self.dst = dst


class StaleState(InvalidTransition):
    """Клиент уже не в том состоянии, из которого вызывающий собирался перейти."""


def is_allowed(src: State, dst: State, actor: str) -> bool:
    if dst in ALLOWED[src]:
        return True
    # Админ может удалить клиента из любого живого состояния.
    return dst is State.DELETED and src in LIVE and actor.startswith(ADMIN_PREFIX)
