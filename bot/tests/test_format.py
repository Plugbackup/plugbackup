from plugbackup_bot.tg.format import NBSP, fmt_bytes, fmt_date, fmt_datetime, gb

GIB = 1024**3


def test_number_and_unit_stay_together() -> None:
    assert gb(50) == f"50{NBSP}ГБ"


def test_bytes_are_scaled() -> None:
    assert fmt_bytes(0) == f"0{NBSP}КБ"
    assert fmt_bytes(512 * 1024) == f"512{NBSP}КБ"
    assert fmt_bytes(5 * 1024**2) == f"5{NBSP}МБ"
    assert fmt_bytes(int(12.34 * GIB)) == f"12,3{NBSP}ГБ"


def test_dates_use_the_given_timezone() -> None:
    ts = 1_793_620_800  # 2026-11-02 12:00 UTC
    assert fmt_date(ts, "Europe/Moscow") == "2 ноября 2026"
    assert fmt_datetime(ts, "Europe/Moscow") == "2 ноября 2026, 15:00"
    assert fmt_datetime(ts, "UTC") == "2 ноября 2026, 12:00"
    # За полночь по Москве дата уже следующая.
    assert fmt_date(ts + 10 * 3600, "Europe/Moscow") == "3 ноября 2026"
