import asyncio
import os
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import SetMyCommands
from aiogram.types import ErrorEvent, Update

from fakes import FakeClock, FakeStorectl, make_bot
from plugbackup_bot.__main__ import main
from plugbackup_bot.config import Settings
from plugbackup_bot.db.nodes import Node, list_nodes
from plugbackup_bot.runtime import (
    ERROR_ALERT_GAP_S,
    _error_handler,
    build_runtime,
    check_environment,
    configure_bot,
)

REPO_PLANS = Path(__file__).resolve().parents[2] / "design" / "data" / "plans.json"
TOKEN = "123456789:" + "x" * 35
REQUIRED_UPDATES = {"message", "callback_query", "pre_checkout_query", "subscription"}

pytestmark = pytest.mark.skipif(not REPO_PLANS.exists(), reason="нет design/data/plans.json")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    key = tmp_path / "provision_key"
    key.write_text("placeholder", encoding="utf-8")
    if os.name == "posix":
        key.chmod(0o600)
    (tmp_path / "known_hosts").write_text("", encoding="utf-8")
    values = {
        "BOT_TOKEN": TOKEN,
        "ADMIN_IDS": "900",
        "ADMIN_CHAT_ID": "777",
        "DB_PATH": str(tmp_path / "data" / "bot.db"),
        "PLANS_FILE": str(REPO_PLANS),
        "PRICES_STARS": '{"gb50": 50, "gb100": 100, "gb200": 170}',
        "NODES": (
            '[{"name": "node1", "addr": "192.0.2.10", "public_host": "node1.example.org",'
            ' "capacity_gb": 1000}]'
        ),
        "SSH_KEY_PATH": str(key),
        "SSH_KNOWN_HOSTS_PATH": str(tmp_path / "known_hosts"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return tmp_path


def load() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def up(node: Node) -> bool:
    return True


# --- предзапусковая проверка ---


def test_a_healthy_environment_has_no_problems(env: Path) -> None:
    assert check_environment(load()) == []


def test_missing_files_are_reported(env: Path) -> None:
    (env / "provision_key").unlink()
    (env / "known_hosts").unlink()
    problems = check_environment(load())
    assert any("SSH-ключа" in p for p in problems)
    assert any("known_hosts" in p for p in problems)


def test_plan_problems_are_reported(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICES_STARS", '{"gb50": 50}')
    assert any("gb100" in p for p in check_environment(load()))


@pytest.mark.skipif(os.name != "posix", reason="права доступа проверяются только на Linux")
def test_a_world_readable_key_is_reported(env: Path) -> None:
    (env / "provision_key").chmod(0o644)
    assert any("0600" in p for p in check_environment(load()))


def test_check_mode(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--check"]) == 0
    assert "Проверка пройдена. Схема базы: v1" in capsys.readouterr().out
    (env / "known_hosts").unlink()
    assert main(["--check"]) == 1
    assert "known_hosts" in capsys.readouterr().err


def test_invalid_settings_are_explained_without_leaking_values(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("NODES")
    monkeypatch.setenv("PRICES_STARS", '{"gb50": 999999}')
    assert main(["--check"]) == 2
    err = capsys.readouterr().err
    assert "nodes" in err
    assert "prices_stars" in err
    assert TOKEN not in err
    assert "999999" not in err


# --- сборка ---


async def test_runtime_assembly(env: Path) -> None:
    bot, _ = make_bot()
    runtime = await build_runtime(load(), bot, clock=FakeClock(), storectl=FakeStorectl(), probe=up)
    try:
        async with runtime.db.read() as conn:
            [node] = await list_nodes(conn)
        assert (node.name, node.addr, node.capacity_gb) == ("node1", "192.0.2.10", 1000)
        assert await runtime.db.user_version() == 1
        names = {task.name for task in runtime.scheduler._tasks}
        assert names == {
            "provisioning",
            "lifecycle",
            "usage",
            "service_events",
            "weekly_digest",
            "health",
            "maintenance",
        }
    finally:
        await runtime.close()


async def test_every_scheduled_task_runs_on_an_empty_database(env: Path) -> None:
    bot, _ = make_bot()
    runtime = await build_runtime(load(), bot, clock=FakeClock(), storectl=FakeStorectl(), probe=up)
    try:
        for task in runtime.scheduler._tasks:
            # Планировщик глотает исключения, поэтому вызываем задачу напрямую.
            await task.func()
    finally:
        await runtime.close()


async def test_payments_and_subscriptions_are_among_the_polled_updates(env: Path) -> None:
    bot, _ = make_bot()
    runtime = await build_runtime(load(), bot, clock=FakeClock(), storectl=FakeStorectl())
    try:
        assert REQUIRED_UPDATES <= set(runtime.dp.resolve_used_update_types())
    finally:
        await runtime.close()


async def test_kicking_the_provisioner_runs_it_in_the_background(env: Path) -> None:
    bot, _ = make_bot()
    runtime = await build_runtime(load(), bot, clock=FakeClock(), storectl=FakeStorectl())
    try:
        runtime.dp.workflow_data["kick_provisioning"]()
        assert len(runtime.background) == 1
        await asyncio.gather(*runtime.background)
        await asyncio.sleep(0)
        assert runtime.background == set()
    finally:
        await runtime.close()


async def test_commands_menu_is_set_and_failures_are_tolerated() -> None:
    bot, session = make_bot()
    await configure_bot(bot)
    [method] = session.of(SetMyCommands)
    assert [c.command for c in method.commands][:2] == ["start", "status"]

    session.errors.append(TelegramAPIError(method, "boom"))
    await configure_bot(bot)  # не бросает


# --- обработчик ошибок ---


async def test_error_alerts_are_rate_limited_and_hide_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alerts: list[str] = []

    class Notifier:
        async def alert(self, text: str) -> None:
            alerts.append(text)

    clock = {"now": 1000.0}
    monkeypatch.setattr("plugbackup_bot.runtime.time.monotonic", lambda: clock["now"])
    handler = _error_handler(Notifier())  # type: ignore[arg-type]
    event = ErrorEvent(update=Update(update_id=1), exception=RuntimeError("password=hunter2"))

    assert await handler(event) is True
    assert await handler(event) is True
    assert alerts == ["Необработанная ошибка в обработчике: RuntimeError. Подробности в журнале."]

    clock["now"] += ERROR_ALERT_GAP_S
    await handler(event)
    assert len(alerts) == 2
