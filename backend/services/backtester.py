from __future__ import annotations

from statistics import mean, pstdev

from backend.models import IndicatorSnapshot, PerformanceSnapshot, SignalType, SimulatedTrade
from backend.services.paper_trader import PaperTradingEngine
from backend.strategy.risk import RiskEngine


class BacktestEngine:
    def __init__(self, risk_engine: RiskEngine | None = None, paper_engine: PaperTradingEngine | None = None) -> None:
        self.risk_engine = risk_engine or RiskEngine()
        self.paper_engine = paper_engine or PaperTradingEngine()

    def run(
        self,
        *,
        symbol: str,
        snapshots: list[IndicatorSnapshot],
        signals,
    ) -> tuple[list[SimulatedTrade], PerformanceSnapshot]:
        trades: list[SimulatedTrade] = []
        equity_curve = [self.risk_engine.params.capital]
        open_trade: SimulatedTrade | None = None
        daily_realized_pnl = 0.0
        consecutive_losses = 0

        for snapshot, signal in zip(snapshots, signals):
            if open_trade:
                open_trade = self._update_open_trade(open_trade, snapshot)
                if open_trade and open_trade.exit_time:
                    trades.append(open_trade)
                    daily_realized_pnl += open_trade.pnl_after_costs
                    equity_curve.append(equity_curve[-1] + open_trade.pnl_after_costs)
                    consecutive_losses = consecutive_losses + 1 if open_trade.pnl_after_costs < 0 else 0
                    open_trade = None

            if signal.signal == SignalType.HOLD or open_trade:
                continue

            allowed, _ = self.risk_engine.can_take_trade(
                trade_count=len(trades) + (1 if open_trade else 0),
                daily_realized_pnl=daily_realized_pnl,
                consecutive_losses=consecutive_losses,
            )
            if not allowed:
                continue

            order_plan = self.risk_engine.build_order_plan(signal, snapshot)
            if order_plan:
                open_trade = self.paper_engine.open_trade(
                    symbol=symbol,
                    signal=signal,
                    order_plan=order_plan,
                )

        if open_trade:
            final_snapshot = snapshots[-1]
            trades.append(
                self.paper_engine.close_trade(
                    open_trade,
                    timestamp=final_snapshot.timestamp,
                    exit_price=final_snapshot.close,
                    exit_reason="EOD",
                )
            )
            equity_curve.append(equity_curve[-1] + trades[-1].pnl_after_costs)

        return trades, self._build_performance(trades, equity_curve)

    def _update_open_trade(self, trade: SimulatedTrade, snapshot: IndicatorSnapshot) -> SimulatedTrade | None:
        if trade.side == SignalType.BUY:
            if snapshot.close <= trade.stop_loss:
                return self.paper_engine.close_trade(
                    trade,
                    timestamp=snapshot.timestamp,
                    exit_price=trade.stop_loss,
                    exit_reason="SL",
                )
            if snapshot.close >= trade.target:
                return self.paper_engine.close_trade(
                    trade,
                    timestamp=snapshot.timestamp,
                    exit_price=trade.target,
                    exit_reason="TARGET",
                )
        else:
            if snapshot.close >= trade.stop_loss:
                return self.paper_engine.close_trade(
                    trade,
                    timestamp=snapshot.timestamp,
                    exit_price=trade.stop_loss,
                    exit_reason="SL",
                )
            if snapshot.close <= trade.target:
                return self.paper_engine.close_trade(
                    trade,
                    timestamp=snapshot.timestamp,
                    exit_price=trade.target,
                    exit_reason="TARGET",
                )
        return trade

    @staticmethod
    def _build_performance(trades: list[SimulatedTrade], equity_curve: list[float]) -> PerformanceSnapshot:
        pnl_values = [trade.pnl_after_costs for trade in trades]
        wins = [value for value in pnl_values if value > 0]
        losses = [value for value in pnl_values if value <= 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        net_profit = sum(pnl_values)
        max_drawdown = BacktestEngine._max_drawdown(equity_curve)
        expectancy = mean(pnl_values) if pnl_values else 0.0
        std_dev = pstdev(pnl_values) if len(pnl_values) > 1 else 0.0
        sharpe = (expectancy / std_dev) if std_dev else 0.0

        return PerformanceSnapshot(
            trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=(len(wins) / len(trades) * 100) if trades else 0.0,
            gross_profit=round(gross_profit, 4),
            gross_loss=round(gross_loss, 4),
            net_profit=round(net_profit, 4),
            profit_factor=round(gross_profit / gross_loss, 4) if gross_loss else 0.0,
            max_drawdown=round(max_drawdown, 4),
            expectancy=round(expectancy, 4),
            sharpe_ratio=round(sharpe, 4),
            equity_curve=[round(value, 4) for value in equity_curve],
        )

    @staticmethod
    def _max_drawdown(equity_curve: list[float]) -> float:
        peak = equity_curve[0] if equity_curve else 0.0
        max_dd = 0.0
        for value in equity_curve:
            peak = max(peak, value)
            max_dd = max(max_dd, peak - value)
        return max_dd
