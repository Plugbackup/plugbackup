import pytest

from plugbackup_bot.devrun import GIB, InMemoryStorectl, always_up, main
from plugbackup_bot.infra.storectl import ErrorKind, StorectlError

ADDR = "192.0.2.10"


async def test_add_usage_resize_and_delete() -> None:
    store = InMemoryStorectl()
    await store.add(ADDR, "u1001", 50, "key")
    usage = await store.usage(ADDR, "u1001")
    assert (usage.used_bytes, usage.quota_bytes) == (int(12.5 * GIB), 50 * GIB)

    await store.resize(ADDR, "u1001", 100)
    assert (await store.usage(ADDR, "u1001")).quota_bytes == 100 * GIB

    await store.delete(ADDR, "u1001")
    await store.delete(ADDR, "u1001")  # повтор безопасен
    with pytest.raises(StorectlError) as info:
        await store.usage(ADDR, "u1001")
    assert info.value.kind is ErrorKind.NOT_FOUND


async def test_quota_cannot_drop_below_usage() -> None:
    store = InMemoryStorectl()
    await store.add(ADDR, "u1001", 50, "key")  # занято 12,5 ГиБ
    with pytest.raises(StorectlError) as info:
        await store.resize(ADDR, "u1001", 10)
    assert info.value.kind is ErrorKind.QUOTA_BELOW_USAGE
    with pytest.raises(StorectlError):
        await store.add(ADDR, "u1001", 10, "key")


async def test_suspension_survives_a_key_replacement() -> None:
    store = InMemoryStorectl()
    await store.add(ADDR, "u1001", 50, "old")
    await store.suspend(ADDR, "u1001")
    await store.add(ADDR, "u1001", 50, "new")
    assert store.clients["u1001"]["suspended"] is True
    assert store.clients["u1001"]["key"] == "new"
    await store.resume(ADDR, "u1001")
    assert store.clients["u1001"]["suspended"] is False


async def test_unknown_clients() -> None:
    store = InMemoryStorectl()
    for call in (store.suspend, store.resume):
        with pytest.raises(StorectlError):
            await call(ADDR, "u1001")
    with pytest.raises(StorectlError):
        await store.logins(ADDR, "u1001", 0)
    await store.add(ADDR, "u1001", 1, "k")
    assert await store.logins(ADDR, "u1001", 0) == []


async def test_probe_is_always_up() -> None:
    assert await always_up(None) is True  # type: ignore[arg-type]


def test_devrun_refuses_to_start_without_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("BOT_TOKEN", "ADMIN_IDS", "ADMIN_CHAT_ID", "NODES", "PRICES_STARS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # здесь нет .env
    assert main() == 2
    assert "bot_token" in capsys.readouterr().err
