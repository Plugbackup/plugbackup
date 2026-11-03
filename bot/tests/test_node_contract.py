"""Контракт бота и ноды: команды, которые собирает бот, понимает node/storectl, а ответ
storectl разбирается кодом бота. Проверяется без SSH и без ZFS."""

import base64
import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from plugbackup_bot.infra.storectl import (
    _EXIT_KINDS,
    ErrorKind,
    LoginEvent,
    SshStorectl,
    StorectlError,
    Usage,
)

SCRIPT = Path(__file__).resolve().parents[2] / "node" / "storectl"
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="node/storectl нет рядом с ботом")

PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
KEY = base64.b64encode(PREFIX + bytes(range(32))).decode()


def load_node_script() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("storectl_node", str(SCRIPT))
    spec = importlib.util.spec_from_loader("storectl_node", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["storectl_node"] = module
    loader.exec_module(module)
    return module


class FakeStore:
    """Хранилище, которое только записывает, что у него попросили."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def add(self, login, quota_gb, key):  # type: ignore[no-untyped-def]
        self.calls.append(("add", login, quota_gb, key))

    def resize(self, login, quota_gb):  # type: ignore[no-untyped-def]
        self.calls.append(("resize", login, quota_gb))

    def suspend(self, login):  # type: ignore[no-untyped-def]
        self.calls.append(("suspend", login))

    def resume(self, login):  # type: ignore[no-untyped-def]
        self.calls.append(("resume", login))

    def delete(self, login):  # type: ignore[no-untyped-def]
        self.calls.append(("delete", login))

    def usage(self, login):  # type: ignore[no-untyped-def]
        self.calls.append(("usage", login))
        return "1234\n53687091200\n"

    def logins(self, login, since):  # type: ignore[no-untyped-def]
        self.calls.append(("logins", login, since))
        return "1793600120 ok 3f9a1c5e07b2d481\n1793600150 fail 0b7e44d2a91c6f38\n"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path):  # type: ignore[no-untyped-def]
    """SshStorectl, у которого «ssh» заменён прямым вызовом storectl --ssh."""
    node = load_node_script()
    store = FakeStore()

    async def fake_run(self: SshStorectl, addr: str, args: list[str]) -> str:
        # sshd склеивает аргументы пробелами и кладёт в SSH_ORIGINAL_COMMAND.
        capsys.readouterr()
        code = node.main(["--ssh"], {"SSH_ORIGINAL_COMMAND": " ".join(args)}, store)
        captured = capsys.readouterr()
        if code != 0:
            kind = _EXIT_KINDS.get(code, ErrorKind.INTERNAL)
            raise StorectlError(kind, captured.err.strip(), exit_code=code)
        return captured.out

    monkeypatch.setattr(SshStorectl, "_run", fake_run)
    ssh = SshStorectl(user="provision", key_path=tmp_path / "k", known_hosts_path=tmp_path / "kh")
    return ssh, store, node


async def test_every_bot_command_is_understood_by_the_node(wired) -> None:  # type: ignore[no-untyped-def]
    ssh, store, _ = wired
    await ssh.add("192.0.2.10", "u1001", 50, KEY)
    await ssh.resize("192.0.2.10", "u1001", 100)
    await ssh.suspend("192.0.2.10", "u1001")
    await ssh.resume("192.0.2.10", "u1001")
    await ssh.delete("192.0.2.10", "u1001")
    assert store.calls == [
        ("add", "u1001", 50, KEY),
        ("resize", "u1001", 100),
        ("suspend", "u1001"),
        ("resume", "u1001"),
        ("delete", "u1001"),
    ]


async def test_usage_output_is_parsed_by_the_bot(wired) -> None:  # type: ignore[no-untyped-def]
    ssh, _, _ = wired
    assert await ssh.usage("192.0.2.10", "u1001") == Usage(1234, 53687091200)


async def test_logins_output_is_parsed_by_the_bot(wired) -> None:  # type: ignore[no-untyped-def]
    ssh, store, _ = wired
    events = await ssh.logins("192.0.2.10", "u1001", 1793600000)
    assert events == [
        LoginEvent(1793600120, True, "3f9a1c5e07b2d481"),
        LoginEvent(1793600150, False, "0b7e44d2a91c6f38"),
    ]
    assert store.calls[-1] == ("logins", "u1001", 1793600000)


def test_exit_codes_match_on_both_sides(wired) -> None:  # type: ignore[no-untyped-def]
    _, _, node = wired
    assert _EXIT_KINDS == {
        node.USAGE: ErrorKind.BAD_REQUEST,
        node.NOT_FOUND: ErrorKind.NOT_FOUND,
        node.CONFLICT: ErrorKind.CONFLICT,
        node.QUOTA_BELOW_USAGE: ErrorKind.QUOTA_BELOW_USAGE,
        node.INTERNAL: ErrorKind.INTERNAL,
        255: ErrorKind.UNREACHABLE,
    }


def test_both_sides_validate_keys_and_logins_identically(wired) -> None:  # type: ignore[no-untyped-def]
    """То, что бот пропускает, нода принимает, и наоборот (на границе форматов)."""
    from plugbackup_bot.domain.keys import InvalidKey, parse_public_key
    from plugbackup_bot.domain.logins import LOGIN_RE

    _, _, node = wired
    keys = [
        KEY,
        KEY[:-4],
        KEY + "AAAA",
        KEY + "\n",
        "AAAAB3NzaC1yc2EAAAADAQABAAABAQ" + "x" * 40,
        "x; reboot",
        "",
    ]
    for key in keys:
        try:
            bot_ok = parse_public_key(key) == key
        except InvalidKey:
            bot_ok = False
        try:
            node.check_key(key)
            node_ok = True
        except node.StoreError:
            node_ok = False
        assert bot_ok == node_ok, key

    for login in ["u1001", "u123456789", "u100", "u1234567890", "U1001", "u1001\n", "root", ""]:
        try:
            node.check_login(login)
            node_ok = True
        except node.StoreError:
            node_ok = False
        assert bool(LOGIN_RE.fullmatch(login)) == node_ok, login
