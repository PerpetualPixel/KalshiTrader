"""One entry point that runs the whole thing: dashboard, trading loop and browser.

This is what a downloadable executable calls. Everything the user needs happens in
one process and one window - no second terminal, no `pip install`, no start script -
because "download it, run it, paste your keys" only works if there is one thing to
run and it explains itself when something is missing.

The trading loop runs on a background thread and the dashboard owns the main thread,
so closing the window stops both. The loop is started even without credentials: the
dashboard is how you enter them, and it reloads them on the next scan.
"""
from __future__ import annotations

import logging
import threading
import time
import webbrowser

from kalshitrader.config import Settings, load_settings
from kalshitrader.log import setup_logging
from kalshitrader.paths import user_file

log = logging.getLogger(__name__)


def _loop(settings: Settings, env_file: str) -> None:
    """The trading loop, restarted if it ever falls over."""
    from kalshitrader.cli import _engine

    while True:
        try:
            _engine(settings, env_file=env_file).run_forever()
            return  # a clean return means it was asked to stop
        except KeyboardInterrupt:
            return
        except Exception:
            # A crashed loop must not take the dashboard with it, or someone whose
            # keys are wrong loses the only screen that can fix them.
            log.exception("trading loop stopped unexpectedly; restarting in 30s")
            time.sleep(30)


def main(env_file: str = ".env", port: int | None = None, open_browser: bool = True) -> int:
    setup_logging()
    try:
        settings = load_settings(env_file)
    except ValueError as exc:
        # An unusable .env is a reason to show the dashboard, not to refuse to start:
        # the dashboard is where it gets fixed.
        log.warning("%s - starting in paper mode so you can fix it in Settings", exc)
        settings = Settings(trading_mode="paper", db_path=str(user_file("kalshitrader.db")))

    port = port or settings.dashboard_port
    threading.Thread(target=_loop, args=(settings, env_file), daemon=True, name="trading").start()

    url = f"http://{settings.dashboard_host}:{port}"
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"KalshiTrader is running. Dashboard: {url}")
    print("The bot starts paused. Add your Kalshi keys in Settings, pick your markets, then press Resume.")

    import uvicorn

    from kalshitrader.dashboard.app import create_app

    uvicorn.run(create_app(settings, env_file=env_file), host=settings.dashboard_host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":  # `python -m kalshitrader.app`
    raise SystemExit(main())
