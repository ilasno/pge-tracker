"""Configuration loading from TOML files."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class Config:
    username: str
    password: str
    totp_secret: str | None
    db_path: Path
    timezone: str
    peak_hours_start: int
    peak_hours_end: int
    default_meter: str  # "electric" | "gas" | "both"
    initial_fetch_days: int


_SEARCH_PATHS = [
    Path.home() / ".config" / "pge-tracker" / "config.toml",
    Path("config.toml"),
]


def find_config_path() -> Path | None:
    """Find the first existing config file in the search order."""
    env_path = os.environ.get("PGE_TRACKER_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    for p in _SEARCH_PATHS:
        if p.exists():
            return p
    return None


def load_config(path: Path | None = None) -> Config:
    """Load and validate configuration from a TOML file.

    Search order:
    1. Explicit path argument
    2. $PGE_TRACKER_CONFIG env var
    3. ~/.config/pge-tracker/config.toml
    4. ./config.toml
    """
    if path is None:
        path = find_config_path()
    if path is None:
        raise FileNotFoundError(
            "No configuration file found. Looked in:\n"
            "  - $PGE_TRACKER_CONFIG\n"
            "  - ~/.config/pge-tracker/config.toml\n"
            "  - ./config.toml\n\n"
            "Copy config.example.toml to one of these locations "
            "and fill in your PG&E credentials."
        )

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    pge = raw.get("pge", {})
    db = raw.get("database", {})
    prefs = raw.get("preferences", {})

    username = pge.get("username")
    password = pge.get("password")
    if not username or not password:
        raise ValueError(
            f"Config at {path}: [pge] username and password are required."
        )

    db_path_str = db.get("path", "data/pge_tracker.db")
    db_path = Path(db_path_str)
    if not db_path.is_absolute():
        db_path = path.parent / db_path

    return Config(
        username=username,
        password=password,
        totp_secret=pge.get("totp_secret"),
        db_path=db_path,
        timezone=prefs.get("timezone", "America/Los_Angeles"),
        peak_hours_start=int(prefs.get("peak_hours_start", 16)),
        peak_hours_end=int(prefs.get("peak_hours_end", 21)),
        default_meter=prefs.get("default_meter", "both"),
        initial_fetch_days=int(prefs.get("initial_fetch_days", 365)),
    )
