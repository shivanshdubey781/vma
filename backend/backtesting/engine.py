from __future__ import annotations

from statistics import mean, pstdev

from backend.models import PerformanceSnapshot


class BacktestAnalytics:
    def build(self, trades: list[dict[str, object]], equity_curve: list[float]) -> PerformanceSnapshot:
        pnl_values = [float(trade["pnl_after_costs"]) for trade in trades]
        wins = [value for value in pnl_values if value > 0]
        losses = [value for value in pnl_values if value <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        expectancy = mean(pnl_values) if pnl_values else 0.0
        std_dev = pstdev(pnl_values) if len(pnl_values) > 1 else 0.0
        sharpe = expectancy / std_dev if std_dev else 0.0
        max_drawdown = self._max_drawdown(equity_curve)
        trades_count = len(trades)

        return PerformanceSnapshot(
            trades=trades_count,
            wins=len(wins),
            losses=len(losses),
            win_rate=round((len(wins) / trades_count) * 100, 4) if trades_count else 0.0,
            loss_rate=round((len(losses) / trades_count) * 100, 4) if trades_count else 0.0,
            gross_profit=round(gross_profit, 4),
            gross_loss=round(gross_loss, 4),
            net_profit=round(sum(pnl_values), 4),
            profit_factor=round(gross_profit / gross_loss, 4) if gross_loss else 0.0,
            max_drawdown=round(max_drawdown, 4),
            expectancy=round(expectancy, 4),
            sharpe_ratio=round(sharpe, 4),
            equity_curve=[round(value, 4) for value in equity_curve],
        )

    @staticmethod
    def _max_drawdown(equity_curve: list[float]) -> float:
        peak = equity_curve[0] if equity_curve else 0.0
        max_drawdown = 0.0
        for value in equity_curve:
            peak = max(peak, value)
            max_drawdown = max(max_drawdown, peak - value)
        return max_drawdown
