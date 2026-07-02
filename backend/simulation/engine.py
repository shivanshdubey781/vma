from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from backend.models import (
    Candle,
    IndicatorSnapshot,
    PositionSide,
    PositionSnapshot,
    SignalDecision,
    SignalType,
    SimulatedTrade,
)
from backend.risk.engine import RiskEngine
from backend.utils.time import parse_timestamp, session_key



class PaperTradingSimulator:
    def __init__(self, risk_engine: RiskEngine | None = None) -> None:
        self.risk_engine = risk_engine or RiskEngine()

    def run(
        self,
        *,
        symbol: str,
        candles: list[Candle],
        snapshots: list[IndicatorSnapshot],
        signals: list[SignalDecision],
    ) -> dict[str, object]:
        trades: list[SimulatedTrade] = []
        signal_log: list[dict[str, object]] = []
        position = PositionSnapshot(symbol=symbol)
        pending_signal_index: int | None = None
        equity = self.risk_engine.params.capital
        peak_equity = equity
        day_pnl = 0.0
        trade_count_today = 0
        consecutive_losses = 0
        last_session = session_key(candles[0].timestamp) if candles else ""
        active_trade: SimulatedTrade | None = None
        equity_curve = [equity]

        for index, candle in enumerate(candles):
            current_session = session_key(candle.timestamp)
            if current_session != last_session:
                day_pnl = 0.0
                trade_count_today = 0
                last_session = current_session

            try:
                dt = parse_timestamp(candle.timestamp)
                hhmm = dt.hour * 100 + dt.minute
            except Exception:
                hhmm = 0


            if pending_signal_index is not None:
                entry_snapshot = snapshots[pending_signal_index]
                entry_signal = signals[pending_signal_index]
                allowed, reason = self.risk_engine.can_trade(
                    equity=equity,
                    day_pnl=day_pnl,
                    trade_count=trade_count_today,
                    consecutive_losses=consecutive_losses,
                    peak_equity=peak_equity,
                )
                if hhmm >= 1515:
                    allowed = False
                    reason = "Time cutoff (>= 15:15)"
                if allowed and position.side == PositionSide.NO_POSITION:
                    active_trade, position = self._open_position(
                        symbol=symbol,
                        candle=candle,
                        snapshot=entry_snapshot,
                        signal=entry_signal,
                        entry_index=index,
                    )
                    trade_count_today += 1
                else:
                    signal_log.append(
                        {"timestamp": candle.timestamp, "signal": entry_signal.signal.value, "status": "BLOCKED", "reason": reason}
                    )
                pending_signal_index = None

            if active_trade:
                active_trade, position, closed_trade = self._update_position(
                    active_trade=active_trade,
                    position=position,
                    candle=candle,
                    snapshot=snapshots[index],
                    candle_index=index,
                )
                if closed_trade:
                    trades.append(closed_trade)
                    equity += closed_trade.pnl_after_costs
                    peak_equity = max(peak_equity, equity)
                    equity_curve.append(round(equity, 4))
                    day_pnl += closed_trade.pnl_after_costs
                    consecutive_losses = consecutive_losses + 1 if closed_trade.pnl_after_costs < 0 else 0
                    active_trade = None

                # Force End-of-Day squareoff if active_trade is still open and time is >= 15:22 or it is the last candle of the day
                is_last_candle_of_day = False
                if index + 1 < len(candles):
                    next_session = session_key(candles[index + 1].timestamp)
                    if next_session != current_session:
                        is_last_candle_of_day = True
                else:
                    is_last_candle_of_day = True

                if active_trade and (hhmm >= 1522 or is_last_candle_of_day):
                    closed_trade = self._close_trade(
                        trade=active_trade,
                        position=position,
                        exit_price=candle.close,
                        exit_time=candle.timestamp,
                        exit_index=index,
                        exit_reason="EOD",
                    )
                    trades.append(closed_trade)
                    equity += closed_trade.pnl_after_costs
                    peak_equity = max(peak_equity, equity)
                    equity_curve.append(round(equity, 4))
                    day_pnl += closed_trade.pnl_after_costs
                    consecutive_losses = consecutive_losses + 1 if closed_trade.pnl_after_costs < 0 else 0
                    active_trade = None
                    position = PositionSnapshot(symbol=symbol)


            signal = signals[index]
            signal_log.append(
                {
                    "timestamp": signal.timestamp,
                    "signal": signal.signal.value,
                    "confidence": signal.confidence,
                    "reason": signal.reason,
                    "price": signal.price,
                }
            )
            if signal.signal == SignalType.HOLD:
                continue

            if position.side == PositionSide.NO_POSITION:
                if index + 1 < len(candles):
                    pending_signal_index = index
            elif (
                position.side == PositionSide.LONG and signal.signal == SignalType.SELL
            ) or (
                position.side == PositionSide.SHORT and signal.signal == SignalType.BUY
            ):
                closed_trade = self._close_trade(
                    trade=active_trade,
                    position=position,
                    exit_price=candle.close,
                    exit_time=candle.timestamp,
                    exit_index=index,
                    exit_reason="REVERSAL",
                )
                trades.append(closed_trade)
                equity += closed_trade.pnl_after_costs
                peak_equity = max(peak_equity, equity)
                equity_curve.append(round(equity, 4))
                day_pnl += closed_trade.pnl_after_costs
                consecutive_losses = consecutive_losses + 1 if closed_trade.pnl_after_costs < 0 else 0
                active_trade = None
                position = PositionSnapshot(symbol=symbol)
                if index + 1 < len(candles):
                    pending_signal_index = index

        if active_trade:
            final_candle = candles[-1]
            trades.append(
                self._close_trade(
                    trade=active_trade,
                    position=position,
                    exit_price=final_candle.close,
                    exit_time=final_candle.timestamp,
                    exit_index=len(candles) - 1,
                    exit_reason="EOD",
                )
            )
            equity += trades[-1].pnl_after_costs
            equity_curve.append(round(equity, 4))

        return {
            "trades": [asdict(trade) for trade in trades],
            "signals": signal_log,
            "final_position": asdict(position),
            "equity_curve": equity_curve,
        }

    def _open_position(
        self,
        *,
        symbol: str,
        candle: Candle,
        snapshot: IndicatorSnapshot,
        signal: SignalDecision,
        entry_index: int,
    ) -> tuple[SimulatedTrade, PositionSnapshot]:
        entry_price = self._apply_slippage(
            price=candle.open,
            side=PositionSide.LONG if signal.signal == SignalType.BUY else PositionSide.SHORT,
            is_entry=True,
        )
        plan = self.risk_engine.build_trade_plan(signal, snapshot, entry_price)
        trade = SimulatedTrade(
            trade_id=str(uuid4()),
            symbol=symbol,
            side=PositionSide(plan["side"]),
            entry_time=candle.timestamp,
            entry_price=entry_price,
            quantity=int(plan["quantity"]),
            stop_loss=float(plan["stop_loss"]),
            target=float(plan["target"]),
            entry_index=entry_index,
            brokerage=self.risk_engine.params.brokerage_per_order,
            slippage=round(abs(entry_price - candle.open) * int(plan["quantity"]), 4),
            meta={
                "entry_reason": signal.reason,
                "confidence": signal.confidence,
                "option_type": "CE" if signal.signal == SignalType.BUY else "PE",
                "trail_trigger": float(plan["trail_trigger"]),
                "trail_lock": float(plan["trailing_distance"]),
                "lot_size": int(plan["lot_size"]),
            },
        )
        position = PositionSnapshot(
            symbol=symbol,
            side=trade.side,
            quantity=trade.quantity,
            entry_time=trade.entry_time,
            entry_price=trade.entry_price,
            current_stop=trade.stop_loss,
            target_price=trade.target,
            entry_index=entry_index,
            last_update_time=candle.timestamp,
        )
        return trade, position

    def _update_position(
        self,
        *,
        active_trade: SimulatedTrade,
        position: PositionSnapshot,
        candle: Candle,
        snapshot: IndicatorSnapshot,
        candle_index: int,
    ) -> tuple[SimulatedTrade | None, PositionSnapshot, SimulatedTrade | None]:
        position.bars_held += 1
        position.last_update_time = candle.timestamp
        trailing_distance = snapshot.atr * self.risk_engine.params.trailing_atr_multiple
        trail_trigger = float(active_trade.meta.get("trail_trigger", trailing_distance))
        trail_lock = float(active_trade.meta.get("trail_lock", trailing_distance))

        if position.side == PositionSide.LONG:
            if candle.high - position.entry_price >= trail_trigger:
                position.current_stop = max(position.current_stop, candle.close - trail_lock)
            if candle.low <= position.current_stop:
                return None, PositionSnapshot(symbol=position.symbol), self._close_trade(
                    trade=active_trade,
                    position=position,
                    exit_price=position.current_stop,
                    exit_time=candle.timestamp,
                    exit_index=candle_index,
                    exit_reason="TRAILING_SL" if position.current_stop > active_trade.stop_loss else "SL",
                )
            if candle.high >= position.target_price:
                return None, PositionSnapshot(symbol=position.symbol), self._close_trade(
                    trade=active_trade,
                    position=position,
                    exit_price=position.target_price,
                    exit_time=candle.timestamp,
                    exit_index=candle_index,
                    exit_reason="TARGET",
                )
            position.unrealized_pnl = round((candle.close - position.entry_price) * position.quantity, 4)
        else:
            if position.entry_price - candle.low >= trail_trigger:
                position.current_stop = min(position.current_stop, candle.close + trail_lock)
            if candle.high >= position.current_stop:
                return None, PositionSnapshot(symbol=position.symbol), self._close_trade(
                    trade=active_trade,
                    position=position,
                    exit_price=position.current_stop,
                    exit_time=candle.timestamp,
                    exit_index=candle_index,
                    exit_reason="TRAILING_SL" if position.current_stop < active_trade.stop_loss else "SL",
                )
            if candle.low <= position.target_price:
                return None, PositionSnapshot(symbol=position.symbol), self._close_trade(
                    trade=active_trade,
                    position=position,
                    exit_price=position.target_price,
                    exit_time=candle.timestamp,
                    exit_index=candle_index,
                    exit_reason="TARGET",
                )
            position.unrealized_pnl = round((position.entry_price - candle.close) * position.quantity, 4)

        return active_trade, position, None

    def _close_trade(
        self,
        *,
        trade: SimulatedTrade | None,
        position: PositionSnapshot,
        exit_price: float,
        exit_time: str,
        exit_index: int,
        exit_reason: str,
    ) -> SimulatedTrade:
        if trade is None:
            raise ValueError("Trade must exist before closing")

        executed_exit = self._apply_slippage(
            price=exit_price,
            side=trade.side,
            is_entry=False,
        )
        gross_pnl = (
            (executed_exit - trade.entry_price) * trade.quantity
            if trade.side == PositionSide.LONG
            else (trade.entry_price - executed_exit) * trade.quantity
        )
        total_cost = (
            trade.brokerage
            + self.risk_engine.params.brokerage_per_order
            + trade.slippage
            + abs(executed_exit - exit_price) * trade.quantity
        )
        trade.exit_time = exit_time
        trade.exit_price = round(executed_exit, 4)
        trade.exit_index = exit_index
        trade.duration_bars = max(0, exit_index - trade.entry_index)
        trade.pnl = round(gross_pnl, 4)
        trade.pnl_after_costs = round(gross_pnl - total_cost, 4)
        trade.brokerage = round(trade.brokerage + self.risk_engine.params.brokerage_per_order, 4)
        trade.slippage = round(trade.slippage + abs(executed_exit - exit_price) * trade.quantity, 4)
        trade.status = exit_reason
        trade.meta["exit_reason"] = exit_reason
        return trade

    def _apply_slippage(self, *, price: float, side: PositionSide, is_entry: bool) -> float:
        move = price * (self.risk_engine.params.slippage_bps / 10_000)
        if side == PositionSide.LONG:
            return round(price + move, 4) if is_entry else round(price - move, 4)
        return round(price - move, 4) if is_entry else round(price + move, 4)
