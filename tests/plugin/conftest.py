import os
import sys
import tempfile
from pathlib import Path

import pytest

# Before anything imports Hermes: no test, and no import made while collecting them, may reach a developer's real
# ~/.hermes. Hermes falls back to that home when HERMES_HOME is unset, so a test never deletes it.
_COLLECTION_HOME = tempfile.TemporaryDirectory(prefix="hermes-kernel-tests-")
os.environ["HERMES_HOME"] = _COLLECTION_HOME.name

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import plugin  # noqa: E402,F401  (the package, imported once so tests can use `plugin.client`)


@pytest.fixture(autouse=True)
def own_hermes_home(tmp_path, monkeypatch):
    """Every test has its own empty Hermes home."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
