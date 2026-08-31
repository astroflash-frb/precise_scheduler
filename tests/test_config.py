"""Tests for the Config loader."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from precise_scheduler.config import Config, ConfigError


VALID_TOML = textwrap.dedent("""\
    [paths]
    schedules_repo = "/tmp/schedules"

    [google]
    spreadsheet_id = "abc123"

    [slack]
    bot_token = "xoxb-test"
    channel_id = "C123"
    signing_secret = "secret"
    webhook_port = 8765

    [mattermost]
    webhook_url = "https://mm.example.com/hooks/x"

    [antenna_server]
    host = "vlbeer.ira.inaf.it"
    user = "vlbeer"
    ssh_key = "/home/user/.ssh/id_ed25519"
    base_path = "vlbi_arch"

    [daemon]
    poll_interval_hours = 6
    lookahead_days = 10
""")


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content)
    return p


class TestConfigLoad:
    def test_valid_config_loads(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, VALID_TOML)
        cfg = Config.load(p)
        assert cfg.google_spreadsheet_id == "abc123"
        assert cfg.slack_channel_id == "C123"
        assert cfg.antenna_host == "vlbeer.ira.inaf.it"
        assert cfg.poll_interval_hours == 6
        assert cfg.lookahead_days == 10
        assert cfg.debug is False

    def test_paths_are_path_objects(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, VALID_TOML)
        cfg = Config.load(p)
        assert isinstance(cfg.schedules_repo, Path)
        assert isinstance(cfg.targets_toml, Path)
        assert isinstance(cfg.activity_db, Path)

    def test_derived_paths(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, VALID_TOML)
        cfg = Config.load(p)
        assert cfg.targets_toml == cfg.schedules_repo / "targets.toml"
        assert cfg.activity_db == cfg.schedules_repo / "activity.csv"

    def test_missing_file_raises_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            Config.load(tmp_path / "nonexistent.toml")

    def test_missing_key_raises_config_error(self, tmp_path: Path) -> None:
        broken = VALID_TOML.replace('bot_token = "xoxb-test"\n', "")
        p = _write_config(tmp_path, broken)
        with pytest.raises(ConfigError, match="bot_token"):
            Config.load(p)

    def test_debug_flag_from_config(self, tmp_path: Path) -> None:
        toml_with_debug = VALID_TOML + "\n[debug]\nenabled = true\n"
        p = _write_config(tmp_path, toml_with_debug)
        cfg = Config.load(p)
        assert cfg.debug is True

    def test_invalid_toml_raises_config_error(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, "this is not = valid toml ===")
        with pytest.raises(ConfigError, match="parse"):
            Config.load(p)

    def test_daemon_defaults_when_section_absent(self, tmp_path: Path) -> None:
        minimal = VALID_TOML.replace("[daemon]\npoll_interval_hours = 6\nlookahead_days = 10\n", "")
        p = _write_config(tmp_path, minimal)
        cfg = Config.load(p)
        assert cfg.poll_interval_hours == 6
        assert cfg.lookahead_days == 10

    def test_debug_defaults_false_when_section_absent(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, VALID_TOML)
        cfg = Config.load(p)
        assert cfg.debug is False


class TestXDGFallback:
    """Config._override_path mechanism used in tests."""

    def test_override_path(self, tmp_path: Path) -> None:
        p = _write_config(tmp_path, VALID_TOML)
        old = Config._override_path
        try:
            Config._override_path = p
            cfg = Config.load()  # no explicit path
            assert cfg.google_spreadsheet_id == "abc123"
        finally:
            Config._override_path = old
