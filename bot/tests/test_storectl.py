"""SshStorectl: проверка аргументов, разбор ответов и запуск процесса.

Вместо ssh запускается маленький Python-скрипт, который по аргументам изображает ноду.
"""

import base64
import sys
import textwrap
from pathlib import Path

import pytest

from plugbackup_bot.infra.storectl import (
    ErrorKind,
    LoginEvent,
    SshStorectl,
    StorectlError,
    Usage,
    parse_logins,
    parse_usage,
)

PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
KEY = base64.b64encode(PREFIX + bytes(range(32))).decode()
ADDR = "192.0.2.10"
HASH = "0123456789abcdef"

FAKE_SSH = textwrap.dedent(
    """
    import sys, time
    argv = sys.argv[1:]
    rest = argv[argv.index("--") + 1:]
    addr, cmd = rest[0], rest[1:]
    op = cmd[0]
    with open(__file__ + ".log", "a", encoding="utf-8") as f:
        f.write(repr(rest) + "\\n")
    if op == "usage":
        print(1000); print(5000)
    elif op == "logins":
        print("1760000100 fail " + "0123456789abcdef")
        print("1760000200 ok " + "fedcba9876543210")
    elif op == "resize":
        sys.stderr.write("quota below usage"); sys.exit(5)
    elif op == "delete":
        sys.stderr.write("ssh: connect to host refused"); sys.exit(255)
    elif op == "suspend":
        time.sleep(30)
    elif op == "resume":
        sys.stderr.write("echo: " + " ".join(cmd)); sys.exit(6)
    elif op == "add":
        sys.stderr.write("got " + " ".join(cmd)); sys.exit(4)
    """
)


@pytest.fixture
def ssh(tmp_path: Path) -> SshStorectl:
    script = tmp_path / "fake_ssh.py"
    script.write_text(FAKE_SSH, encoding="utf-8")
    return SshStorectl(
        user="provision",
        key_path=tmp_path / "key",
        known_hosts_path=tmp_path / "known_hosts",
        port=2222,
        timeout_s=1.5,
        ssh_command=(sys.executable, str(script)),
    )


def spawned(tmp_path: Path) -> list[str]:
    log = tmp_path / "fake_ssh.py.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


async def test_commands_reach_the_node_as_plain_arguments(ssh: SshStorectl, tmp_path: Path) -> None:
    assert await ssh.usage(ADDR, "u1001") == Usage(1000, 5000)
    assert spawned(tmp_path) == [repr([ADDR, "usage", "u1001"])]


def test_ssh_command_is_hardened() -> None:
    ssh = SshStorectl(
        user="provision",
        key_path=Path("/k"),
        known_hosts_path=Path("/kh"),
        port=2222,
        timeout_s=20,
    )
    command = ssh.build_command(ADDR, ["add", "u1001", "50G", KEY])
    assert command[0] == "ssh"
    assert all(isinstance(part, str) for part in command)
    assert "BatchMode=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "ConnectTimeout=10" in command
    # Адрес и команда идут после «--», ключ отдельным аргументом.
    assert command[-6:] == ["--", ADDR, "add", "u1001", "50G", KEY]
    assert command[command.index("-p") + 1] == "2222"
    assert command[command.index("-l") + 1] == "provision"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.usage(ADDR, "u1001; reboot"),
        lambda s: s.usage(ADDR, "root"),
        lambda s: s.usage(ADDR, "u1001\n"),
        lambda s: s.usage("-oProxyCommand=x", "u1001"),
        lambda s: s.usage("host name", "u1001"),
        lambda s: s.add(ADDR, "u1001", 0, KEY),
        lambda s: s.add(ADDR, "u1001", -5, KEY),
        lambda s: s.add(ADDR, "u1001", 10**9, KEY),
        lambda s: s.add(ADDR, "u1001", "50; reboot", KEY),  # type: ignore[arg-type]
        lambda s: s.add(ADDR, "u1001", True, KEY),  # type: ignore[arg-type]
        lambda s: s.add(ADDR, "u1001", 50, "x; reboot"),
        lambda s: s.add(ADDR, "u1001", 50, KEY + " extra"),
        lambda s: s.add(ADDR, "u1001", 50, "ssh-ed25519 " + KEY),
        lambda s: s.add(ADDR, "u1001", 50, KEY + "\n"),
        lambda s: s.resize(ADDR, "u1001", 0),
        lambda s: s.logins(ADDR, "u1001", -1),
        lambda s: s.logins(ADDR, "u1001", "1; reboot"),  # type: ignore[arg-type]
    ],
)
async def test_bad_arguments_never_reach_ssh(ssh: SshStorectl, tmp_path: Path, call) -> None:
    with pytest.raises(StorectlError) as info:
        await call(ssh)
    assert info.value.kind is ErrorKind.BAD_REQUEST
    assert not info.value.retryable
    assert spawned(tmp_path) == []


@pytest.mark.parametrize(
    ("call", "kind", "retryable"),
    [
        (lambda s: s.resize(ADDR, "u1001", 50), ErrorKind.QUOTA_BELOW_USAGE, False),
        (lambda s: s.delete(ADDR, "u1001"), ErrorKind.UNREACHABLE, True),
        (lambda s: s.resume(ADDR, "u1001"), ErrorKind.INTERNAL, True),
        (lambda s: s.add(ADDR, "u1001", 50, KEY), ErrorKind.CONFLICT, False),
    ],
)
async def test_exit_codes_are_mapped(ssh: SshStorectl, call, kind, retryable) -> None:
    with pytest.raises(StorectlError) as info:
        await call(ssh)
    assert info.value.kind is kind
    assert info.value.retryable is retryable


async def test_error_detail_does_not_leak_the_key(ssh: SshStorectl) -> None:
    with pytest.raises(StorectlError) as info:
        await ssh.add(ADDR, "u1001", 50, KEY)
    assert KEY not in str(info.value)
    assert KEY not in info.value.detail


async def test_timeout_kills_the_process(ssh: SshStorectl) -> None:
    with pytest.raises(StorectlError) as info:
        await ssh.suspend(ADDR, "u1001")
    assert info.value.kind is ErrorKind.TIMEOUT
    assert info.value.retryable


async def test_logins_are_parsed(ssh: SshStorectl) -> None:
    events = await ssh.logins(ADDR, "u1001", 0)
    assert events == [
        LoginEvent(1760000100, False, HASH),
        LoginEvent(1760000200, True, "fedcba9876543210"),
    ]


async def test_missing_ssh_binary_is_an_internal_error(tmp_path: Path) -> None:
    ssh = SshStorectl(
        user="provision",
        key_path=tmp_path / "k",
        known_hosts_path=tmp_path / "kh",
        ssh_command=(str(tmp_path / "no-such-ssh"),),
    )
    with pytest.raises(StorectlError) as info:
        await ssh.usage(ADDR, "u1001")
    assert info.value.kind is ErrorKind.INTERNAL


def test_parse_usage() -> None:
    assert parse_usage("123\n456\n") == Usage(123, 456)
    assert parse_usage(" 0 \r\n 0 ") == Usage(0, 0)


@pytest.mark.parametrize("output", ["", "1", "1\n2\n3", "a\nb", "1\n-2", "1.5\n2", "1 2"])
def test_parse_usage_rejects_garbage(output: str) -> None:
    with pytest.raises(StorectlError) as info:
        parse_usage(output)
    assert info.value.kind is ErrorKind.BAD_OUTPUT


def test_parse_logins_sorts_and_accepts_empty() -> None:
    assert parse_logins("") == []
    events = parse_logins(f"1760000300 ok {HASH}\n1760000100 fail {HASH}\n")
    assert [e.ts for e in events] == [1760000100, 1760000300]
    assert [e.ok for e in events] == [False, True]


@pytest.mark.parametrize(
    "line",
    [
        "1760000100 ok 203.0.113.5",
        "1760000100 ok 2001:db8::1",
        "1760000100 maybe 0123456789abcdef",
        "1760000100 ok 0123456789abcde",
        "1760000100 ok 0123456789ABCDEF",
        "yesterday ok 0123456789abcdef",
        "1760000100 ok 0123456789abcdef extra",
        "1760000100  ok 0123456789abcdef",
    ],
)
def test_parse_logins_rejects_raw_ips_and_garbage(line: str) -> None:
    with pytest.raises(StorectlError) as info:
        parse_logins(line)
    assert info.value.kind is ErrorKind.BAD_OUTPUT
