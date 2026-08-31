"""
Configuration loader for PRECISE Scheduler.

Reads from (in order of preference):
  1. $XDG_CONFIG_HOME/precise/config.toml
  2. ~/.config/precise/config.toml
  3. $HOME/.precise/config.toml

A path can be overridden programmatically for testing.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable, or has missing keys."""


def _default_config_path() -> Path:
    """Return the first candidate config path that exists, or the primary candidate."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    candidates = [
        Path(xdg) / "precise" / "config.toml" if xdg else None,
        Path.home() / ".config" / "precise" / "config.toml",
        Path.home() / ".precise" / "config.toml",
    ]
    for p in candidates:
        if p is not None and p.exists():
            return p
    # Return primary even if it doesn't exist so the error message is useful.
    return Path.home() / ".config" / "precise" / "config.toml"


@dataclass
class Config:
    """Typed configuration for the PRECISE Scheduler."""

    # [paths] — only one path required; targets_toml and activity_db are derived
    schedules_repo: Path

    # [google]
    google_spreadsheet_id: str

    # [slack]
    slack_bot_token: str
    slack_channel_id: str
    slack_signing_secret: str
    slack_webhook_port: int

    # [mattermost]
    mattermost_webhook_url: str

    # [antenna_server]
    antenna_host: str
    antenna_user: str
    antenna_ssh_key: Path
    antenna_base_path: str

    # Fields with defaults must come last
    # [daemon]
    poll_interval_hours: int = 6
    lookahead_days: int = 10

    # debug mode — never sends anything to the antenna server
    debug: bool = False

    # ------------------------------------------------------------------ #
    # Class-level override for testing                                     #
    # ------------------------------------------------------------------ #
    _override_path: ClassVar[Path | None] = None

    # ------------------------------------------------------------------ #
    # Derived paths (resolved from schedules_repo)                        #
    # ------------------------------------------------------------------ #

    @property
    def targets_toml(self) -> Path:
        return self.schedules_repo / "targets.toml"

    @property
    def activity_db(self) -> Path:
        return self.schedules_repo / "activity.csv"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """
        Load and return a Config from *path*.  If *path* is None, search the
        standard XDG / home locations.

        Raises
        ------
        ConfigError
            If the file cannot be found, cannot be parsed, or is missing
            required keys.
        """
        resolved = path or cls._override_path or _default_config_path()
        if not resolved.exists():
            raise ConfigError(
                f"Config file not found: {resolved}\n"
                f"Copy config.toml.example to {resolved} and fill in your values."
            )
        try:
            with open(resolved, "rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Failed to parse config file {resolved}: {exc}") from exc

        try:
            return cls(
                # paths
                schedules_repo=Path(raw["paths"]["schedules_repo"]).expanduser(),
                # google
                google_spreadsheet_id=raw["google"]["spreadsheet_id"],
                # slack
                slack_bot_token=raw["slack"]["bot_token"],
                slack_channel_id=raw["slack"]["channel_id"],
                slack_signing_secret=raw["slack"]["signing_secret"],
                slack_webhook_port=int(raw["slack"].get("webhook_port", 8765)),
                # mattermost
                mattermost_webhook_url=raw["mattermost"]["webhook_url"],
                # antenna server
                antenna_host=raw["antenna_server"]["host"],
                antenna_user=raw["antenna_server"]["user"],
                antenna_ssh_key=Path(raw["antenna_server"]["ssh_key"]).expanduser(),
                antenna_base_path=raw["antenna_server"]["base_path"],
                # daemon
                poll_interval_hours=int(raw.get("daemon", {}).get("poll_interval_hours", 6)),
                lookahead_days=int(raw.get("daemon", {}).get("lookahead_days", 10)),
                # debug
                debug=bool(raw.get("debug", {}).get("enabled", False)),
            )
        except KeyError as exc:
            raise ConfigError(
                f"Missing required config key {exc} in {resolved}.\n"
                f"Compare your file against config.toml.example."
            ) from exc
