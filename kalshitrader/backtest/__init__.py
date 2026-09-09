"""Replay recorded prices through the real strategy, so settings can be tested for free."""
from kalshitrader.backtest.replay import BacktestResult, load_history, run_backtest, sweep_settings

__all__ = ["BacktestResult", "load_history", "run_backtest", "sweep_settings"]
