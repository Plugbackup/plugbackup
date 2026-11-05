import pytest

from plugbackup_bot.db.usage import Sample
from plugbackup_bot.domain.alerts import (
    ALERTS,
    BY_KEY,
    DAY,
    DROP_MIN_BASELINE_BYTES,
    eval_drop,
    eval_fails,
    eval_fill,
    eval_spike,
    eval_stale,
    is_digest_time,
    is_quiet,
    last_change_ts,
    threshold_label,
    week_key,
)
from plugbackup_bot.domain.plans import GIB

HOUR = 3600
T = 1_793_620_800  # 2026-11-02 12:00 UTC, понедельник, 15:00 по Москве
QUOTA = 50 * GIB


def s(hours_ago: float, used_gib: float, quota: int = QUOTA) -> Sample:
    return Sample(1, T - int(hours_ago * HOUR), int(used_gib * GIB), quota)


def flat(days: int, used_gib: float = 5, extra_hours: int = 2) -> list[Sample]:
    """Ровная история за `days` суток плюс запас, последний отсчёт свежий."""
    return [s(h, used_gib) for h in range(days * 24 + extra_hours, -1, -1)]


# --- каталог ---


def test_defaults_match_the_spec() -> None:
    defaults = {a.key: (a.default_enabled, a.default_threshold) for a in ALERTS}
    assert defaults == {
        "stale": (True, 3),
        "drop": (True, 50),
        "spike": (False, 20),
        "fill_80": (False, None),
        "fill_90": (True, None),
        "fill_100": (True, None),
        "fails": (False, 20),
        "new_ip": (False, None),
        "service": (True, None),
        "weekly": (False, None),
    }


def test_critical_alerts() -> None:
    assert {a.key for a in ALERTS if a.critical} == {"drop", "fill_100", "fails", "new_ip"}


def test_threshold_options_from_the_spec() -> None:
    assert BY_KEY["stale"].options == (1, 3, 7)
    assert BY_KEY["drop"].options == (25, 50, 75)
    assert set(BY_KEY["spike"].options) >= {5, 20, 50, -10, -25, -50}


@pytest.mark.parametrize(
    ("key", "value", "label"),
    [
        ("stale", 3, "3\u00a0дн."),
        ("drop", 50, "больше 50%"),
        ("spike", 20, "больше 20\u00a0ГБ"),
        ("spike", -25, "больше 25% квоты"),
        ("fails", 20, "больше 20 в час"),
        ("new_ip", None, ""),
    ],
)
def test_threshold_labels(key: str, value: int | None, label: str) -> None:
    assert threshold_label(key, value) == label


# --- 1. бэкап давно не приходил ---


@pytest.mark.parametrize("days", [1, 3, 7])
def test_flat_usage_is_stale(days: int) -> None:
    finding = eval_stale(flat(days), days, T)
    assert finding is not None
    assert (finding.key, finding.critical, finding.data["days"]) == ("stale", False, days)


def test_any_change_resets_staleness() -> None:
    samples = flat(4)
    samples[-5] = Sample(1, samples[-5].hour_ts, samples[-5].used_bytes + 1, QUOTA)
    assert eval_stale(samples, 3, T) is None


def test_stale_needs_history_as_long_as_the_window() -> None:
    assert eval_stale(flat(2), 3, T) is None  # клиент моложе трёх суток
    assert eval_stale([], 3, T) is None


def test_stale_ignores_silent_nodes() -> None:
    old = [Sample(1, x.hour_ts - 10 * HOUR, x.used_bytes, QUOTA) for x in flat(4)]
    assert eval_stale(old, 3, T) is None


def test_old_changes_outside_the_window_do_not_count() -> None:
    samples = [s(24 * 6, 1), *flat(3, used_gib=5, extra_hours=2)]
    assert eval_stale(samples, 3, T) is not None


# --- 2. резкое уменьшение ---


def test_drop_over_threshold() -> None:
    finding = eval_drop([s(20, 10), s(10, 10), s(0, 4)], 50, T)
    assert finding is not None
    assert finding.critical
    assert finding.data["percent"] == 60
    assert (finding.data["from"], finding.data["to"]) == (10 * GIB, 4 * GIB)


def test_drop_at_or_below_threshold_is_quiet() -> None:
    assert eval_drop([s(10, 10), s(0, 5)], 50, T) is None  # ровно 50%
    assert eval_drop([s(10, 10), s(0, 6)], 50, T) is None
    assert eval_drop([s(10, 10), s(0, 6)], 25, T) is not None


def test_drop_ignores_tiny_repositories() -> None:
    tiny = DROP_MIN_BASELINE_BYTES - 1
    samples = [Sample(1, T - HOUR, tiny, QUOTA), Sample(1, T, 0, QUOTA)]
    assert eval_drop(samples, 25, T) is None


def test_old_drop_is_not_reported_again() -> None:
    samples = [s(60, 10), s(40, 4), s(20, 4), s(0, 4)]
    assert eval_drop(samples, 50, T) is None  # падение было двое суток назад


def test_drop_needs_a_fresh_sample() -> None:
    assert eval_drop([s(30, 10), s(5, 1)], 50, T) is None


# --- 3. большая загрузка ---


def test_spike_in_gigabytes() -> None:
    samples = [s(23, 5), s(12, 20), s(0, 36)]
    assert eval_spike(samples, 20, T) is not None
    assert eval_spike(samples, 50, T) is None


def test_spike_in_percent_of_quota() -> None:
    samples = [s(23, 5), s(0, 20)]  # +15 ГиБ = 30% от 50
    assert eval_spike(samples, -25, T) is not None
    assert eval_spike(samples, -50, T) is None


def test_spike_needs_history_and_fresh_data() -> None:
    assert eval_spike([s(0, 30)], 5, T) is None
    assert eval_spike([s(30, 1), s(10, 30)], 5, T) is None


# --- 4. заполнение ---


@pytest.mark.parametrize(
    ("used", "levels", "expected"),
    [
        (47.5, {80, 90, 100}, "fill_90"),
        (50, {80, 90, 100}, "fill_100"),
        (50, {80, 90}, "fill_90"),
        (42, {80}, "fill_80"),
        (42, {90, 100}, None),
        (49.99, {100}, None),
        (30, {80, 90, 100}, None),
        (60, {100}, "fill_100"),
    ],
)
def test_fill_levels(used: float, levels: set[int], expected: str | None) -> None:
    finding = eval_fill([s(0, used)], levels, T)
    assert (finding.key if finding else None) == expected


def test_only_the_full_quota_is_critical() -> None:
    assert eval_fill([s(0, 50)], {90, 100}, T).critical  # type: ignore[union-attr]
    assert not eval_fill([s(0, 46)], {90, 100}, T).critical  # type: ignore[union-attr]


def test_fill_ignores_stale_samples_and_zero_quota() -> None:
    assert eval_fill([s(5, 50)], {100}, T) is None
    assert eval_fill([Sample(1, T, 5, 0)], {100}, T) is None
    assert eval_fill([], {100}, T) is None


# --- 5. неудачные входы ---


def test_failed_logins_over_the_limit() -> None:
    current = T // HOUR * HOUR
    assert eval_fails({current: 25}, 20, T) is not None
    assert eval_fails({current: 20}, 20, T) is None
    assert eval_fails({current - HOUR: 30}, 20, T) is not None  # прошлый час тоже считается
    assert eval_fails({current - 2 * HOUR: 99}, 20, T) is None
    assert eval_fails({}, 5, T) is None


# --- тихие часы и время ---


@pytest.mark.parametrize(
    ("hour_msk", "quiet"),
    [(23, True), (0, True), (3, True), (7, True), (8, False), (12, False), (22, False)],
)
def test_default_quiet_hours(hour_msk: int, quiet: bool) -> None:
    now = T - 15 * HOUR + hour_msk * HOUR  # 00:00 MSK + hour
    assert is_quiet(now, "Europe/Moscow", 23, 8) is quiet


def test_other_quiet_windows() -> None:
    midnight = T - 15 * HOUR
    assert is_quiet(midnight + 22 * HOUR + 1800, "Europe/Moscow", 22, 7)
    assert is_quiet(midnight + 3 * HOUR, "Europe/Moscow", 0, 6)
    assert not is_quiet(midnight + 6 * HOUR, "Europe/Moscow", 0, 6)
    assert not any(is_quiet(midnight + h * HOUR, "UTC", 0, 0) for h in range(24))


def test_quiet_hours_follow_the_client_timezone() -> None:
    assert not is_quiet(T, "Europe/Moscow", 23, 8)  # 15:00
    assert is_quiet(T, "Asia/Vladivostok", 23, 8) is False  # 22:00
    assert is_quiet(T + HOUR, "Asia/Vladivostok", 23, 8)  # 23:00


def test_weekly_digest_window() -> None:
    assert week_key(T, "Europe/Moscow") == "2026-W45"
    assert is_digest_time(T, "Europe/Moscow")  # понедельник, 15:00
    assert not is_digest_time(T - 7 * HOUR, "Europe/Moscow")  # 08:00
    assert not is_digest_time(T + DAY, "Europe/Moscow")  # вторник
    assert week_key(T + 7 * DAY, "Europe/Moscow") == "2026-W46"


def test_last_change() -> None:
    assert last_change_ts([]) is None
    assert last_change_ts([s(5, 1)]) is None
    assert last_change_ts([s(5, 1), s(4, 1), s(3, 1)]) is None
    samples = [s(5, 1), s(4, 2), s(3, 2), s(2, 2)]
    assert last_change_ts(samples) == T - 4 * HOUR
