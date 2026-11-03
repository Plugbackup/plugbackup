import base64

import pytest

from plugbackup_bot.domain.keys import (
    KEY_BODY_RE,
    InvalidKey,
    KeyProblem,
    fingerprint,
    parse_public_key,
)
from plugbackup_bot.domain.logins import make_login, parse_login

PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
BODY = base64.b64encode(PREFIX + bytes(range(32))).decode()
RSA_BODY = "AAAAB3NzaC1yc2EAAAADAQABAAABAQ" + "x" * 40


def problem(text: str) -> KeyProblem:
    with pytest.raises(InvalidKey) as info:
        parse_public_key(text)
    return info.value.problem


def test_bare_body_is_accepted() -> None:
    assert parse_public_key(BODY) == BODY


def test_full_line_with_comment_gives_body_only() -> None:
    assert parse_public_key(f"ssh-ed25519 {BODY} laptop@home") == BODY


def test_whitespace_around_is_ignored() -> None:
    assert parse_public_key(f"  ssh-ed25519   {BODY}\n") == BODY


def test_shell_in_comment_is_dropped() -> None:
    body = parse_public_key(f"ssh-ed25519 {BODY} $(reboot);rm -rf /")
    assert body == BODY
    assert KEY_BODY_RE.fullmatch(body)


@pytest.mark.parametrize(
    "text",
    [
        f"ssh-rsa {RSA_BODY} me",
        f"ecdsa-sha2-nistp256 {RSA_BODY}",
        f"sk-ssh-ed25519@openssh.com {BODY}",
        RSA_BODY,
    ],
)
def test_other_key_types_are_named(text: str) -> None:
    assert problem(text) is KeyProblem.NOT_ED25519


def test_private_key_is_recognised() -> None:
    # Заголовок собран из частей, чтобы в исходниках не было похожей на ключ строки.
    header = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
    footer = "-----END " + "OPENSSH PRIVATE KEY-----"
    assert problem(chr(10).join([header, "b3BlbnNzaC1rZXk=", footer])) is KeyProblem.PRIVATE_KEY


def test_empty_and_too_long() -> None:
    assert problem("  \n") is KeyProblem.EMPTY
    assert problem("A" * 601) is KeyProblem.TOO_LONG


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        f"{BODY}\n{BODY}",
        f"{BODY};rm -rf /",
        f"{BODY}`id`",
        f"{BODY} && reboot",
        BODY[:-4],
        BODY + "AAAA",
        BODY.replace("AAAAIAABAg", "AAAAIQABAg", 1),
        "AAAAC3NzaC1lZDI1NTE5",
        "AAAAC3NzaC1lZDI1NTE5" + "=" * 60,
        BODY + "\x00",
        BODY + "ё",
    ],
)
def test_malformed_keys_are_rejected(text: str) -> None:
    assert problem(text) is KeyProblem.BAD_FORMAT


def test_trailing_newline_cannot_slip_past_the_regex() -> None:
    assert KEY_BODY_RE.fullmatch(BODY + "\n") is None


def test_fingerprint_matches_ssh_keygen() -> None:
    # Значение получено командой `ssh-keygen -lf` для этого же ключа.
    assert fingerprint(BODY) == "SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA"


def test_login_roundtrip() -> None:
    assert make_login(1) == "u1001"
    assert parse_login("u1001") == 1
    assert all(parse_login(make_login(i)) == i for i in (1, 42, 8999, 123_456_789))


@pytest.mark.parametrize(
    "login",
    ["", "u", "u1000", "u999", "U1001", "u1001\n", " u1001", "u١٠٠١", "u10010000000", "u1001;x"],
)
def test_bad_logins_are_rejected(login: str) -> None:
    assert parse_login(login) is None


def test_make_login_rejects_zero() -> None:
    with pytest.raises(ValueError):
        make_login(0)
