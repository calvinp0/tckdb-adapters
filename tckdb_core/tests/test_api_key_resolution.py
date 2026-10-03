"""API-key resolution: env var vs configured local files (``tckdb_core.config``).

Moved from the ARC adapter's ``test_config.py``; the resolution does not depend
on which producer parsed the config.
"""

import os
import tempfile
import unittest
from pathlib import Path
import unittest.mock  # noqa: F401  (the moved tests call unittest.mock.patch.dict)

from tckdb_core.config import (
    InputError,
    _read_tckdb_api_key_from_env_file,
    resolve_tckdb_api_key,
)


class TestResolveTckdbApiKey(unittest.TestCase):
    """Resolution of the API key from env vs configured local files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _write(self, name: str, content: str) -> Path:
        p = self.tmp / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_env_var_wins_over_api_key_file(self):
        path = self._write("key.txt", "from_file_key")
        with unittest.mock.patch.dict(os.environ, {"X_T_KEY": "from_env_key"}, clear=False):
            got = resolve_tckdb_api_key(
                api_key_env="X_T_KEY",
                api_key_file=str(path),
            )
        self.assertEqual(got, "from_env_key")

    def test_api_key_file_returns_raw_key(self):
        path = self._write("key.txt", "tck_abcdef")
        os.environ.pop("X_T_KEY", None)
        got = resolve_tckdb_api_key(api_key_env="X_T_KEY", api_key_file=str(path))
        self.assertEqual(got, "tck_abcdef")

    def test_api_key_file_strips_trailing_newline(self):
        path = self._write("key.txt", "tck_abcdef\n")
        os.environ.pop("X_T_KEY", None)
        got = resolve_tckdb_api_key(api_key_env="X_T_KEY", api_key_file=str(path))
        self.assertEqual(got, "tck_abcdef")

    def test_api_key_file_strips_surrounding_whitespace(self):
        path = self._write("key.txt", "   tck_abcdef \n\n")
        os.environ.pop("X_T_KEY", None)
        got = resolve_tckdb_api_key(api_key_env="X_T_KEY", api_key_file=str(path))
        self.assertEqual(got, "tck_abcdef")

    def test_missing_api_key_file_raises(self):
        os.environ.pop("X_T_KEY", None)
        with self.assertRaises(InputError) as ctx:
            resolve_tckdb_api_key(
                api_key_env="X_T_KEY",
                api_key_file=str(self.tmp / "does_not_exist"),
            )
        self.assertIn("does not exist", str(ctx.exception))

    def test_empty_api_key_file_raises(self):
        path = self._write("key.txt", "   \n\n")
        os.environ.pop("X_T_KEY", None)
        with self.assertRaises(InputError) as ctx:
            resolve_tckdb_api_key(
                api_key_env="X_T_KEY",
                api_key_file=str(path),
            )
        self.assertIn("empty", str(ctx.exception))

    def test_no_sources_returns_none(self):
        os.environ.pop("X_T_KEY", None)
        self.assertIsNone(resolve_tckdb_api_key(api_key_env="X_T_KEY"))

    def test_user_home_expansion(self):
        # Paths starting with ~ should be expanded.
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.tmp)}, clear=False):
            self._write("key.txt", "tck_home")
            os.environ.pop("X_T_KEY", None)
            got = resolve_tckdb_api_key(
                api_key_env="X_T_KEY",
                api_key_file="~/key.txt",
            )
        self.assertEqual(got, "tck_home")

    # api_key_env_file paths -------------------------------------------------

    def test_env_file_supports_unquoted(self):
        path = self._write("auth.env", "TCKDB_API_KEY=tck_unq\n")
        os.environ.pop("TCKDB_API_KEY", None)
        got = resolve_tckdb_api_key(api_key_env_file=str(path))
        self.assertEqual(got, "tck_unq")

    def test_env_file_supports_single_quoted(self):
        path = self._write("auth.env", "TCKDB_API_KEY='tck_sq'\n")
        os.environ.pop("TCKDB_API_KEY", None)
        got = resolve_tckdb_api_key(api_key_env_file=str(path))
        self.assertEqual(got, "tck_sq")

    def test_env_file_supports_double_quoted(self):
        path = self._write("auth.env", 'TCKDB_API_KEY="tck_dq"\n')
        os.environ.pop("TCKDB_API_KEY", None)
        got = resolve_tckdb_api_key(api_key_env_file=str(path))
        self.assertEqual(got, "tck_dq")

    def test_env_file_supports_export_prefix(self):
        path = self._write("auth.env", "export TCKDB_API_KEY='tck_exp'\n")
        os.environ.pop("TCKDB_API_KEY", None)
        got = resolve_tckdb_api_key(api_key_env_file=str(path))
        self.assertEqual(got, "tck_exp")

    def test_env_file_ignores_comments_and_blank_lines(self):
        body = (
            "# top comment\n"
            "\n"
            "OTHER_VAR=irrelevant\n"
            "   # indented comment\n"
            "\n"
            "export TCKDB_API_KEY='tck_real'\n"
            "TRAILING=stuff\n"
        )
        path = self._write("auth.env", body)
        os.environ.pop("TCKDB_API_KEY", None)
        got = resolve_tckdb_api_key(api_key_env_file=str(path))
        self.assertEqual(got, "tck_real")

    def test_env_file_missing_var_raises(self):
        path = self._write("auth.env", "OTHER_VAR=not_the_one\n")
        os.environ.pop("TCKDB_API_KEY", None)
        with self.assertRaises(InputError) as ctx:
            resolve_tckdb_api_key(api_key_env_file=str(path))
        self.assertIn("does not define", str(ctx.exception))

    def test_env_file_missing_path_raises(self):
        os.environ.pop("TCKDB_API_KEY", None)
        with self.assertRaises(InputError) as ctx:
            resolve_tckdb_api_key(
                api_key_env_file=str(self.tmp / "no_such_auth.env"),
            )
        self.assertIn("does not exist", str(ctx.exception))

    def test_env_file_does_not_execute_shell(self):
        # If the parser were sourcing the file, the $(...) would run
        # and the value would equal "SHOULD_NOT_RUN" (echo's output).
        # Asserting we get the literal, untransformed token proves the
        # subshell never ran.
        body = "TCKDB_API_KEY='$(echo SHOULD_NOT_RUN)'\n"
        path = self._write("auth.env", body)
        os.environ.pop("TCKDB_API_KEY", None)
        got = resolve_tckdb_api_key(api_key_env_file=str(path))
        self.assertEqual(got, "$(echo SHOULD_NOT_RUN)")
        self.assertNotEqual(got, "SHOULD_NOT_RUN")

    def test_env_file_does_not_interpolate_dollar_vars(self):
        # POSIX shlex does not expand $VAR. Confirm we hand back the
        # literal "$HOME" rather than its expanded value.
        path = self._write("auth.env", "TCKDB_API_KEY='$HOME'\n")
        os.environ.pop("TCKDB_API_KEY", None)
        got = resolve_tckdb_api_key(api_key_env_file=str(path))
        self.assertEqual(got, "$HOME")

    def test_env_file_uses_configured_var_name(self):
        path = self._write("auth.env", "MY_KEY='tck_custom'\n")
        os.environ.pop("MY_KEY", None)
        got = resolve_tckdb_api_key(
            api_key_env="MY_KEY",
            api_key_env_file=str(path),
        )
        self.assertEqual(got, "tck_custom")

    # Resolution-order priority ---------------------------------------------

    def test_api_key_file_takes_precedence_over_env_file(self):
        kf = self._write("key.txt", "tck_from_keyfile")
        ef = self._write("auth.env", "TCKDB_API_KEY='tck_from_envfile'\n")
        os.environ.pop("TCKDB_API_KEY", None)
        got = resolve_tckdb_api_key(
            api_key_file=str(kf),
            api_key_env_file=str(ef),
        )
        self.assertEqual(got, "tck_from_keyfile")


class TestReadEnvFileHelper(unittest.TestCase):
    """Direct tests of the env-file parser."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "auth.env"

    def _write(self, content: str) -> Path:
        self.path.write_text(content, encoding="utf-8")
        return self.path

    def test_returns_none_when_var_absent(self):
        self._write("OTHER=xx\n")
        self.assertIsNone(_read_tckdb_api_key_from_env_file(self.path, "TCKDB_API_KEY"))

    def test_first_match_wins(self):
        self._write("TCKDB_API_KEY=first\nTCKDB_API_KEY=second\n")
        self.assertEqual(
            _read_tckdb_api_key_from_env_file(self.path, "TCKDB_API_KEY"),
            "first",
        )
