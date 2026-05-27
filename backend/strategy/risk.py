from __future__ import annotations

from backend.models import IndicatorSnapshot, RiskParameters, SignalDecision, SignalType


class RiskEngine:
    def __init__(self, params: RiskParameters | None = None) -> None:
        self.params = params or RiskParameters()

    def can_take_trade(
        self,
        *,
        trade_count: int,
        daily_realized_pnl: float,
        consecutive_losses: int,
    ) -> tuple[bool, str]:
        if trade_count >= self.params.max_trades_per_day:
            return False, "Max trades per day reached"
        if daily_realized_pnl <= -(self.params.capital * self.params.max_daily_loss_pct):
            return False, "Max daily loss breached"
        if consecutive_losses >= self.params.cooldown_after_losses:
            return False, "Loss cooldown active"
        return True, "OK"

    def build_order_plan(
        self,
        signal: SignalDecision,
        snapshot: IndicatorSnapshot,
    ) -> dict[str, float | int | str]:
        if signal.signal not in {SignalType.BUY, SignalType.SELL}:
            return {}

        entry = snapshot.close
        atr_stop = snapshot.atr * self.params.atr_stop_multiple
        fixed_stop = entry * self.params.fixed_stop_pct
        stop_distance = max(atr_stop, fixed_stop)

        if signal.signal == SignalType.BUY:
            stop_loss = entry - stop_distance
            target = entry + stop_distance * self.params.risk_reward_ratio
        else:
            stop_loss = entry + stop_distance
            target = entry - stop_distance * self.params.risk_reward_ratio

        risk_budget = self.params.capital * self.params.risk_per_trade
        quantity = max(1, int(risk_budget / max(stop_distance, 0.01)))

        return {
            "entry_price": round(entry, 4),
            "stop_loss": round(stop_loss, 4),
            "target": round(target, 4),
            "quantity": quantity,
            "stop_distance": round(stop_distance, 4),
        }
