"""
conftest.py — pytest-wide fixtures.

Currently provides one fixture: isolate the XDG data home (and config
home) per session into a temp directory so that test runs do not read
or write the user's real cache / config files.

Without this, R7's `lookup_cache.db` would persist results between
tests, causing one test's mocked MB response to bleed into another
test that expects a network error.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_xdg_dirs():
    """Point XDG_DATA_HOME and XDG_CONFIG_HOME at a session-scoped temp dir.

    autouse=True so every test runs under the isolated paths without
    each test having to opt in. session scope keeps the temp dir alive
    across all tests for performance.
    """
    with tempfile.TemporaryDirectory() as td:
        old_data = os.environ.get("XDG_DATA_HOME")
        old_config = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_DATA_HOME"] = td + "/data"
        os.environ["XDG_CONFIG_HOME"] = td + "/config"
        try:
            yield
        finally:
            if old_data is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old_data
            if old_config is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old_config


@pytest.fixture(autouse=True)
def _clear_lookup_cache():
    """Delete the R7 lookup cache before every test.

    A function-scoped reset prevents one test's mocked MB response from
    bleeding into another test (e.g. a network-error test seeing a prior
    test's success-cached value for the same disc-ID).
    """
    from cdda2img.lookup_cache import cache_db_path

    path = cache_db_path()
    if path.exists():
        path.unlink()
    yield
