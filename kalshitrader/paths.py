"""Where the app finds its files, whether run from a checkout or a frozen executable.

Two things break when a Python app is turned into a single downloadable binary, and
both are cheaper to get right now than to retrofit:

  bundled assets  under PyInstaller the package is unpacked to a temporary directory
                  named by `sys._MEIPASS`, so `Path(__file__).parent` is not where
                  the data files ended up. `asset()` looks in the right place either
                  way.

  writable state  a checkout can keep its database in ./data, but someone who
                  double-clicks an executable in their Downloads folder must not have
                  a database and their API keys written next to it - the directory may
                  be read-only, synced to the cloud, or cleared. `user_data_dir()`
                  returns the per-user application directory the OS intends for this,
                  and only falls back to ./data when running from a source checkout.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def frozen() -> bool:
    """True when running inside a PyInstaller (or similar) bundle."""
    return getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    """The directory bundled read-only data was unpacked to."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else Path(__file__).resolve().parent


def asset(*parts: str) -> Path:
    """A read-only file shipped with the app, e.g. `asset("dashboard", "static")`."""
    if frozen():
        return bundle_root().joinpath("kalshitrader", *parts)
    return Path(__file__).resolve().parent.joinpath(*parts)


def user_data_dir() -> Path:
    """Where this install keeps its database, settings and logs.

    `KALSHITRADER_HOME` overrides everything, so a portable install can keep its state
    on a USB stick and a test can point somewhere disposable.
    """
    override = os.environ.get("KALSHITRADER_HOME")
    if override:
        return Path(override).expanduser()
    if not frozen() and Path("pyproject.toml").exists():
        return Path("data")  # a source checkout keeps its data in the tree, as before
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "KalshiTrader"


def user_file(name: str) -> Path:
    """A writable path under the user data directory, with parents created."""
    path = user_data_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resolve_state_path(path: str) -> str:
    """Where a writable file named in config should actually live.

    From a source checkout, exactly where it says: `data/kalshitrader.db` stays in the
    tree. From a frozen executable a relative path is meaningless - it would resolve
    against whatever directory the user happened to double-click from - so it moves
    under the per-user application directory. Absolute paths are always honoured.
    """
    p = Path(path)
    if p.is_absolute() or not frozen():
        return str(p)
    target = user_data_dir() / p.name
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)
