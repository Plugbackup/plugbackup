import pytest
from pydantic import ValidationError

from plugbackup_bot.config import Settings

TOKEN = "123456789:" + "x" * 35

BASE_ENV = {
    "BOT_TOKEN": TOKEN,
    "ADMIN_IDS": "111, 222",
    "ADMIN_CHAT_ID": "111",
    "PRICES_STARS": '{"gb50": 50, "gb100": 100, "gb200": 170}',
    "NODES": (
        '[{"name": "node1", "addr": "192.0.2.10",'
        ' "public_host": "node1.example.org", "capacity_gb": 1000}]'
    ),
    "SSH_KEY_PATH": "/etc/bot/key",
    "SSH_KNOWN_HOSTS_PATH": "/etc/bot/known_hosts",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def load() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def with_node(addr: str = "192.0.2.10", **extra: object) -> str:
    import json

    node = {"name": "node1", "addr": addr, "public_host": "node1.example.org", "capacity_gb": 10}
    node.update(extra)
    return json.dumps([node])


def test_valid_settings(env: pytest.MonkeyPatch) -> None:
    settings = load()
    assert settings.admin_ids == [111, 222]
    assert settings.nodes[0].oversell == 1.0
    assert settings.ssh_user == "provision"
    assert settings.prices_stars["gb100"] == 100


def test_token_is_not_printed(env: pytest.MonkeyPatch) -> None:
    settings = load()
    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings)
    assert settings.bot_token.get_secret_value() == TOKEN


def test_missing_token_fails(env: pytest.MonkeyPatch) -> None:
    env.delenv("BOT_TOKEN")
    with pytest.raises(ValidationError):
        load()


def test_empty_token_fails(env: pytest.MonkeyPatch) -> None:
    env.setenv("BOT_TOKEN", "")
    with pytest.raises(ValidationError, match="bot_token"):
        load()


def test_empty_admin_ids_fail(env: pytest.MonkeyPatch) -> None:
    env.setenv("ADMIN_IDS", " , ")
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize(
    "addr",
    [
        "-oProxyCommand=x",
        "host name",
        "a;b",
        "fe80::1%eth0",
        "",
        "UPPER.example",
        "a..b",
        "node1\n",
        "192.0.2.10\n",
    ],
)
def test_bad_node_address_is_rejected(env: pytest.MonkeyPatch, addr: str) -> None:
    env.setenv("NODES", with_node(addr))
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize("addr", ["192.0.2.10", "2001:db8::10", "node1.example.org", "wg-node"])
def test_good_node_address_is_accepted(env: pytest.MonkeyPatch, addr: str) -> None:
    env.setenv("NODES", with_node(addr))
    assert load().nodes[0].addr == addr


def test_bad_node_name_is_rejected(env: pytest.MonkeyPatch) -> None:
    env.setenv("NODES", with_node(name="Node 1"))
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize("price", [0, -1, 10_001])
def test_price_out_of_range(env: pytest.MonkeyPatch, price: int) -> None:
    env.setenv("PRICES_STARS", f'{{"gb50": {price}}}')
    with pytest.raises(ValidationError):
        load()


def test_unknown_timezone(env: pytest.MonkeyPatch) -> None:
    env.setenv("DEFAULT_TZ", "Mars/Olympus")
    with pytest.raises(ValidationError):
        load()


def test_env_example_is_a_valid_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пример из репозитория должен читаться как настройки: его копируют при установке."""
    from pathlib import Path

    for name in BASE_ENV:
        monkeypatch.delenv(name, raising=False)
    example = Path(__file__).resolve().parents[1] / ".env.example"
    settings = Settings(_env_file=example)  # type: ignore[call-arg]
    assert settings.admin_ids
    assert settings.nodes[0].addr == "192.0.2.10"  # адрес из диапазона для документации
    assert set(settings.prices_stars) == {"gb50", "gb100", "gb200"}
