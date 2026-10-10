"""#263: the typed environment readers in tools/common/env.py."""

import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from env import ConfigError, env_bool, env_int, env_str  # noqa: E402

NAME = "COMMON_TEST_VAR"


class EnvReaders(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(NAME, None)

    def set(self, value):
        os.environ[NAME] = value

    def test_str_unset_or_empty_gives_default(self):
        self.assertEqual(env_str(NAME, "fallback"), "fallback")
        self.assertEqual(env_str(NAME), "")
        self.set("")
        self.assertEqual(env_str(NAME, "fallback"), "fallback")

    def test_str_returns_value(self):
        self.set("http://example")
        self.assertEqual(env_str(NAME, "fallback"), "http://example")

    def test_int_parses_and_defaults(self):
        self.assertEqual(env_int(NAME, 9500), 9500)
        self.set("8090")
        self.assertEqual(env_int(NAME, 9500), 8090)
        self.set(" 42 ")
        self.assertEqual(env_int(NAME, 0), 42)

    def test_int_rejects_garbage_and_names_the_variable(self):
        self.set("ninety")
        with self.assertRaises(ConfigError) as ctx:
            env_int(NAME, 1)
        self.assertIn(NAME, str(ctx.exception))

    def test_config_error_is_a_value_error(self):
        self.assertTrue(issubclass(ConfigError, ValueError))

    def test_bool_unset_gives_default(self):
        self.assertFalse(env_bool(NAME))
        self.assertTrue(env_bool(NAME, True))

    def test_bool_true_values_case_insensitive(self):
        for value in ("1", "true", "TRUE", "Yes", "on", " On "):
            with self.subTest(value=value):
                self.set(value)
                self.assertTrue(env_bool(NAME))

    def test_bool_anything_else_is_false(self):
        for value in ("0", "false", "no", "off", "2", "enabled"):
            with self.subTest(value=value):
                self.set(value)
                self.assertFalse(env_bool(NAME, True))

    def test_bool_empty_gives_default(self):
        self.set("")
        self.assertTrue(env_bool(NAME, True))
        self.assertFalse(env_bool(NAME))


if __name__ == "__main__":
    unittest.main()
