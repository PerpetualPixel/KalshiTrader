"""Pre-trade risk checks and circuit breakers.

Every order the strategies want to send passes through `check_order` first.
The daily stop-loss is evaluated against the first equity snapshot of the
day; once tripped, the engine cancels all resting orders and refuses new
ones until the breaker is manually reset from the dashboard.
"""
from __future__ import annotations

import dataclasses
import logging

from .config import BotSettings

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""


class RiskManager:
    def __init__(self, settings: BotSettings):
        self.settings = settings
        self.halted: bool = False
        self.halt_reason: str = ""

    def update_settings(self, settings: BotSettings) -> None:
        self.settings = settings

    # ── Circuit breakers ──────────────────────────────────────────────

    def check_daily_stop(self, day_start_equity_cents: int | None, equity_cents: int) -> bool:
        """Returns True if the daily stop-loss just tripped."""
        if self.halted or not day_start_equity_cents or day_start_equity_cents <= 0:
            return False
        loss_pct = (day_start_equity_cents - equity_cents) / day_start_equity_cents * 100
        if loss_pct >= self.settings.daily_stop_loss_pct:
            self.trip(
                f"daily stop-loss: down {loss_pct:.2f}% "
                f"(limit {self.settings.daily_stop_loss_pct}%)"
            )
            return True
        return False

    def trip(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        logger.warning("RISK HALT: %s", reason)

    def reset(self) -> None:
        self.halted = False
        self.halt_reason = ""

    # ── Pre-trade checks ──────────────────────────────────────────────

    def check_order(
        self,
        count: int,
        price_cents: int,
        money_working_cents: int,
        action: str = "buy",
    ) -> RiskDecision:
        """Validate a prospective order.

        money_working_cents: capital already committed to open orders and
        positions, before this order. Sells reduce exposure, so the working-
        capital ceiling only applies to buys — an exit must never be trapped
        behind the allocation limit.
        """
        if self.halted:
            return RiskDecision(False, f"trading halted: {self.halt_reason}")
        if count <= 0:
            return RiskDecision(False, "count must be positive")
        if count > self.settings.max_contracts_per_order:
            return RiskDecision(
                False,
                f"count {count} exceeds max_contracts_per_order "
                f"{self.settings.max_contracts_per_order}",
            )
        cost = count * price_cents
        if action == "buy" and money_working_cents + cost > self.settings.max_money_working_cents:
            return RiskDecision(
                False,
                f"order cost {cost}c would push working capital past "
                f"{self.settings.max_money_working_cents}c ceiling "
                f"(currently {money_working_cents}c)",
            )
        return RiskDecision(True)
