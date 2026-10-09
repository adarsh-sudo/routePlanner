"""The .env reader, and how settings use it."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from config import envfile

KEY = "test-key-not-a-real-one"


class ParseTests(SimpleTestCase):
    def test_plain_lines_comments_and_blanks(self):
        text = "# a comment\n\nROUTING_ENGINE=ors\nORS_API_KEY=abc123\n   # indented comment\n"
        self.assertEqual(envfile.parse(text), {"ROUTING_ENGINE": "ors", "ORS_API_KEY": "abc123"})

    def test_spaces_quotes_export_and_equals_signs_in_values(self):
        text = "  A = spaced  \nB='single quoted'\nC=\"double quoted\"\nexport D=exported\nE=x=y=z\nF=\n"
        self.assertEqual(
            envfile.parse(text),
            {"A": "spaced", "B": "single quoted", "C": "double quoted", "D": "exported", "E": "x=y=z", "F": ""},
        )

    def test_a_trailing_comment_is_dropped_unless_the_value_is_quoted(self):
        text = "A=value # note\nB=\"value # kept\"\nC=a#b\n"
        self.assertEqual(envfile.parse(text), {"A": "value", "B": "value # kept", "C": "a#b"})

    def test_windows_line_endings_and_a_byte_order_mark(self):
        self.assertEqual(envfile.parse("﻿A=1\r\nB=2\r\n"), {"A": "1", "B": "2"})

    def test_lines_that_are_not_name_equals_value_are_ignored(self):
        self.assertEqual(envfile.parse("just words\n1BAD=x\nBAD-NAME=x\n=novalue\nGOOD=1\n"), {"GOOD": "1"})

    def test_the_example_file_changes_nothing_when_copied_unchanged(self):
        example = (Path(settings.BASE_DIR) / ".env.example").read_text(encoding="utf-8")
        self.assertEqual(envfile.parse(example), {})  # every line is a comment: no engine switched on, no key


class LoadTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / ".env"

    def test_values_are_loaded_into_the_environment_given(self):
        self.path.write_text("ROUTING_ENGINE=ors\nORS_API_KEY=abc\n", encoding="utf-8")
        env = {}
        self.assertEqual(sorted(envfile.load(self.path, env)), ["ORS_API_KEY", "ROUTING_ENGINE"])
        self.assertEqual(env, {"ROUTING_ENGINE": "ors", "ORS_API_KEY": "abc"})

    def test_a_real_environment_variable_wins_over_the_file(self):
        self.path.write_text("ORS_API_KEY=from-file\nROUTING_ENGINE=ors\n", encoding="utf-8")
        env = {"ORS_API_KEY": "from-shell"}
        self.assertEqual(envfile.load(self.path, env), ["ROUTING_ENGINE"])
        self.assertEqual(env["ORS_API_KEY"], "from-shell")

    def test_a_blank_value_is_not_loaded(self):
        self.path.write_text("ORS_API_KEY=\nROUTING_ENGINE=\n", encoding="utf-8")
        env = {}
        self.assertEqual(envfile.load(self.path, env), [])
        self.assertEqual(env, {})

    def test_a_missing_file_is_fine(self):
        self.assertEqual(envfile.load(self.path, {}), [])

    def test_notepad_and_powershell_utf16_files_are_understood(self):
        self.path.write_text("ORS_API_KEY=abc\n", encoding="utf-16")  # what `echo ... > .env` writes in PowerShell 5
        env = {}
        envfile.load(self.path, env)
        self.assertEqual(env, {"ORS_API_KEY": "abc"})
        self.path.write_text("ORS_API_KEY=def\n", encoding="utf-8-sig")  # what Notepad's "UTF-8" writes
        env = {}
        envfile.load(self.path, env)
        self.assertEqual(env, {"ORS_API_KEY": "def"})

    def test_a_file_in_another_encoding_says_what_to_do(self):
        self.path.write_bytes("ORS_API_KEY=caf\xe9\n".encode("cp1252"))
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            envfile.load(self.path, {})


class WhichFileSettingsReadTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.default = Path(folder.name) / ".env"
        self.default.write_text("ROUTING_ENGINE=ors\n", encoding="utf-8")
        self.other = Path(folder.name) / "other.env"
        self.other.write_text("ORS_API_KEY=from-other\n", encoding="utf-8")

    def test_the_server_and_other_commands_read_the_default_file(self):
        for command in ("runserver", "check", "check_ors", "migrate"):
            env = {}
            envfile.load_for_settings(self.default, ["manage.py", command], env)
            self.assertEqual(env, {"ROUTING_ENGINE": "ors"}, command)

    def test_the_test_command_ignores_it_so_a_developers_own_file_cannot_change_the_tests(self):
        env = {}
        self.assertEqual(envfile.load_for_settings(self.default, ["manage.py", "test"], env), [])
        self.assertEqual(env, {})

    def test_dotenv_path_picks_another_file_even_for_the_test_command(self):
        env = {"DOTENV_PATH": str(self.other)}
        envfile.load_for_settings(self.default, ["manage.py", "test"], env)
        self.assertEqual(env, {"DOTENV_PATH": str(self.other), "ORS_API_KEY": "from-other"})

    def test_started_without_a_command_line_it_still_reads_the_default_file(self):
        env = {}  # for example gunicorn or a python -c import: argv has no "test"
        envfile.load_for_settings(self.default, ["gunicorn"], env)
        self.assertEqual(env, {"ROUTING_ENGINE": "ors"})


class SettingsReadTheFileTests(SimpleTestCase):
    """Import settings in a fresh process with a .env file, as a real start-up would."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dotenv = Path(folder.name) / ".env"

    def settings_with(self, file_text, **env):
        self.dotenv.write_text(file_text, encoding="utf-8")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("ROUTING_", "ORS_", "TRUCK_"))}
        code = "import config.settings as s; print(s.ROUTING_ENGINE, s.ORS_API_KEY, s.TRUCK['HEIGHT_M'])"
        return subprocess.run(
            [sys.executable, "-c", code], cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=60,
            env={**base, "DOTENV_PATH": str(self.dotenv), **env},
        )

    def test_the_engine_the_key_and_the_truck_come_from_the_file(self):
        done = self.settings_with(f"ROUTING_ENGINE=ors\nORS_API_KEY={KEY}\nTRUCK_HEIGHT_M=3.9\n")
        self.assertEqual((done.returncode, done.stdout.split()), (0, ["ors", KEY, "3.9"]), done.stderr)

    def test_a_real_environment_variable_beats_the_file(self):
        done = self.settings_with("ROUTING_ENGINE=ors\nORS_API_KEY=from-file\n", ORS_API_KEY=KEY)
        self.assertEqual(done.stdout.split()[:2], ["ors", KEY], done.stderr)

    def test_the_engine_in_the_file_with_no_key_anywhere_stops_start_up_and_says_where_to_put_it(self):
        done = self.settings_with("ROUTING_ENGINE=ors\nORS_API_KEY=\n")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn(".env", done.stderr)
        self.assertIn("ORS_API_KEY", done.stderr)

    def test_a_key_in_the_file_alone_switches_on_truck_routing(self):
        done = self.settings_with(f"ORS_API_KEY={KEY}\n")  # no ROUTING_ENGINE line
        self.assertEqual(done.stdout.split()[:2], ["ors", KEY], done.stderr)

    def test_with_no_file_it_is_osrm_and_needs_no_key(self):
        done = self.settings_with("")
        self.assertEqual(done.stdout.split()[:1], ["osrm"], done.stderr)
