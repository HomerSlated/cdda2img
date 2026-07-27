"""
conftest.py — pytest-wide fixtures.

Isolates XDG_DATA_HOME and XDG_CONFIG_HOME per session into a temp directory
so that test runs do not read or write the user's real config files, and pins
the AccuDisc transport so drive-seam tests assert about a chosen path.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from cdda2img.accudisc_reader import TRANSPORT_ENV


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
def _pin_accudisc_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the AccuDisc transport to the subprocess for every test.

    The suite mocks ``accudisc_reader.subprocess.run`` in a few dozen places. Under
    the default ``auto`` policy those mocks are only reached because the binding
    happens not to be importable here — so on a machine where it *is* importable
    the same assertions would silently exercise a different transport and still
    pass, which is the shape of a test that has stopped testing anything.

    Pinning makes the subprocess the *chosen* subject. A test that wants the
    binding path overrides this with its own ``monkeypatch.setenv``; there is no
    ambient default either way.
    """
    monkeypatch.setenv(TRANSPORT_ENV, "subprocess")
