"""Test package marker and settings isolation.

The explicit package prevents test modules from resolving against another
checkout. It also redirects test saves away from the user's real settings
file, including for targeted `python -m unittest tests.test_module` runs.
"""
import os
import pathlib
import tempfile

import config.settings as _settings

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SETTINGS_ENV = "HARMONY_TEST_SETTINGS_PATH"

if SETTINGS_ENV not in os.environ:
    os.environ[SETTINGS_ENV] = str(
        pathlib.Path(tempfile.mkdtemp(prefix="harmony-tests-settings-"))
        / "settings.json"
    )

_settings.SETTINGS_PATH = pathlib.Path(os.environ[SETTINGS_ENV])
