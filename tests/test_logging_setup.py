import logging
import sys

import pytest

from core.logging_setup import REDACTED, RedactingFormatter, redact_secrets, setup_logging

FAKE_TG_TOKEN = "123456789:" + "A" * 35
FAKE_GOOGLE_KEY = "AIza" + "b" * 35
FAKE_OPENAI_KEY = "sk-" + "c" * 32


def test_redacts_telegram_token_inside_api_url():
    text = f"GET https://api.telegram.org/bot{FAKE_TG_TOKEN}/getUpdates"
    out = redact_secrets(text)
    assert FAKE_TG_TOKEN not in out
    assert "api.telegram.org/bot<redacted>/getUpdates" in out


def test_redacts_google_and_openai_keys():
    out = redact_secrets(f"key={FAKE_GOOGLE_KEY} other={FAKE_OPENAI_KEY}")
    assert FAKE_GOOGLE_KEY not in out
    assert FAKE_OPENAI_KEY not in out


def test_redacts_bearer_and_assignment_secrets():
    out = redact_secrets(
        "Authorization: Bearer abcdef1234567890 token=plainvalue password: hunter2 "
        "X-Local-Control-Token: local-secret-value"
    )
    assert "abcdef1234567890" not in out
    assert "plainvalue" not in out
    assert "hunter2" not in out
    assert "local-secret-value" not in out
    assert REDACTED in out


def test_redacts_json_quoted_value():
    out = redact_secrets('{"token": "json-secret-value"}')
    assert "json-secret-value" not in out
    assert '"token": "<redacted>"' in out


def test_leaves_normal_text_untouched():
    text = "Đã xử lý 8/8 stage trong 179 giây, không có lỗi."
    assert redact_secrets(text) == text


def test_formatter_redacts_exception_traceback():
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, "lỗi khi gọi %s", ("api",), None)
    try:
        raise ValueError(f"upstream trả về token {FAKE_TG_TOKEN}")
    except ValueError:
        record.exc_info = sys.exc_info()
    out = RedactingFormatter("%(message)s").format(record)
    assert FAKE_TG_TOKEN not in out
    assert "ValueError" in out


def test_formatter_redacts_lazy_args():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "call %s", (FAKE_TG_TOKEN,), None)
    out = RedactingFormatter("%(message)s").format(record)
    assert FAKE_TG_TOKEN not in out


@pytest.fixture
def cleanup_logging():
    root = logging.getLogger()
    saved_level = root.level
    saved_noisy = {name: logging.getLogger(name).level for name in ("httpx", "httpcore", "urllib3")}
    yield
    for handler in list(root.handlers):
        if getattr(handler, "_autodub_service", None) == "test_logging_service":
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved_level)
    for name, level in saved_noisy.items():
        logging.getLogger(name).setLevel(level)


def test_setup_logging_writes_redacted_rotating_file(tmp_path, cleanup_logging):
    log_path = tmp_path / "nested" / "service.log"
    setup_logging("test_logging_service", log_path, console=False)
    logging.getLogger("test_logging_service").info("token=%s", FAKE_TG_TOKEN)

    content = log_path.read_text(encoding="utf-8")
    assert FAKE_TG_TOKEN not in content
    assert REDACTED in content
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_logging_is_idempotent(tmp_path, cleanup_logging):
    log_path = tmp_path / "service.log"
    setup_logging("test_logging_service", log_path, console=False)
    setup_logging("test_logging_service", log_path, console=False)
    marked = [h for h in logging.getLogger().handlers if getattr(h, "_autodub_service", None) == "test_logging_service"]
    assert len(marked) == 1
