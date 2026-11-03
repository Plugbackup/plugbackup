"""Правила жизненного цикла как таблица: состояние клиента + время -> решение."""

import pytest

from helpers import NOW, fake_key_body
from plugbackup_bot.db.clients import Client
from plugbackup_bot.domain.lifecycle import NoticeKind, Step, evaluate
from plugbackup_bot.domain.policy import DAY
from plugbackup_bot.domain.states import State

KEY = fake_key_body(0)
HOUR = 3600


def mk(
    state: State,
    *,
    plan: str = "gb50",
    key: str | None = KEY,
    expires: int | None = None,
    due: int | None = None,
    since: int = NOW,
    renewal: bool = True,
    hold: bool = False,
) -> Client:
    return Client(
        id=1,
        tg_id=5,
        node_id=1,
        plan_id=plan,
        quota_gb=50,
        state=state,
        ssh_key_body=key,
        expires_at=expires,
        confirm_due_at=due,
        state_since=since,
        sub_charge_id=None,
        sub_payload=None,
        renewal_on=renewal,
        admin_hold=hold,
        created_at=since,
    )


def ev(client: Client, now: int) -> Step | None:
    return evaluate(client, now, beta_plan_id="beta")


def kinds(step: Step | None) -> list[NoticeKind]:
    return [] if step is None else [n.kind for n in step.notices]


# --- pending ---


def test_pending_with_a_key_waits_for_the_queue() -> None:
    assert ev(mk(State.PENDING), NOW + 30 * DAY) is None


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (HOUR, []),
        (DAY - 1, []),
        (DAY, [NoticeKind.KEY_REMINDER_1]),
        (3 * DAY - 1, [NoticeKind.KEY_REMINDER_1]),
        (3 * DAY, [NoticeKind.KEY_REMINDER_3]),
        (7 * DAY - 1, [NoticeKind.KEY_REMINDER_3]),
    ],
)
def test_pending_beta_without_a_key_gets_reminders(age: int, expected: list[NoticeKind]) -> None:
    step = ev(mk(State.PENDING, plan="beta", key=None), NOW + age)
    assert kinds(step) == expected
    assert step is None or step.to is None


def test_pending_beta_without_a_key_is_cancelled_after_a_week() -> None:
    step = ev(mk(State.PENDING, plan="beta", key=None), NOW + 7 * DAY)
    assert step is not None
    assert (step.to, step.reason, step.job, step.reset_beta) == (
        State.DELETED,
        "no_key",
        None,
        True,
    )
    assert kinds(step) == [NoticeKind.BETA_EXPIRED]


def test_paid_pending_is_never_cancelled() -> None:
    step = ev(mk(State.PENDING, key=None), NOW + 60 * DAY)
    assert step is not None
    assert step.to is None
    assert kinds(step) == [NoticeKind.KEY_REMINDER_3]


# --- active: бета ---


def test_beta_is_quiet_until_the_question_is_due() -> None:
    client = mk(State.ACTIVE, plan="beta", due=NOW + 30 * DAY)
    assert ev(client, NOW + 30 * DAY - 1) is None
    assert ev(mk(State.ACTIVE, plan="beta", due=None), NOW + 99 * DAY) is None


def test_beta_question_starts_the_grace_window() -> None:
    due = NOW + 30 * DAY
    step = ev(mk(State.ACTIVE, plan="beta", due=due), due + 5)
    assert step is not None
    assert (step.to, step.reason) == (State.EXPIRING, "confirm_due")
    [notice] = step.notices
    assert notice.kind is NoticeKind.BETA_PROMPT
    assert notice.is_beta
    assert notice.at == due + 5 + 7 * DAY


# --- active: платные ---


def test_paid_without_an_end_date_is_left_alone() -> None:
    assert ev(mk(State.ACTIVE, expires=None), NOW + 99 * DAY) is None


@pytest.mark.parametrize("offset", [30 * DAY, 4 * DAY, 3 * DAY + 1])
def test_paid_far_from_the_end_is_quiet(offset: int) -> None:
    end = NOW + offset
    assert ev(mk(State.ACTIVE, expires=end, renewal=False), NOW) is None


def test_auto_renewing_client_is_quiet_before_the_end() -> None:
    end = NOW + 2 * DAY
    assert ev(mk(State.ACTIVE, expires=end, renewal=True), NOW) is None


@pytest.mark.parametrize(
    ("left", "kind"),
    [(3 * DAY, NoticeKind.WARN_3D), (2 * DAY, NoticeKind.WARN_3D), (DAY, NoticeKind.WARN_1D)],
)
def test_cancelled_subscription_warns_before_the_end(left: int, kind: NoticeKind) -> None:
    end = NOW + left
    step = ev(mk(State.ACTIVE, expires=end, renewal=False), NOW)
    assert step is not None
    assert (step.to, step.reason) == (State.EXPIRING, "renewal_off")
    assert kinds(step) == [kind]
    assert step.notices[0].key == str(end)
    assert step.notices[0].at == end


def test_late_renewal_gets_a_grace_period() -> None:
    end = NOW + 10 * DAY
    step = ev(mk(State.ACTIVE, expires=end, renewal=True), end + HOUR)
    assert step is not None
    assert (step.to, step.reason) == (State.EXPIRING, "period_over")
    assert kinds(step) == [NoticeKind.RENEWAL_LATE]
    assert step.notices[0].at == end + 3 * DAY


# --- expiring ---


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (3 * DAY, []),
        (4 * DAY, [NoticeKind.WARN_3D]),
        (6 * DAY, [NoticeKind.WARN_3D, NoticeKind.SUSPEND_SOON]),
    ],
)
def test_beta_reminders_before_suspension(age: int, expected: list[NoticeKind]) -> None:
    step = ev(mk(State.EXPIRING, plan="beta", due=NOW), NOW + age)
    assert kinds(step) == expected
    assert step is None or step.to is None


def test_beta_is_suspended_a_week_after_the_question() -> None:
    step = ev(mk(State.EXPIRING, plan="beta"), NOW + 7 * DAY)
    assert step is not None
    assert (step.to, step.job) == (State.SUSPENDED, "suspend")
    assert kinds(step) == [NoticeKind.SUSPENDED]
    assert step.notices[0].at == NOW + 7 * DAY + 14 * DAY


def paid_expiring(end: int, since: int, renewal: bool = False) -> Client:
    return mk(State.EXPIRING, expires=end, since=since, renewal=renewal)


def test_cancelled_paid_client_walks_through_warnings_to_suspension() -> None:
    end = NOW + 10 * DAY
    since = end - 3 * DAY
    client = paid_expiring(end, since)
    assert kinds(ev(client, end - 4 * DAY)) == []
    assert kinds(ev(client, end - 2 * DAY)) == [NoticeKind.WARN_3D]
    assert kinds(ev(client, end - 12 * HOUR)) == [NoticeKind.WARN_1D]
    assert kinds(ev(client, end + DAY)) == []  # льгота, до блокировки ещё двое суток
    assert kinds(ev(client, end + 2 * DAY)) == [NoticeKind.SUSPEND_SOON]
    step = ev(client, end + 3 * DAY)
    assert step is not None
    assert (step.to, step.job) == (State.SUSPENDED, "suspend")


def test_there_is_always_a_day_of_notice_before_suspension() -> None:
    """Если бот долго простоял, клиента не блокируют в ту же минуту, когда он стал expiring."""
    client = paid_expiring(end=NOW - 10 * DAY, since=NOW, renewal=True)
    step = ev(client, NOW + HOUR)
    assert step is not None
    assert step.to is None
    assert kinds(step) == [NoticeKind.SUSPEND_SOON]
    suspend = ev(client, NOW + DAY)
    assert suspend is not None
    assert suspend.to is State.SUSPENDED


# --- suspended ---


def test_suspended_client_is_deleted_after_two_weeks() -> None:
    client = mk(State.SUSPENDED)
    assert ev(client, NOW + 12 * DAY) is None
    assert kinds(ev(client, NOW + 13 * DAY)) == [NoticeKind.DELETE_SOON]
    step = ev(client, NOW + 14 * DAY)
    assert step is not None
    assert (step.to, step.job) == (State.DELETED, "delete")
    assert kinds(step) == [NoticeKind.DELETED]


def test_manual_hold_is_never_deleted_automatically() -> None:
    assert ev(mk(State.SUSPENDED, hold=True), NOW + 365 * DAY) is None


def test_deleted_clients_are_ignored() -> None:
    assert ev(mk(State.DELETED), NOW + 365 * DAY) is None
