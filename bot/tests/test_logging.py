import io
import logging

from plugbackup_bot.logging_setup import RedactingFormatter, redact, setup_logging

TOKEN = "123456789:" + "a" * 35
KEY = "AAAAC3NzaC1lZDI1NTE5AAAAI" + "b" * 43


def test_token_in_url_is_redacted() -> None:
    text = redact(f"POST https://api.telegram.org/bot{TOKEN}/getUpdates failed")
    assert TOKEN not in text
    assert "[bot-token]" in text


def test_key_body_is_redacted() -> None:
    assert KEY not in redact(f"key {KEY} accepted")


def test_full_public_key_line_is_redacted() -> None:
    text = redact(f"got ssh-ed25519 {KEY} laptop")
    assert KEY not in text
    assert "[ssh-key]" in text


def test_long_hex_is_redacted() -> None:
    secret = "ab12" * 16
    assert secret not in redact(f"hmac {secret}")


def test_key_value_secrets_are_redacted() -> None:
    assert "hunter2" not in redact("password=hunter2 user=u1001")
    assert "u1001" in redact("password=hunter2 user=u1001")


def test_plain_text_is_untouched() -> None:
    line = "storectl add u1001 ok in 0.4s"
    assert redact(line) == line


def test_traceback_is_redacted() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.getLogger("test_traceback_is_redacted")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        raise RuntimeError(f"bad key {KEY} for token {TOKEN}")
    except RuntimeError:
        logger.exception("failed")
    output = stream.getvalue()
    assert KEY not in output
    assert TOKEN not in output
    assert "RuntimeError" in output


def test_setup_logging_does_not_duplicate_handler() -> None:
    root = logging.getLogger()
    before = len(root.handlers)
    setup_logging("INFO")
    setup_logging("INFO")
    try:
        assert len(root.handlers) == before + 1
    finally:
        setup_logging("WARNING")
