"""Алерты безопасности: каталог, настройки по умолчанию и чистые правила проверки.

Правила ничего не читают и не пишут: получают отсчёты занятого места (по часам) и порог,
возвращают `Finding` или None. Доставкой, тихими часами и кулдауном занимается
`services.alerts`.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from plugbackup_bot import texts_ru as T
from plugbackup_bot.db.usage import Sample
from plugbackup_bot.domain.plans import GIB

HOUR = 3600
DAY = 24 * HOUR
# Любое изменение занятого места считаем активностью: даже «пустой» запуск restic пишет снимок.
STALE_TOLERANCE_BYTES = 0
# Меньше этого не считаем: удаление пары мелких файлов не должно пугать клиента.
DROP_MIN_BASELINE_BYTES = 100 * 1024**2
# Отсчёт старше этого срока считаем устаревшим: нода молчит, судить не по чему.
FRESH_SAMPLE_S = 2 * HOUR

# Бот хранит не больше 20 хешей IP на клиента и не дольше 30 дней.
IP_HASH_LIMIT = 20
IP_HASH_RETENTION_S = 30 * DAY

# Обязательные уведомления: отключить нельзя, на экране /alerts они показаны отдельно.
MANDATORY_NOTICES = T.MANDATORY_NOTICES


@dataclass(frozen=True, slots=True)
class AlertDef:
    key: str
    title: str
    description: str
    default_enabled: bool
    default_threshold: int | None = None
    # Допустимые пороги. Для spike положительное число — ГБ, отрицательное — % от квоты.
    options: tuple[int, ...] = ()
    # Критичные алерты приходят и в тихие часы.
    critical: bool = False


def _alert(
    key: str,
    default_enabled: bool,
    default_threshold: int | None = None,
    options: tuple[int, ...] = (),
    *,
    critical: bool = False,
) -> AlertDef:
    """Название и описание алерта лежат в texts_ru.ALERT_CATALOG."""
    title, description = T.ALERT_CATALOG[key]
    return AlertDef(key, title, description, default_enabled, default_threshold, options, critical)


ALERTS: tuple[AlertDef, ...] = (
    _alert("stale", True, 3, (1, 3, 7)),
    _alert("drop", True, 50, (25, 50, 75), critical=True),
    _alert("spike", False, 20, (5, 20, 50, -10, -25, -50)),
    _alert("fill_80", False),
    _alert("fill_90", True),
    _alert("fill_100", True, critical=True),
    _alert("fails", False, 20, (5, 20, 50), critical=True),
    _alert("new_ip", False, critical=True),
    _alert("service", True),
    _alert("weekly", False),
)

BY_KEY: dict[str, AlertDef] = {alert.key: alert for alert in ALERTS}

COOLDOWN_OPTIONS = (6, 12, 24, 72)
# Варианты «тихих часов»: (с, до). Одинаковые значения отключают тихие часы.
QUIET_OPTIONS = ((23, 8), (22, 7), (0, 6), (0, 0))
# Часовые пояса на выбор: (подпись, имя IANA).
TIMEZONES = tuple((label, name) for name, label in T.TIMEZONE_LABELS.items())


@dataclass(frozen=True, slots=True)
class Finding:
    key: str
    critical: bool
    data: dict[str, int | str] = field(default_factory=dict)


def threshold_label(key: str, value: int | None) -> str:
    if value is None:
        return ""
    if key == "stale":
        return T.THRESHOLD_STALE.format(value=value)
    if key == "drop":
        return T.THRESHOLD_DROP.format(value=value)
    if key == "spike":
        if value > 0:
            return T.THRESHOLD_SPIKE_GB.format(value=value)
        return T.THRESHOLD_SPIKE_PERCENT.format(value=-value)
    if key == "fails":
        return T.THRESHOLD_FAILS.format(value=value)
    return str(value)


# --- правила ---


def eval_stale(samples: Sequence[Sample], days: int, now: int) -> Finding | None:
    """Занятое место не менялось `days` суток. Нужна история не короче окна."""
    window = days * DAY
    if not samples or samples[0].hour_ts > now - window + FRESH_SAMPLE_S:
        return None
    if samples[-1].hour_ts < now - FRESH_SAMPLE_S:
        return None
    used = [s.used_bytes for s in samples if s.hour_ts >= now - window]
    if max(used) - min(used) > STALE_TOLERANCE_BYTES:
        return None
    return Finding("stale", False, {"days": days})


def eval_drop(samples: Sequence[Sample], percent: int, now: int) -> Finding | None:
    """За сутки занятое место упало больше чем на `percent` процентов от суточного максимума."""
    if not samples or samples[-1].hour_ts < now - FRESH_SAMPLE_S:
        return None
    current = samples[-1].used_bytes
    peak = max(s.used_bytes for s in samples if s.hour_ts >= now - DAY)
    if peak < DROP_MIN_BASELINE_BYTES:
        return None
    dropped = (peak - current) * 100 / peak
    if dropped <= percent:
        return None
    return Finding("drop", True, {"percent": round(dropped), "from": peak, "to": current})


def eval_spike(samples: Sequence[Sample], threshold: int, now: int) -> Finding | None:
    """За сутки занятое место выросло больше порога (ГБ или % квоты)."""
    window = [s for s in samples if s.hour_ts >= now - DAY - HOUR]
    if len(window) < 2 or window[-1].hour_ts < now - FRESH_SAMPLE_S:
        return None
    rise = window[-1].used_bytes - min(s.used_bytes for s in window)
    quota = window[-1].quota_bytes
    if threshold > 0:
        exceeded = rise > threshold * GIB
    else:
        exceeded = quota > 0 and rise * 100 > -threshold * quota
    if not exceeded:
        return None
    return Finding("spike", False, {"rise": rise, "threshold": threshold})


def eval_fill(samples: Sequence[Sample], enabled_levels: Iterable[int], now: int) -> Finding | None:
    """Самый высокий из включённых порогов заполнения (80, 90, 100), который достигнут."""
    if not samples or samples[-1].hour_ts < now - FRESH_SAMPLE_S:
        return None
    sample = samples[-1]
    levels = set(enabled_levels)
    for level in (100, 90, 80):
        if level in levels and sample.used_bytes * 100 >= level * sample.quota_bytes > 0:
            data = {"percent": level, "used": sample.used_bytes, "quota": sample.quota_bytes}
            return Finding(f"fill_{level}", level == 100, data)
    return None


def eval_fails(buckets: dict[int, int], limit: int, now: int) -> Finding | None:
    """Неудачных входов за последний час больше `limit` (текущий и прошлый часовой отсчёт)."""
    current = now // HOUR * HOUR
    count = max(buckets.get(current, 0), buckets.get(current - HOUR, 0))
    if count <= limit:
        return None
    return Finding("fails", True, {"count": count})


# --- время ---


def local_hour(now: int, tz: str) -> int:
    return datetime.fromtimestamp(now, ZoneInfo(tz)).hour


def is_quiet(now: int, tz: str, quiet_from: int, quiet_to: int) -> bool:
    """Попадает ли момент в тихие часы клиента. Одинаковые границы — тихих часов нет."""
    if quiet_from == quiet_to:
        return False
    hour = local_hour(now, tz)
    if quiet_from < quiet_to:
        return quiet_from <= hour < quiet_to
    return hour >= quiet_from or hour < quiet_to


def week_key(now: int, tz: str) -> str:
    year, week, _ = datetime.fromtimestamp(now, ZoneInfo(tz)).isocalendar()
    return f"{year}-W{week:02d}"


def is_digest_time(now: int, tz: str) -> bool:
    """Сводка уходит в понедельник с 9 утра по времени клиента."""
    moment = datetime.fromtimestamp(now, ZoneInfo(tz))
    return moment.weekday() == 0 and moment.hour >= 9


def last_change_ts(samples: Sequence[Sample]) -> int | None:
    """Время последнего отсчёта, на котором занятое место изменилось. None, если не менялось."""
    for previous, current in zip(reversed(samples[:-1]), reversed(samples[1:]), strict=True):
        if current.used_bytes != previous.used_bytes:
            return current.hour_ts
    return None
