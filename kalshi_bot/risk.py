"""Risk limits enforced in the backtest and, later, in the live bot.

Defaults are the limits Carson approved for the first live stage:
5% daily loss cap and a 20% drawdown kill switch. Per-market and per-event
caps bound how much a single surprise outcome can cost.
"""

from dataclasses import dataclass


@dataclass
class RiskLimits:
    max_daily_loss_pct: float = 0.05
    max_drawdown_pct: float = 0.20
    max_market_loss_pct: float = 0.05  # worst-case loss in one market, as a share of starting bankroll
    max_event_loss_pct: float = 0.10  # worst-case loss across one event's markets


class RiskManager:
    def __init__(self, limits: RiskLimits, starting_equity: float):
        self.limits = limits
        self.starting_equity = starting_equity
        self.peak_equity = starting_equity
        self.day = None
        self.day_start_equity = starting_equity
        self.halted = False  # drawdown kill switch: stays off for the rest of the run
        self.paused_today = False  # daily loss cap: resets at the next UTC day
        self.events: list[tuple] = []

    def update(self, now, equity: float) -> None:
        day = now.date()
        if day != self.day:
            self.day = day
            self.day_start_equity = equity
            self.paused_today = False
        self.peak_equity = max(self.peak_equity, equity)
        if not self.halted and equity < self.peak_equity * (1 - self.limits.max_drawdown_pct):
            self.halted = True
            self.events.append((now, "drawdown_kill_switch", equity))
        if not self.paused_today and equity < self.day_start_equity * (1 - self.limits.max_daily_loss_pct):
            self.paused_today = True
            self.events.append((now, "daily_loss_cap", equity))

    @property
    def trading_allowed(self) -> bool:
        return not (self.halted or self.paused_today)

    def allows(self, market_loss_after: float, event_loss_after: float) -> bool:
        if not self.trading_allowed:
            return False
        if market_loss_after > self.starting_equity * self.limits.max_market_loss_pct + 1e-9:
            return False
        return event_loss_after <= self.starting_equity * self.limits.max_event_loss_pct + 1e-9
