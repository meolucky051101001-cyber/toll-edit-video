"""Mock-only startup diagnostics regressions; production entrypoints are AST-extracted, never imported."""
import ast
import errno
import io
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

ROOT = Path(__file__).resolve().parents[1]


def extract_definitions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    found = {node.name for node in selected}
    if found != set(names):
        raise AssertionError(f"Missing definitions in {path}: {set(names) - found}")
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def bot_helpers():
    namespace = {
        "errno": errno,
        "sys": sys,
        "logging": logging,
        "RedactingFormatter": logging.Formatter,
    }
    return extract_definitions(
        ROOT / "backend" / "telegram_bot.py",
        {
            "BotInstanceAlreadyRunning",
            "BotStartupError",
            "_startup_failure_message",
            "_acquire_bot_instance_lock",
        },
        namespace,
    )


def service_helpers():
    namespace = {
        "logging": logging,
        "sys": sys,
        "Path": Path,
        "RedactingFormatter": logging.Formatter,
        "RotatingFileHandler": Mock,
    }
    return extract_definitions(
        ROOT / "backend" / "background_service.py",
        {
            "_startup_failure_message",
            "_install_stderr_fallback",
            "_configure_service_logging",
        },
        namespace,
    )


class BotLockStartupTests(unittest.TestCase):
    def setUp(self):
        self.helpers = bot_helpers()

    def test_open_permission_error_is_fatal_and_diagnostic_omits_exception_text(self):
        lock_path = r"D:\tool\bot_system\bot_instance.lock"
        secret = "private-token-value"
        opener = Mock(side_effect=PermissionError(errno.EACCES, f"denied {secret}"))
        lock_acquirer = Mock()

        with self.assertRaises(self.helpers["BotStartupError"]) as raised:
            self.helpers["_acquire_bot_instance_lock"](
                lock_path, opener=opener, lock_acquirer=lock_acquirer
            )

        message = str(raised.exception)
        self.assertIn("operation=open instance lock", message)
        self.assertIn(lock_path, message)
        self.assertIn("error_type=PermissionError", message)
        self.assertNotIn(secret, message)
        lock_acquirer.assert_not_called()

    def test_only_lock_contention_preserves_clean_duplicate_behavior(self):
        lock_file = Mock()
        lock_acquirer = Mock(side_effect=PermissionError(errno.EACCES, "lock is held"))

        with self.assertRaises(self.helpers["BotInstanceAlreadyRunning"]) as raised:
            self.helpers["_acquire_bot_instance_lock"](
                "instance.lock", opener=Mock(return_value=lock_file), lock_acquirer=lock_acquirer
            )

        self.assertIn("operation=acquire instance lock", str(raised.exception))
        lock_file.close.assert_called_once_with()

    def test_non_contention_lock_error_is_fatal(self):
        lock_file = Mock()
        lock_acquirer = Mock(side_effect=OSError(errno.EIO, "private detail"))

        with self.assertRaises(self.helpers["BotStartupError"]) as raised:
            self.helpers["_acquire_bot_instance_lock"](
                "instance.lock", opener=Mock(return_value=lock_file), lock_acquirer=lock_acquirer
            )

        self.assertIn("error_type=OSError", str(raised.exception))
        self.assertNotIn("private detail", str(raised.exception))
        lock_file.close.assert_called_once_with()

    def test_successful_lock_is_returned_without_closing(self):
        lock_file = Mock()
        result = self.helpers["_acquire_bot_instance_lock"](
            "instance.lock", opener=Mock(return_value=lock_file), lock_acquirer=Mock()
        )
        self.assertIs(result, lock_file)
        lock_file.close.assert_not_called()

    def test_fatal_polling_exception_exits_instead_of_restarting(self):
        tree = ast.parse((ROOT / "backend" / "telegram_bot.py").read_text(encoding="utf-8"))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        retry_loop = next(node for node in ast.walk(main) if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True)
        fatal_handler = next(
            node for node in ast.walk(retry_loop)
            if isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "Exception"
        )
        self.assertTrue(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SystemExit"
            for node in ast.walk(fatal_handler)
        ))
        self.assertFalse(any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sleep"
            for node in ast.walk(fatal_handler)
        ))


class ServiceLogStartupTests(unittest.TestCase):
    def _test_logger(self):
        name = f"startup-regression-{id(self)}"
        logger = logging.getLogger(name)
        previous_handlers = list(logger.handlers)
        previous_level = logger.level
        logger.handlers.clear()
        return logger, previous_handlers, previous_level

    def _restore_logger(self, logger, previous_handlers, previous_level):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        for handler in previous_handlers:
            logger.addHandler(handler)
        logger.setLevel(previous_level)

    def test_mkdir_permission_error_uses_stderr_and_exits_nonzero_without_io(self):
        helpers = service_helpers()
        logger, old_handlers, old_level = self._test_logger()
        stream = io.StringIO()
        logs = MagicMock()
        logs.__truediv__.return_value = Path(r"D:\restricted\service_logs\telegram_supervisor.log")
        secret = "private-token-value"
        logs.mkdir.side_effect = PermissionError(errno.EACCES, f"denied {secret}")
        try:
            with self.assertRaises(SystemExit) as raised:
                helpers["_configure_service_logging"](
                    logger.name, logs, file_handler_factory=Mock(), stream=stream
                )
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("operation=configure supervisor log", stream.getvalue())
            self.assertIn("error_type=PermissionError", stream.getvalue())
            self.assertIn("telegram_supervisor.log", stream.getvalue())
            self.assertNotIn(secret, stream.getvalue())
        finally:
            self._restore_logger(logger, old_handlers, old_level)

    def test_handler_permission_error_uses_stderr_and_exits_nonzero_without_io(self):
        helpers = service_helpers()
        logger, old_handlers, old_level = self._test_logger()
        stream = io.StringIO()
        logs = MagicMock()
        logs.__truediv__.return_value = Path(r"D:\restricted\service_logs\telegram_supervisor.log")
        factory = Mock(side_effect=PermissionError(errno.EACCES, "cannot open"))
        try:
            with self.assertRaises(SystemExit) as raised:
                helpers["_configure_service_logging"](
                    logger.name, logs, file_handler_factory=factory, stream=stream
                )
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("error_type=PermissionError", stream.getvalue())
            self.assertIn("telegram_supervisor.log", stream.getvalue())
            factory.assert_called_once()
        finally:
            self._restore_logger(logger, old_handlers, old_level)

    def test_successful_log_setup_removes_fallback_and_keeps_file_handler(self):
        helpers = service_helpers()
        logger, old_handlers, old_level = self._test_logger()
        stream = io.StringIO()
        logs = MagicMock()
        logs.__truediv__.return_value = Path("unused-supervisor.log")
        file_handler = logging.NullHandler()
        try:
            result = helpers["_configure_service_logging"](
                logger.name,
                logs,
                file_handler_factory=Mock(return_value=file_handler),
                stream=stream,
            )
            self.assertIs(result, logger)
            self.assertIn(file_handler, logger.handlers)
            self.assertFalse(any(getattr(item, "_autodub_startup_stderr", False) for item in logger.handlers))
            logs.mkdir.assert_called_once_with(parents=True, exist_ok=True)
        finally:
            self._restore_logger(logger, old_handlers, old_level)


if __name__ == "__main__":
    unittest.main()
