"""Правила жизненного цикла клиента: что делать с ним в данный момент времени.

`evaluate` ничего не пишет и никуда не ходит, она возвращает решение (`Step`). Применяет его
`services.lifecycle.LifecycleService`. Решение зависит только от состояния клиента и
времени, поэтому после простоя бота цикл догоняет сам.

Бета:    active -> (30 дней) expiring -> (+7 дней без «Я ещё пользуюсь») suspended
                  -> (+14 дней) deleted
Платные: active -> expiring, когда осталось 3 дня и продление выключено, либо период
         закончился -> (+3 дня льготы) suspended -> (+14 дней) deleted
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from plugbackup_bot.domain.policy import (
    BETA_GRACE_S,
    DAY,
    NOTICE_BEFORE_ACTION_S,
    PAID_GRACE_S,
    PENDING_TTL_S,
    SUSPENDED_TTL_S,
    WARN_BEFORE_S,
)
from plugbackup_bot.domain.states import State

if TYPE_CHECKING:
    from plugbackup_bot.db.clients import Client


class NoticeKind(StrEnum):
    KEY_REMINDER_1 = "key_reminder_1"
    KEY_REMINDER_3 = "key_reminder_3"
    BETA_EXPIRED = "beta_expired"
    BETA_PROMPT = "beta_prompt"
    WARN_3D = "warn_3d"
    WARN_1D = "warn_1d"
    RENEWAL_LATE = "renewal_late"
    SUSPEND_SOON = "suspend_soon"
    SUSPENDED = "suspended"
    DELETE_SOON = "delete_soon"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class Notice:
    """Обязательное уведомление. kind + key уникальны для клиента: повторно не отправляем."""

    kind: NoticeKind
    key: str
    is_beta: bool
    # Дата, о которой сообщение (блокировка, удаление, конец доступа), unix-секунды.
    at: int | None = None


@dataclass(frozen=True, slots=True)
class Step:
    to: State | None = None
    reason: str = ""
    # Операция storectl, которую надо поставить в очередь вместе с переходом.
    job: str | None = None
    notices: tuple[Notice, ...] = ()
    # Бета без ключа сгорела: снимаем отметку «бета использована».
    reset_beta: bool = False


def suspend_at(client: "Client", *, is_beta: bool) -> int:
    """Когда блокировать клиента в состоянии expiring. Не раньше чем через сутки после входа."""
    if is_beta:
        return client.state_since + BETA_GRACE_S
    end = client.expires_at if client.expires_at is not None else client.state_since
    return paid_suspend_at(end, client.state_since)


def paid_suspend_at(end: int, state_since: int) -> int:
    return max(end + PAID_GRACE_S, state_since + NOTICE_BEFORE_ACTION_S)


def delete_at(client: "Client") -> int:
    return client.state_since + SUSPENDED_TTL_S


def evaluate(client: "Client", now: int, *, beta_plan_id: str) -> Step | None:
    is_beta = client.plan_id == beta_plan_id
    match client.state:
        case State.PENDING:
            return _pending(client, now, is_beta)
        case State.ACTIVE:
            return _active(client, now, is_beta)
        case State.EXPIRING:
            return _expiring(client, now, is_beta)
        case State.SUSPENDED:
            return _suspended(client, now, is_beta)
    return None


def _pending(client: "Client", now: int, is_beta: bool) -> Step | None:
    if client.ssh_key_body is not None:
        return None  # ключ есть, add стоит в очереди
    age = now - client.state_since
    if is_beta and age >= PENDING_TTL_S:
        notice = Notice(NoticeKind.BETA_EXPIRED, str(client.state_since), True)
        return Step(State.DELETED, "no_key", notices=(notice,), reset_beta=True)
    if age >= 3 * DAY:
        return Step(notices=(Notice(NoticeKind.KEY_REMINDER_3, "3", is_beta),))
    if age >= DAY:
        return Step(notices=(Notice(NoticeKind.KEY_REMINDER_1, "1", is_beta),))
    return None


def _active(client: "Client", now: int, is_beta: bool) -> Step | None:
    if is_beta:
        due = client.confirm_due_at
        if due is None or now < due:
            return None
        notice = Notice(NoticeKind.BETA_PROMPT, str(due), True, at=now + BETA_GRACE_S)
        return Step(State.EXPIRING, "confirm_due", notices=(notice,))

    end = client.expires_at
    if end is None:
        return None
    if now >= end:
        notice = Notice(NoticeKind.RENEWAL_LATE, str(end), False, at=paid_suspend_at(end, now))
        return Step(State.EXPIRING, "period_over", notices=(notice,))
    if not client.renewal_on and end - now <= WARN_BEFORE_S[0]:
        kind = NoticeKind.WARN_1D if end - now <= WARN_BEFORE_S[1] else NoticeKind.WARN_3D
        return Step(State.EXPIRING, "renewal_off", notices=(Notice(kind, str(end), False, at=end),))
    return None


def _expiring(client: "Client", now: int, is_beta: bool) -> Step | None:
    deadline = suspend_at(client, is_beta=is_beta)
    if now >= deadline:
        notice = Notice(NoticeKind.SUSPENDED, str(deadline), is_beta, at=now + SUSPENDED_TTL_S)
        return Step(State.SUSPENDED, "grace_over", job="suspend", notices=(notice,))

    notices: list[Notice] = []
    if is_beta:
        if deadline - now <= WARN_BEFORE_S[0]:
            notices.append(Notice(NoticeKind.WARN_3D, str(deadline), True, at=deadline))
    elif client.expires_at is not None and not client.renewal_on:
        left = client.expires_at - now
        key = str(client.expires_at)
        if 0 < left <= WARN_BEFORE_S[1]:
            notices.append(Notice(NoticeKind.WARN_1D, key, False, at=client.expires_at))
        elif 0 < left <= WARN_BEFORE_S[0]:
            notices.append(Notice(NoticeKind.WARN_3D, key, False, at=client.expires_at))
    if deadline - now <= NOTICE_BEFORE_ACTION_S:
        notices.append(Notice(NoticeKind.SUSPEND_SOON, str(deadline), is_beta, at=deadline))
    return Step(notices=tuple(notices)) if notices else None


def _suspended(client: "Client", now: int, is_beta: bool) -> Step | None:
    if client.admin_hold:
        return None  # ручную блокировку автоматика не удаляет
    deadline = delete_at(client)
    if now >= deadline:
        notice = Notice(NoticeKind.DELETED, str(deadline), is_beta)
        return Step(State.DELETED, "ttl", job="delete", notices=(notice,))
    if deadline - now <= NOTICE_BEFORE_ACTION_S:
        notice = Notice(NoticeKind.DELETE_SOON, str(deadline), is_beta, at=deadline)
        return Step(notices=(notice,))
    return None
