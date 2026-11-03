from dataclasses import dataclass

import pytest
import pytest_asyncio

from fakes import FakeClock, FakeStorectl, RecordingMessenger
from helpers import add_client, add_node, add_user
from plugbackup_bot.db import Database
from plugbackup_bot.db import alerts as alerts_repo
from plugbackup_bot.db.usage import record_sample
from plugbackup_bot.domain.alerts import IP_HASH_LIMIT
from plugbackup_bot.domain.plans import GIB
from plugbackup_bot.infra.storectl import ErrorKind, LoginEvent, StorectlError
from plugbackup_bot.services.alerts import AlertEngine
from plugbackup_bot.tg.alert_texts import TelegramAlertRenderer

HOUR = 3600
DAY = 24 * HOUR
MONDAY_NOON_UTC = 1_793_620_800  # 2026-11-02 12:00 UTC = 15:00 MSK
QUOTA = 50 * GIB
TZ = "Europe/Moscow"


@dataclass
class Env:
    db: Database
    clock: FakeClock
    storectl: FakeStorectl
    messenger: RecordingMessenger
    engine: AlertEngine
    client_id: int
    node_id: int

    def texts(self) -> list[str]:
        return [text for _, text in self.messenger.sent]

    async def samples(self, *points: tuple[float, float], client_id: int | None = None) -> None:
        """Отсчёты: (часов назад, занято ГиБ). Время отсчитывается от текущих часов."""
        cid = client_id or self.client_id
        now = self.clock.now()
        async with self.db.transaction() as conn:
            for hours_ago, used in points:
                hour_ts = (now - int(hours_ago * HOUR)) // HOUR * HOUR
                await record_sample(conn, cid, hour_ts, int(used * GIB), QUOTA)

    async def flat(self, days: int, used: float = 5) -> None:
        await self.samples(*[(h, used) for h in range(days * 24 + 2, -1, -1)])

    async def enable(self, key: str, *, consent: bool = False, tg_id: int = 5) -> None:
        async with self.db.transaction() as conn:
            await alerts_repo.set_enabled(conn, tg_id, key, True, self.clock.now(), consent=consent)

    async def disable(self, key: str, tg_id: int = 5) -> None:
        async with self.db.transaction() as conn:
            await alerts_repo.set_enabled(conn, tg_id, key, False, self.clock.now())

    async def prefs(self, tg_id: int = 5, **changes: object) -> None:
        async with self.db.transaction() as conn:
            await alerts_repo.set_prefs(
                conn,
                tg_id,
                self.clock.now(),
                default_tz=TZ,
                **changes,  # type: ignore[arg-type]
            )

    def logins(self, *events: LoginEvent, login: str = "u1001") -> None:
        self.storectl.logins_by_login[login] = list(events)


@pytest_asyncio.fixture
async def env(db: Database, clock: FakeClock) -> Env:
    clock.set(MONDAY_NOON_UTC)
    node_id = await add_node(db, "node1")
    await add_user(db, 5)
    client_id = await add_client(db, 5, node_id)
    storectl, messenger = FakeStorectl(), RecordingMessenger()
    engine = AlertEngine(db, clock, storectl, messenger, TelegramAlertRenderer(), TZ)
    return Env(db, clock, storectl, messenger, engine, client_id, node_id)


# --- алерты по данным о месте ---


async def test_defaults_on_a_healthy_client_say_nothing(env: Env) -> None:
    await env.samples((2, 5), (1, 5.1), (0, 5.2))
    report = await env.engine.evaluate_all()
    assert (report.evaluated, report.sent, report.errors) == (1, 0, 0)
    assert env.messenger.sent == []


async def test_stale_backup_is_reported(env: Env) -> None:
    await env.flat(4)
    await env.engine.evaluate_all()
    [text] = env.texts()
    assert text.startswith("Занятое место не менялось 3 дн.")
    assert env.messenger.sent[0][0] == 5


async def test_stale_threshold_follows_the_setting(env: Env) -> None:
    async with env.db.transaction() as conn:
        await alerts_repo.set_threshold(conn, 5, "stale", 7, env.clock.now())
    await env.flat(4)
    await env.engine.evaluate_all()
    assert env.messenger.sent == []  # данных за семь суток ещё нет
    await env.flat(8)
    await env.engine.evaluate_all()
    assert env.texts()[0].startswith("Занятое место не менялось 7 дн.")


async def test_sharp_drop_is_reported_with_a_restore_button(env: Env) -> None:
    await env.samples((20, 10), (10, 10), (0, 3.5))
    await env.engine.evaluate_all()
    [text] = env.texts()
    assert "уменьшилось на 65%" in text
    assert "запросите восстановление" in text
    markup = env.messenger.markups[0]
    assert markup is not None
    pairs = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    assert pairs == [
        ("Запросить восстановление", "restore:start"),
        ("Отключить этот алерт", "al:off:drop"),
    ]


async def test_every_alert_has_a_disable_button(env: Env) -> None:
    await env.flat(4)
    await env.engine.evaluate_all()
    markup = env.messenger.markups[0]
    assert markup is not None
    pairs = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    assert pairs == [("Отключить этот алерт", "al:off:stale")]


async def test_quota_fill_levels(env: Env) -> None:
    await env.samples((0, 46))
    await env.engine.evaluate_all()
    assert env.texts()[-1].startswith("Хранилище заполнено на 90%")

    env.clock.advance(DAY)
    await env.samples((0, 50))
    await env.engine.evaluate_all()
    assert env.texts()[-1].startswith("Хранилище заполнено на 100%")
    assert "/upgrade" in env.texts()[-1]
    assert len(env.messenger.sent) == 2  # на 100% не приходит ещё и «90%»


async def test_spike_is_off_until_enabled(env: Env) -> None:
    await env.samples((23, 5), (12, 20), (0, 36))
    await env.engine.evaluate_all()
    assert env.messenger.sent == []
    await env.enable("spike")
    await env.engine.evaluate_all()
    assert "За сутки загружено" in env.texts()[-1]


async def test_disabled_alerts_stay_silent(env: Env) -> None:
    await env.flat(4)
    await env.disable("stale")
    await env.engine.evaluate_all()
    assert env.messenger.sent == []


# --- тихие часы ---


async def test_quiet_hours_hold_back_ordinary_alerts_until_morning(env: Env) -> None:
    env.clock.set(MONDAY_NOON_UTC + 8 * HOUR + 1800)  # 23:30 по Москве
    await env.flat(4)
    await env.engine.evaluate_all()
    assert env.messenger.sent == []

    env.clock.set(MONDAY_NOON_UTC + 8 * HOUR + 1800 + 9 * HOUR)  # 08:30
    await env.flat(4)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 1  # отложенный алерт не потерян


async def test_critical_alerts_ignore_quiet_hours(env: Env) -> None:
    env.clock.set(MONDAY_NOON_UTC + 12 * HOUR)  # 03:00 по Москве
    await env.samples((20, 10), (0, 3))
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 1
    assert "уменьшилось" in env.texts()[0]


async def test_quiet_hours_can_be_turned_off(env: Env) -> None:
    await env.prefs(quiet=(0, 0))
    env.clock.set(MONDAY_NOON_UTC + 12 * HOUR)
    await env.flat(4)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 1


async def test_quiet_hours_use_the_client_timezone(env: Env) -> None:
    await env.prefs(tz="Asia/Vladivostok")  # 12:00 UTC = 22:00 там, тихие часы ещё не начались
    await env.flat(4)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 1


async def test_the_same_moment_is_quiet_in_another_timezone(env: Env) -> None:
    await env.prefs(tz="Asia/Vladivostok")
    env.clock.advance(HOUR)  # 23:00 во Владивостоке
    await env.flat(4)
    await env.engine.evaluate_all()
    assert env.messenger.sent == []


# --- кулдаун ---


async def test_same_alert_is_not_repeated_within_the_cooldown(env: Env) -> None:
    await env.flat(4)
    await env.engine.evaluate_all()
    env.clock.advance(HOUR)
    await env.flat(4)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 1

    env.clock.advance(24 * HOUR)
    await env.flat(4)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 2


async def test_cooldown_is_configurable(env: Env) -> None:
    await env.prefs(cooldown_hours=6)
    await env.flat(4)
    await env.engine.evaluate_all()
    env.clock.advance(5 * HOUR)
    await env.flat(4)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 1
    env.clock.advance(HOUR)
    await env.flat(4)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 2


async def test_growing_usage_is_not_stale(env: Env) -> None:
    await env.samples(*[(h, 5 + (100 - h) * 0.42) for h in range(100, -1, -1)])  # к концу 47 ГиБ
    await env.engine.evaluate_all()
    [text] = env.texts()
    assert text.startswith("Хранилище заполнено на 90%")


# --- 5. неудачные входы ---


def fail(ts: int) -> LoginEvent:
    return LoginEvent(ts, False, "0123456789abcdef")


async def test_failed_logins_alert_and_idempotent_counting(env: Env) -> None:
    await env.enable("fails")
    async with env.db.transaction() as conn:
        await alerts_repo.set_threshold(conn, 5, "fails", 5, env.clock.now())
    now = env.clock.now()
    env.logins(*[fail(now - 60 * i) for i in range(1, 9)])
    await env.engine.evaluate_all()
    [text] = env.texts()
    assert "8 неудачных попыток входа" in text
    assert "/key" in text

    # Повторный опрос тех же событий не удваивает счётчик.
    await env.engine.evaluate_all()
    async with env.db.read() as conn:
        counts = await alerts_repo.fail_counts(conn, env.client_id, 0)
    assert sum(counts.values()) == 8
    assert len(env.messenger.sent) == 1


async def test_few_failed_logins_are_ignored(env: Env) -> None:
    await env.enable("fails")  # порог по умолчанию 20
    now = env.clock.now()
    env.logins(*[fail(now - 60 * i) for i in range(1, 6)])
    await env.engine.evaluate_all()
    assert env.messenger.sent == []


async def test_logins_are_not_requested_unless_needed(env: Env) -> None:
    await env.flat(1)
    await env.engine.evaluate_all()
    assert [c for c in env.storectl.calls if c[0] == "logins"] == []


async def test_node_errors_while_reading_logins_are_tolerated(env: Env) -> None:
    await env.enable("fails")
    env.storectl.fail("logins", StorectlError(ErrorKind.UNREACHABLE, "ssh: timeout"))
    report = await env.engine.evaluate_all()
    assert (report.evaluated, report.errors) == (1, 0)


async def test_unexpected_errors_stay_inside_one_client(env: Env) -> None:
    await env.enable("fails")
    await add_user(env.db, 6)
    other = await add_client(env.db, 6, env.node_id)
    await env.samples((20, 10), (0, 3), client_id=other)
    env.storectl.fail("logins", RuntimeError("boom"))
    report = await env.engine.evaluate_all()
    assert report.errors == 1
    assert report.evaluated == 1
    assert any("уменьшилось" in text for text in env.texts())


# --- 6. новый IP ---


def ok(ts: int, ip_hash: str) -> LoginEvent:
    return LoginEvent(ts, True, ip_hash)


H1, H2, H3 = "a1b2c3d4e5f60718", "0011223344556677", "ffeeddccbbaa9988"


async def test_new_ip_requires_explicit_consent(env: Env) -> None:
    with pytest.raises(ValueError, match="согласия"):
        await env.enable("new_ip")
    await env.engine.evaluate_all()
    assert [c for c in env.storectl.calls if c[0] == "logins"] == []


async def test_new_ip_learns_a_baseline_then_alerts_only_on_news(env: Env) -> None:
    await env.enable("new_ip", consent=True)
    now = env.clock.now()
    env.logins(ok(now - 3 * DAY, H1), ok(now - DAY, H2))

    await env.engine.evaluate_all()
    assert env.messenger.sent == []  # первая выгрузка только запоминается
    assert env.storectl.calls[-1][-1] == now - 7 * DAY  # неделя истории
    async with env.db.read() as conn:
        assert await alerts_repo.known_hash_count(conn, env.client_id) == 2
        assert await alerts_repo.new_ip_hashes(conn, env.client_id) == []

    env.clock.advance(HOUR)
    env.logins(ok(now - 3 * DAY, H1), ok(env.clock.now() - 120, H3))
    await env.engine.evaluate_all()
    [text] = env.texts()
    assert "IP-адреса" in text
    assert "метка ffeeddcc" in text
    assert "/key" in text
    assert "203." not in text  # адресов бот не знает

    env.clock.advance(HOUR)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 1  # тот же адрес повторно не показывается


async def test_new_ip_ignores_failed_attempts_and_known_hashes(env: Env) -> None:
    await env.enable("new_ip", consent=True)
    now = env.clock.now()
    env.logins(ok(now - 100, H1))
    await env.engine.evaluate_all()
    env.clock.advance(HOUR)
    env.logins(LoginEvent(env.clock.now() - 60, False, H2), ok(env.clock.now() - 30, H1))
    await env.engine.evaluate_all()
    assert env.messenger.sent == []


async def test_new_ip_is_critical_and_ignores_quiet_hours(env: Env) -> None:
    await env.enable("new_ip", consent=True)
    env.logins(ok(env.clock.now() - 100, H1))
    await env.engine.evaluate_all()
    env.clock.set(MONDAY_NOON_UTC + 13 * HOUR)  # 04:00 по Москве
    env.logins(ok(env.clock.now() - 60, H2))
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 1


async def test_a_held_back_new_ip_is_delivered_later(env: Env) -> None:
    await env.enable("new_ip", consent=True)
    env.logins(ok(env.clock.now() - 100, H1))
    await env.engine.evaluate_all()
    for hash_value in (H2, H3):
        env.clock.advance(HOUR)
        env.logins(ok(env.clock.now() - 60, hash_value))
        await env.engine.evaluate_all()
    # H2 показан сразу, H3 попал под кулдаун и ждёт.
    assert len(env.messenger.sent) == 1
    async with env.db.read() as conn:
        assert await alerts_repo.new_ip_hashes(conn, env.client_id) == [H3]
    env.clock.advance(24 * HOUR)
    await env.engine.evaluate_all()
    assert len(env.messenger.sent) == 2


async def test_disabling_new_ip_erases_the_hashes(env: Env) -> None:
    await env.enable("new_ip", consent=True)
    env.logins(ok(env.clock.now() - 100, H1))
    await env.engine.evaluate_all()
    await env.disable("new_ip")
    async with env.db.read() as conn:
        assert await alerts_repo.known_hash_count(conn, env.client_id) == 0
        settings = await alerts_repo.get_settings(conn, 5)
    assert settings["new_ip"].enabled is False
    assert settings["new_ip"].consent_at is None


async def test_hash_storage_is_bounded(env: Env) -> None:
    now = env.clock.now()
    seen = {f"{i:016x}": now - i * HOUR for i in range(IP_HASH_LIMIT + 10)}
    async with env.db.transaction() as conn:
        await alerts_repo.record_ip_hashes(conn, env.client_id, seen, now, baseline=True)
        assert await alerts_repo.known_hash_count(conn, env.client_id) == IP_HASH_LIMIT
        old = {"deadbeefdeadbeef": now - 31 * DAY}
        await alerts_repo.record_ip_hashes(conn, env.client_id, old, now, baseline=True)
        assert await alerts_repo.known_hash_count(conn, env.client_id) == IP_HASH_LIMIT
    async with (
        env.db.read() as conn,
        conn.execute(
            "SELECT COUNT(*) FROM ip_hashes WHERE last_seen < ?", (now - 30 * DAY,)
        ) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0


# --- 7. состояние сервиса ---


async def add_event(
    env: Env, kind: str, text: str, *, starts: int = -60, ends: int | None = None,
    node_id: int | None = -1,
) -> None:  # fmt: skip
    now = env.clock.now()
    target = env.node_id if node_id == -1 else node_id
    async with env.db.transaction() as conn:
        await conn.execute(
            "INSERT INTO service_events (kind, node_id, text, starts_at, ends_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (kind, target, text, now + starts, None if ends is None else now + ends),
        )


async def test_maintenance_notice_reaches_the_node_once(env: Env) -> None:
    await add_event(env, "maintenance", "перезагрузка <ноды> в 03:00")
    assert await env.engine.process_service_events() == 1
    assert await env.engine.process_service_events() == 0
    [text] = env.texts()
    assert text == "Плановые работы: перезагрузка &lt;ноды&gt; в 03:00"


async def test_service_notice_respects_the_setting_and_the_node(env: Env) -> None:
    other_node = await add_node(env.db, "node2")
    await add_user(env.db, 6)
    await add_client(env.db, 6, other_node)
    await add_event(env, "maintenance", "работы")
    await env.disable("service")
    assert await env.engine.process_service_events() == 0

    await env.enable("service")
    assert await env.engine.process_service_events() == 1
    assert [tg for tg, _ in env.messenger.sent] == [5]


async def test_global_notice_reaches_every_node(env: Env) -> None:
    other_node = await add_node(env.db, "node2")
    await add_user(env.db, 6)
    await add_client(env.db, 6, other_node)
    await add_event(env, "restore", "восстановление после сбоя", node_id=None)
    assert await env.engine.process_service_events() == 2
    assert all(text.startswith("Восстановление:") for text in env.texts())


async def test_service_notice_waits_out_quiet_hours(env: Env) -> None:
    env.clock.set(MONDAY_NOON_UTC + 12 * HOUR)  # 03:00 по Москве
    await add_event(env, "maintenance", "работы", starts=-3600)
    assert await env.engine.process_service_events() == 0
    env.clock.advance(6 * HOUR)  # 09:00
    assert await env.engine.process_service_events() == 1


async def test_outage_has_a_start_and_an_end_message(env: Env) -> None:
    await add_event(env, "node_down", "", starts=-1800)
    await env.engine.process_service_events()
    assert "недоступна больше 15 минут" in env.texts()[-1]

    env.clock.advance(1200)
    async with env.db.transaction() as conn:
        await conn.execute("UPDATE service_events SET ends_at = ?", (env.clock.now() - 10,))
    assert await env.engine.process_service_events() == 1
    assert env.texts()[-1] == "Нода хранения снова доступна."
    assert await env.engine.process_service_events() == 0


async def test_old_events_are_not_delivered(env: Env) -> None:
    await add_event(env, "maintenance", "давно", starts=-2 * DAY)
    assert await env.engine.process_service_events() == 0


async def test_future_events_wait_for_their_time(env: Env) -> None:
    await add_event(env, "maintenance", "завтра", starts=3600)
    assert await env.engine.process_service_events() == 0


async def test_finished_event_sends_start_and_end_together(env: Env) -> None:
    await env.disable("service")
    await add_event(env, "node_down", "", starts=-1800, ends=-60)
    assert await env.engine.process_service_events() == 0
    await env.enable("service")
    assert await env.engine.process_service_events() == 2
    assert "недоступна" in env.texts()[0]
    assert env.texts()[1] == "Нода хранения снова доступна."
    assert await env.engine.process_service_events() == 0


# --- 8. сводка за неделю ---


async def test_weekly_digest_once_a_week_on_monday(env: Env) -> None:
    await env.enable("weekly")
    await env.samples((7 * 24, 4), (3, 6), (0, 7))
    assert await env.engine.weekly_digest() == 1
    assert await env.engine.weekly_digest() == 0
    [text] = env.texts()
    assert text.startswith("Сводка за неделю")
    assert "Занято: 7,0 ГБ из 50,0 ГБ" in text
    assert "За неделю: +3,0 ГБ" in text
    assert "Последнее изменение размера:" in text

    env.clock.advance(7 * DAY)
    assert await env.engine.weekly_digest() == 1


async def test_digest_is_off_by_default_and_not_sent_midweek(env: Env) -> None:
    await env.samples((0, 7))
    assert await env.engine.weekly_digest() == 0
    await env.enable("weekly")
    env.clock.advance(DAY)  # вторник
    assert await env.engine.weekly_digest() == 0
    env.clock.set(MONDAY_NOON_UTC - 6 * HOUR)  # понедельник 09:00 UTC = 12:00 MSK
    assert await env.engine.weekly_digest() == 1


async def test_digest_without_data_says_so(env: Env) -> None:
    await env.enable("weekly")
    assert await env.engine.weekly_digest() == 1
    assert "данных пока нет" in env.texts()[0]
