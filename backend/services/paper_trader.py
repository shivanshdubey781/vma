from __future__ import annotations

from uuid import uuid4

from backend.config import settings
from backend.models import IndicatorSnapshot, SignalDecision, SignalType, SimulatedTrade


class PaperTradingEngine:
    def __init__(self) -> None:
        self.brokerage_per_order = settings.trading.brokerage_per_order
        self.slippage_bps = settings.trading.slippage_bps

    def open_trade(
        self,
        *,
        symbol: str,
        signal: SignalDecision,
        order_plan: dict[str, float | int | str],
    ) -> SimulatedTrade:
        entry = float(order_plan["entry_price"])
        quantity = int(order_plan["quantity"])
        slippage = entry * (self.slippage_bps / 10_000)
        executed_entry = entry + slippage if signal.signal == SignalType.BUY else entry - slippage

        return SimulatedTrade(
            trade_id=str(uuid4()),
            symbol=symbol,
            side=signal.signal,
            entry_time=signal.timestamp,
            entry_price=round(executed_entry, 4),
            quantity=quantity,
            stop_loss=float(order_plan["stop_loss"]),
            target=float(order_plan["target"]),
            brokerage=self.brokerage_per_order,
            slippage=round(slippage * quantity, 4),
            meta={"reason": signal.reason, "confidence": signal.confidence},
        )

    def close_trade(
        self,
        trade: SimulatedTrade,
        *,
        timestamp: str,
        exit_price: float,
        exit_reason: str,
    ) -> SimulatedTrade:
        slippage = exit_price * (self.slippage_bps / 10_000)
        executed_exit = exit_price - slippage if trade.side == SignalType.BUY else exit_price + slippage
        gross_pnl = (
            (executed_exit - trade.entry_price) * trade.quantity
            if trade.side == SignalType.BUY
            else (trade.entry_price - executed_exit) * trade.quantity
        )
        total_cost = trade.brokerage + self.brokerage_per_order + trade.slippage + (slippage * trade.quantity)
        trade.exit_time = timestamp
        trade.exit_price = round(executed_exit, 4)
        trade.pnl = round(gross_pnl, 4)
        trade.pnl_after_costs = round(gross_pnl - total_cost, 4)
        trade.brokerage = round(trade.brokerage + self.brokerage_per_order, 4)
        trade.slippage = round(trade.slippage + (slippage * trade.quantity), 4)
        trade.status = exit_reason
        trade.meta["exit_reason"] = exit_reason
        return trade
