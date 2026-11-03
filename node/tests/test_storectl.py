"""Тесты node/storectl. zfs, useradd и journalctl подменены, на ноде ничего не нужно."""

import base64
import json
from pathlib import Path
from types import ModuleType

import pytest

GIB = 1024**3
PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
KEY = base64.b64encode(PREFIX + bytes(range(32))).decode()
KEY2 = base64.b64encode(PREFIX + bytes(range(1, 33))).decode()
SECRET = b"s" * 40
NOW = 1_793_620_800


class FakeRunner:
    """Играет роль zfs, getent, useradd, userdel, install, pkill и journalctl."""

    def __init__(self, sc: ModuleType) -> None:
        self.sc = sc
        self.calls: list[list[str]] = []
        self.datasets: dict[str, dict[str, int]] = {}
        self.users: set[str] = set()
        self.journal = ""
        self.zfs_list_error: str | None = None

    def __call__(self, args):  # type: ignore[no-untyped-def]
        args = list(args)
        assert all(isinstance(part, str) for part in args)
        self.calls.append(args)
        result = self.sc.Result
        tool, rest = args[0], args[1:]
        if tool == "getent":
            return result(0 if rest[1] in self.users else 2)
        if tool == "useradd":
            self.users.add(rest[-1])
            return result(0)
        if tool == "userdel":
            self.users.discard(rest[0])
            return result(0)
        if tool in ("install", "pkill"):
            return result(0 if tool == "install" else 1)
        if tool == "journalctl":
            return result(0, self.journal)
        assert tool == "zfs", args
        return self._zfs(rest)

    def _zfs(self, args: list[str]):  # type: ignore[no-untyped-def]
        result = self.sc.Result
        verb, name = args[0], args[-1]
        if verb == "list":
            if self.zfs_list_error is not None:
                return result(1, "", self.zfs_list_error)
            if name in self.datasets:
                return result(0, name + "\n")
            return result(1, "", f"cannot open '{name}': dataset does not exist")
        if verb == "create":
            quota = next(a for a in args if a.startswith("refquota="))
            self.datasets[name] = {"referenced": 0, "refquota": int(quota[9:-1]) * GIB}
            return result(0)
        if verb == "set":
            self.datasets[name]["refquota"] = int(args[1][9:-1]) * GIB
            return result(0)
        if verb == "get":
            props = args[args.index("-o") + 2].split(",")
            return result(0, "".join(f"{self.datasets[name][p]}\n" for p in props))
        if verb == "destroy":
            del self.datasets[name]
            return result(0)
        raise AssertionError(args)

    def verbs(self, tool: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == tool]


@pytest.fixture
def env(sc: ModuleType, tmp_path: Path):  # type: ignore[no-untyped-def]
    config = sc.Config(
        keys_dir=tmp_path / "keys",
        suspended_dir=tmp_path / "suspended",
        hmac_key_file=tmp_path / "hmac.key",
        lock_file=tmp_path / "lock",
    )
    config.hmac_key_file.write_bytes(SECRET + b"\n")
    runner = FakeRunner(sc)
    store = sc.Store(config, runner, now=lambda: NOW)
    return store, runner, config


def run(sc: ModuleType, store, *argv: str, capsys=None) -> int:  # type: ignore[no-untyped-def]
    return sc.main(list(argv), {}, store)


# --- проверка аргументов ---


BAD_ARGS = [
    [],
    ["bogus"],
    ["add"],
    ["add", "u1001", "50G"],
    ["add", "u1001", "50G", KEY, "extra"],
    ["usage"],
    ["usage", "u1001", "extra"],
    ["usage", "u1001;reboot"],
    ["usage", "u1001 && id"],
    ["usage", "$(id)"],
    ["usage", "u1001\n"],
    ["usage", "../u1001"],
    ["usage", "root"],
    ["usage", "u100"],
    ["usage", "u1234567890"],
    ["usage", "U1001"],
    ["usage", "u١٠٠١"],
    ["resize", "u1001", "50"],
    ["resize", "u1001", "50g"],
    ["resize", "u1001", "0G"],
    ["resize", "u1001", "100001G"],
    ["resize", "u1001", "-5G"],
    ["resize", "u1001", "50G;reboot"],
    ["resize", "u1001", "050G0"],
    ["add", "u1001", "50G", "x; reboot"],
    ["add", "u1001", "50G", "ssh-ed25519 " + KEY],
    ["add", "u1001", "50G", KEY[:-4]],
    ["add", "u1001", "50G", KEY + "AAAA"],
    ["add", "u1001", "50G", "AAAAB3NzaC1yc2EAAAADAQABAAABAQ" + "x" * 40],
    ["add", "u1001", "50G", KEY + "\n"],
    ["logins", "u1001"],
    ["logins", "u1001", "-1"],
    ["logins", "u1001", "abc"],
    ["logins", "u1001", "1; reboot"],
    ["logins", "u1001", "1" * 13],
    ["--ssh", "usage", "u1001"],
]


@pytest.mark.parametrize("argv", BAD_ARGS)
def test_bad_arguments_are_refused_before_anything_runs(sc, env, argv) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    assert sc.main(argv, {}, store) == sc.USAGE
    assert runner.calls == []


def test_ssh_mode_reads_the_original_command(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    runner.datasets["tank/clients/u1001"] = {"referenced": 5, "refquota": 7}
    runner.users.add("u1001")
    assert sc.main(["--ssh"], {"SSH_ORIGINAL_COMMAND": "usage u1001"}, store) == sc.OK


@pytest.mark.parametrize(
    "command",
    ["", "   ", "usage", "usage u1001\nreboot", "usage u1001 u1002", "reboot", "usage u1001;id"],
)
def test_ssh_mode_applies_the_same_checks(sc, env, command) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    assert sc.main(["--ssh"], {"SSH_ORIGINAL_COMMAND": command}, store) == sc.USAGE
    assert runner.calls == []


def test_ssh_mode_without_the_variable(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    assert sc.main(["--ssh"], {}, store) == sc.USAGE
    assert runner.calls == []


# --- add ---


def test_add_creates_everything(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    assert sc.main(["add", "u1001", "50G", KEY], {}, store) == sc.OK

    [useradd] = runner.verbs("useradd")
    assert useradd[-1] == "u1001"
    assert useradd[useradd.index("--uid") + 1] == "31001"
    assert useradd[useradd.index("--shell") + 1] == "/usr/sbin/nologin"
    assert runner.datasets["tank/clients/u1001"] == {"referenced": 0, "refquota": 50 * GIB}
    [create] = [c for c in runner.verbs("zfs") if c[1] == "create"]
    assert "mountpoint=/srv/plugbackup/u1001" in create
    [install] = runner.verbs("install")
    assert install == ["install", "-d", "-o", "31001", "-g", "30000", "-m", "0700",
                       "/srv/plugbackup/u1001/data"]  # fmt: skip
    assert (config.keys_dir / "u1001").read_text() == f"restrict ssh-ed25519 {KEY}\n"


def test_add_is_idempotent_and_updates_key_and_quota(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    assert sc.main(["add", "u1001", "50G", KEY], {}, store) == sc.OK
    assert len(runner.verbs("useradd")) == 1
    assert len([c for c in runner.verbs("zfs") if c[1] == "create"]) == 1

    assert sc.main(["add", "u1001", "100G", KEY2], {}, store) == sc.OK
    assert runner.datasets["tank/clients/u1001"]["refquota"] == 100 * GIB
    assert (config.keys_dir / "u1001").read_text() == f"restrict ssh-ed25519 {KEY2}\n"


def test_add_refuses_a_quota_below_usage_and_keeps_the_old_key(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    runner.datasets["tank/clients/u1001"]["referenced"] = 40 * GIB
    assert sc.main(["add", "u1001", "10G", KEY2], {}, store) == sc.QUOTA_BELOW_USAGE
    assert (config.keys_dir / "u1001").read_text() == f"restrict ssh-ed25519 {KEY}\n"
    assert runner.datasets["tank/clients/u1001"]["refquota"] == 50 * GIB
    assert "storectl:" in capsys.readouterr().err


def test_add_to_a_suspended_client_keeps_it_suspended(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, _, config = env
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    sc.main(["suspend", "u1001"], {}, store)
    sc.main(["add", "u1001", "50G", KEY2], {}, store)
    assert not (config.keys_dir / "u1001").exists()
    assert (config.suspended_dir / "u1001").read_text() == f"restrict ssh-ed25519 {KEY2}\n"


def test_the_key_file_never_holds_more_than_the_key(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, _, config = env
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    lines = (config.keys_dir / "u1001").read_text().splitlines()
    assert lines == [f"restrict ssh-ed25519 {KEY}"]
    assert not list(config.keys_dir.glob(".*"))  # временных файлов не осталось


# --- resize ---


def test_resize(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    assert sc.main(["resize", "u1001", "10G"], {}, store) == sc.NOT_FOUND
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    runner.datasets["tank/clients/u1001"]["referenced"] = 20 * GIB
    assert sc.main(["resize", "u1001", "100G"], {}, store) == sc.OK
    assert runner.datasets["tank/clients/u1001"]["refquota"] == 100 * GIB
    assert sc.main(["resize", "u1001", "10G"], {}, store) == sc.QUOTA_BELOW_USAGE
    assert runner.datasets["tank/clients/u1001"]["refquota"] == 100 * GIB
    assert sc.main(["resize", "u1001", "20G"], {}, store) == sc.OK  # ровно по занятому


# --- suspend, resume ---


def test_suspend_and_resume(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    assert sc.main(["suspend", "u1001"], {}, store) == sc.NOT_FOUND
    sc.main(["add", "u1001", "50G", KEY], {}, store)

    assert sc.main(["suspend", "u1001"], {}, store) == sc.OK
    assert not (config.keys_dir / "u1001").exists()
    assert (config.suspended_dir / "u1001").exists()
    assert ["pkill", "-KILL", "-u", "u1001"] in runner.calls
    assert sc.main(["suspend", "u1001"], {}, store) == sc.OK  # повтор безопасен

    assert sc.main(["resume", "u1001"], {}, store) == sc.OK
    assert (config.keys_dir / "u1001").read_text() == f"restrict ssh-ed25519 {KEY}\n"
    assert not (config.suspended_dir / "u1001").exists()
    assert sc.main(["resume", "u1001"], {}, store) == sc.OK


def test_resume_without_any_key(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    runner.datasets["tank/clients/u1001"] = {"referenced": 0, "refquota": GIB}
    assert sc.main(["resume", "u1001"], {}, store) == sc.NOT_FOUND
    assert sc.main(["suspend", "u1001"], {}, store) == sc.NOT_FOUND


# --- delete ---


def test_delete_removes_everything(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    assert sc.main(["delete", "u1001"], {}, store) == sc.OK
    assert runner.datasets == {}
    assert runner.users == set()
    assert not (config.keys_dir / "u1001").exists()
    assert ["zfs", "destroy", "-r", "tank/clients/u1001"] in runner.calls
    assert ["pkill", "-KILL", "-u", "u1001"] in runner.calls


def test_delete_of_a_suspended_client(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    sc.main(["suspend", "u1001"], {}, store)
    assert sc.main(["delete", "u1001"], {}, store) == sc.OK
    assert not (config.suspended_dir / "u1001").exists()


def test_delete_of_a_missing_client_is_a_success(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    assert sc.main(["delete", "u1001"], {}, store) == sc.OK
    assert [c for c in runner.verbs("zfs") if c[1] == "destroy"] == []
    assert runner.verbs("pkill") == []  # пользователя нет — убивать некого


def test_a_zfs_hiccup_is_never_mistaken_for_a_missing_client(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    runner.zfs_list_error = "internal error: pool I/O is currently suspended"
    assert sc.main(["delete", "u1001"], {}, store) == sc.INTERNAL
    assert "tank/clients/u1001" in runner.datasets  # данные на месте
    assert sc.main(["usage", "u1001"], {}, store) == sc.INTERNAL


# --- usage ---


def test_usage(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    assert sc.main(["usage", "u1001"], {}, store) == sc.NOT_FOUND
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    runner.datasets["tank/clients/u1001"]["referenced"] = 1234
    capsys.readouterr()
    assert sc.main(["usage", "u1001"], {}, store) == sc.OK
    assert capsys.readouterr().out == f"1234\n{50 * GIB}\n"


# --- logins ---


def jl(ts: int, message: object) -> str:
    return json.dumps({"__REALTIME_TIMESTAMP": str(ts * 1_000_000), "MESSAGE": message})


JOURNAL = "\n".join(
    [
        jl(NOW - 500, "Accepted publickey for u1001 from 203.0.113.5 port 51234 ssh2: ED25519 SHA256:x"),
        jl(NOW - 400, "Failed publickey for u1001 from 203.0.113.6 port 1 ssh2: ED25519 SHA256:x"),
        jl(NOW - 390, "Failed password for invalid user u1001 from 2001:db8::1 port 22 ssh2"),
        jl(NOW - 380, "Invalid user u1001 from 203.0.113.7 port 2222"),
        jl(NOW - 370, "Connection closed by authenticating user u1001 203.0.113.8 port 4444 [preauth]"),
        jl(NOW - 360, "Disconnected from authenticating user u1001 203.0.113.8 port 4445 [preauth]"),
        jl(NOW - 350, "Accepted publickey for u1002 from 203.0.113.5 port 1 ssh2"),
        jl(NOW - 340, "pam_unix(sshd:session): session opened for user u1001(uid=31001)"),
        jl(NOW - 330, "Accepted publickey for u1001 from not-an-address port 1 ssh2"),
        jl(NOW - 320, [1, 2, 3]),
        "this is not json",
        jl(NOW - 100000, "Accepted publickey for u1001 from 203.0.113.99 port 1 ssh2"),
    ]
)


def get_logins(sc, store, runner, since: int = NOW - 3600, journal: str = JOURNAL, login="u1001"):  # type: ignore[no-untyped-def]
    runner.journal = journal
    return sc.parse_command(["logins", login, str(since)], {}), store


def logins_output(sc, store, runner, capsys, since: int = NOW - 3600, login: str = "u1001") -> list[str]:  # type: ignore[no-untyped-def]
    runner.journal = JOURNAL
    capsys.readouterr()
    assert sc.main(["logins", login, str(since)], {}, store) == sc.OK
    return capsys.readouterr().out.splitlines()


def test_logins_output_has_only_time_result_and_hash(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    lines = logins_output(sc, store, runner, capsys)
    assert len(lines) == 6
    for line in lines:
        ts, result, digest = line.split(" ")
        assert ts.isdigit() and result in ("ok", "fail")
        assert len(digest) == 16 and set(digest) <= set("0123456789abcdef")
    assert [line.split(" ")[1] for line in lines] == ["ok", "fail", "fail", "fail", "fail", "fail"]
    assert [int(line.split(" ")[0]) for line in lines] == sorted(
        int(line.split(" ")[0]) for line in lines
    )


def test_logins_never_leak_addresses(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    text = "\n".join(logins_output(sc, store, runner, capsys))
    for fragment in ("203.0.113", "2001:db8", "port", "u1001", "SHA256"):
        assert fragment not in text


def test_same_address_same_hash_and_repeated_attempts_stay_comparable(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    lines = logins_output(sc, store, runner, capsys)
    hashes = [line.split(" ")[2] for line in lines]
    # Два входа с 203.0.113.8 дают один хеш, остальные адреса разные.
    assert hashes[4] == hashes[5]
    assert len(set(hashes)) == 5


def test_hashes_are_bound_to_the_login_and_the_secret(sc, env, tmp_path) -> None:  # type: ignore[no-untyped-def]
    one = sc.ip_hash(SECRET, "u1001", "203.0.113.5")
    assert one == sc.ip_hash(SECRET, "u1001", "203.0.113.5")
    assert one != sc.ip_hash(SECRET, "u1002", "203.0.113.5")
    assert one != sc.ip_hash(b"t" * 40, "u1001", "203.0.113.5")
    assert one != sc.ip_hash(SECRET, "u1001", "203.0.113.6")
    mapped = sc.ip_hash(SECRET, "u1001", "::ffff:203.0.113.5")
    assert mapped == one  # IPv4 в записи IPv6 даёт тот же хеш
    assert sc.ip_hash(SECRET, "u1001", "nonsense") is None


def test_only_the_requested_login_and_period(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    assert len(logins_output(sc, store, runner, capsys, login="u1002")) == 1
    assert len(logins_output(sc, store, runner, capsys, login="u1003")) == 0
    late = logins_output(sc, store, runner, capsys, since=NOW - 395)
    assert len(late) == 4  # события старше since отброшены


def test_journal_is_queried_for_sshd_only(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    logins_output(sc, store, runner, capsys, since=NOW - 3600)
    [call] = runner.verbs("journalctl")
    assert call == [
        "journalctl", "--no-pager", "-o", "json", "--since", f"@{NOW - 3600}",
        "SYSLOG_IDENTIFIER=sshd", "SYSLOG_IDENTIFIER=sshd-session",
    ]  # fmt: skip


def test_lookback_is_bounded(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    logins_output(sc, store, runner, capsys, since=0)
    [call] = runner.verbs("journalctl")
    assert call[call.index("--since") + 1] == f"@{NOW - 31 * 24 * 3600}"


def test_the_event_count_is_capped_to_the_newest(sc, env, capsys, tmp_path) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    capped = sc.Store(
        sc.Config(**{**config.__dict__, "login_max_events": 2}), runner, now=lambda: NOW
    )
    lines = logins_output(sc, capped, runner, capsys)
    assert len(lines) == 2
    assert lines[-1].startswith(str(NOW - 360))


def test_missing_or_weak_secret_is_an_internal_error(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    store, runner, config = env
    config.hmac_key_file.write_bytes(b"short")
    assert sc.main(["logins", "u1001", str(NOW - 60)], {}, store) == sc.INTERNAL
    config.hmac_key_file.unlink()
    assert sc.main(["logins", "u1001", str(NOW - 60)], {}, store) == sc.INTERNAL
    assert runner.verbs("journalctl") == []  # без секрета журнал даже не читаем
    assert "секрет" in capsys.readouterr().err


# --- main и конфигурация ---


def test_unexpected_errors_become_exit_code_6_without_a_traceback(sc, env, capsys) -> None:  # type: ignore[no-untyped-def]
    class Broken:
        def usage(self, login: str) -> str:
            raise RuntimeError("secret detail")

    assert sc.main(["usage", "u1001"], {}, Broken()) == sc.INTERNAL
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "secret detail" not in err
    assert "Traceback" not in err


def test_missing_commands_are_reported(sc, tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    store = sc.Store(sc.Config(lock_file=tmp_path / "lock"), sc.system_runner)
    assert sc.main(["usage", "u1001"], {}, store) == sc.INTERNAL
    assert "storectl:" in capsys.readouterr().err


def test_config_file(sc, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "storectl.conf"
    path.write_text(
        "# комментарий\nZFS_PARENT=pool/backups\nUID_BASE=50000\nCLIENT_GID = 50000\n"
        'MOUNT_BASE="/mnt/clients"\n',
        encoding="utf-8",
    )
    config = sc.load_config(path)
    assert (config.zfs_parent, config.uid_base, config.client_gid) == ("pool/backups", 50000, 50000)
    assert config.mount_base == "/mnt/clients"
    assert sc.load_config(tmp_path / "missing.conf") == sc.Config()


@pytest.mark.parametrize("parent", ["pool; reboot", "pool/ x", "", "pool//a", "-o"])
def test_a_hostile_dataset_parent_is_refused(sc, tmp_path, parent) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "storectl.conf"
    path.write_text(f"ZFS_PARENT={parent}\n", encoding="utf-8")
    with pytest.raises(sc.StoreError):
        sc.load_config(path)


@pytest.mark.parametrize("base", ["relative/path", "/srv/a b", "/srv;reboot", "", "-o", "/"])
def test_a_hostile_mount_base_is_refused(sc, tmp_path, base) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "storectl.conf"
    path.write_text(f"MOUNT_BASE={base}" + chr(10), encoding="utf-8")
    with pytest.raises(sc.StoreError):
        sc.load_config(path)


def test_a_broken_number_in_the_config(sc, tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "storectl.conf"
    path.write_text("UID_BASE=abc\n", encoding="utf-8")
    with pytest.raises(sc.StoreError):
        sc.load_config(path)


def test_dataset_names_come_only_from_the_validated_login(sc, env) -> None:  # type: ignore[no-untyped-def]
    store, runner, _ = env
    sc.main(["add", "u1001", "50G", KEY], {}, store)
    sc.main(["delete", "u1001"], {}, store)
    for call in runner.calls:
        for part in call:
            assert ";" not in part and " " not in part and "\n" not in part
