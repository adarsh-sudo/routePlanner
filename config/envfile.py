"""A tiny reader for a ``.env`` file (``NAME=value`` lines), so settings can live in a private file instead of the shell.

No package needed. ``settings.py`` loads ``.env`` from the folder with ``manage.py``; a real environment variable
always wins over the file, and a blank value in the file is ignored.
"""

import codecs
import os
from pathlib import Path


def parse(text):
    """``{name: value}`` from the text of a ``.env`` file.

    Blank lines and ``#`` comments are skipped. ``export NAME=value`` is accepted. A value may be wrapped in single or
    double quotes (kept as written inside, ``#`` included); an unquoted value ends at `` #``. Lines that are not
    ``NAME=value`` are ignored.
    """
    values = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, equals, value = line.partition("=")
        name = name.strip()
        if not equals or not name or name[0].isdigit() or not name.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


def load_for_settings(default_path, argv, environ=None):
    """What ``settings.py`` calls: load ``DOTENV_PATH`` if that is set, else ``default_path``.

    ``manage.py test`` ignores the default file, so a developer's own ``.env`` (a key, a different engine, a blank
    value awaiting a key) cannot change or block the test run; the tests set what they need themselves.
    """
    environ = os.environ if environ is None else environ
    explicit = environ.get("DOTENV_PATH")
    if not explicit and argv[1:2] == ["test"]:
        return []
    return load(explicit or default_path, environ)


def load(path, environ=None):
    """Put the values from the ``.env`` file at ``path`` into ``environ`` (default ``os.environ``).

    A variable that is already set there is left alone (a real environment variable wins), and so is a blank value.
    A missing file is fine. Returns the names it set. Raises ValueError if the file is not text it can read:
    UTF-8 is expected, and UTF-16 (what PowerShell's ``>`` and Notepad's "Unicode" write) is understood too.
    """
    environ = os.environ if environ is None else environ
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    try:
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not a text file this can read: save it as UTF-8.") from exc
    loaded = []
    for name, value in parse(text).items():
        if value and name not in environ:
            environ[name] = value
            loaded.append(name)
    return loaded
