from __future__ import annotations

from backend.models import IndicatorSnapshot, PositionSide, RiskParameters, SignalDecision, SignalType


class RiskEngine:
    def __init__(self, params: RiskParameters | None = None) -> None:
        self.params = params or RiskParameters()

    def can_trade(
        self,
        *,
        equity: float,
        day_pnl: float,
        trade_count: int,
        consecutive_losses: int,
        peak_equity: float,
    ) -> tuple[bool, str]:
        if trade_count >= self.params.max_trades_per_day:
            return False, "Max trades per day reached"
        if day_pnl <= -(self.params.capital * self.params.max_daily_loss_pct):
            return False, "Max daily loss reached"
        if consecutive_losses >= self.params.cooldown_after_losses:
            return False, "Consecutive-loss cooldown active"
        if peak_equity > 0 and equity <= peak_equity * (1 - self.params.max_drawdown_pct):
            return False, "Drawdown protection active"
        return True, "OK"

    def build_trade_plan(
        self,
        signal: SignalDecision,
        snapshot: IndicatorSnapshot,
        entry_price: float,
    ) -> dict[str, float | int | str]:
        atr_stop_distance = max(snapshot.atr * self.params.atr_stop_multiple, 0.01)
        stop_distance = max(self.params.fixed_stop_points, atr_stop_distance, 0.01)
        target_distance = (
            self.params.fixed_target_points
            if self.params.fixed_target_points > 0
            else stop_distance * self.params.risk_reward_ratio
        )
        trailing_distance = max(
            self.params.trail_lock_points if self.params.trail_lock_points > 0 else snapshot.atr * self.params.trailing_atr_multiple,
            0.01,
        )
        trail_trigger = self.params.trail_trigger_points if self.params.trail_trigger_points > 0 else trailing_distance
        if signal.signal == SignalType.BUY:
            stop_loss = entry_price - stop_distance
            target = entry_price + target_distance
            side = PositionSide.LONG
        else:
            stop_loss = entry_price + stop_distance
            target = entry_price - target_distance
            side = PositionSide.SHORT

        risk_budget = self.params.capital * self.params.risk_per_trade
        lots = max(1, int(risk_budget / max(stop_distance * self.params.lot_size, 0.01)))
        quantity = max(self.params.lot_size, lots * self.params.lot_size)
        return {
            "side": side.value,
            "entry_price": round(entry_price, 4),
            "stop_loss": round(stop_loss, 4),
            "target": round(target, 4),
            "trailing_distance": round(trailing_distance, 4),
            "trail_trigger": round(trail_trigger, 4),
            "quantity": quantity,
            "risk_budget": round(risk_budget, 4),
            "lot_size": self.params.lot_size,
        }
