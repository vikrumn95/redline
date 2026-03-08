"""Unit tests: load_config() DB path defaults and mock-in-prod safety gate."""
from __future__ import annotations

import pytest

from redline.config import load_config


# ---------------------------------------------------------------------------
# Safety gate: mock blocked in prod
# ---------------------------------------------------------------------------


def test_mock_blocked_in_prod_explicit(monkeypatch):
    """REDLINE_MOCK=true with REDLINE_ENV=prod must exit 1."""
    monkeypatch.setenv("REDLINE_ENV", "prod")
    monkeypatch.setenv("REDLINE_MOCK", "true")
    monkeypatch.setenv("COINBASE_API_KEY", "key")  # prevent separate auto-mock path

    with pytest.raises(SystemExit) as exc_info:
        load_config(env_file=None)

    assert exc_info.value.code == 1


def test_mock_blocked_in_prod_via_missing_api_key(monkeypatch):
    """Missing COINBASE_API_KEY auto-enables mock; must also be blocked in prod."""
    monkeypatch.setenv("REDLINE_ENV", "prod")
    monkeypatch.delenv("COINBASE_API_KEY", raising=False)
    monkeypatch.delenv("REDLINE_MOCK", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        load_config(env_file=None)

    assert exc_info.value.code == 1


def test_mock_allowed_in_dev(monkeypatch):
    """Mock mode is fine in dev — must not raise."""
    monkeypatch.setenv("REDLINE_ENV", "dev")
    monkeypatch.setenv("REDLINE_MOCK", "true")
    monkeypatch.delenv("REDLINE_DB_PATH", raising=False)

    cfg = load_config(env_file=None)  # must not raise
    assert cfg.mock is True


# ---------------------------------------------------------------------------
# Default DB path by env / mock
# ---------------------------------------------------------------------------


def test_default_db_is_mock_when_mock_true(monkeypatch):
    monkeypatch.setenv("REDLINE_ENV", "dev")
    monkeypatch.setenv("REDLINE_MOCK", "true")
    monkeypatch.delenv("REDLINE_DB_PATH", raising=False)

    cfg = load_config(env_file=None)
    assert cfg.db_path.name == "redline-mock.db"


def test_default_db_is_mock_when_api_key_missing(monkeypatch):
    """Auto-mock (no API key) also routes to redline-mock.db."""
    monkeypatch.setenv("REDLINE_ENV", "dev")
    monkeypatch.delenv("COINBASE_API_KEY", raising=False)
    monkeypatch.delenv("REDLINE_MOCK", raising=False)
    monkeypatch.delenv("REDLINE_DB_PATH", raising=False)

    cfg = load_config(env_file=None)
    assert cfg.db_path.name == "redline-mock.db"


def test_default_db_is_dev_when_env_dev_and_not_mock(monkeypatch):
    monkeypatch.setenv("REDLINE_ENV", "dev")
    monkeypatch.setenv("COINBASE_API_KEY", "key")  # prevent auto-mock
    monkeypatch.delenv("REDLINE_MOCK", raising=False)
    monkeypatch.delenv("REDLINE_DB_PATH", raising=False)

    cfg = load_config(env_file=None)
    assert cfg.db_path.name == "redline-dev.db"


def test_default_db_is_prod_when_env_prod_and_not_mock(monkeypatch):
    monkeypatch.setenv("REDLINE_ENV", "prod")
    monkeypatch.setenv("COINBASE_API_KEY", "key")  # prevent auto-mock
    monkeypatch.delenv("REDLINE_MOCK", raising=False)
    monkeypatch.delenv("REDLINE_DB_PATH", raising=False)

    cfg = load_config(env_file=None)
    assert cfg.db_path.name == "redline.db"


def test_explicit_db_path_overrides_default(monkeypatch, tmp_path):
    """REDLINE_DB_PATH always wins regardless of env/mock."""
    monkeypatch.setenv("REDLINE_ENV", "dev")
    monkeypatch.setenv("REDLINE_MOCK", "true")
    monkeypatch.setenv("REDLINE_DB_PATH", str(tmp_path / "custom.db"))

    cfg = load_config(env_file=None)
    assert cfg.db_path == tmp_path / "custom.db"
