from __future__ import annotations

from backend.models import IndicatorSnapshot, SignalDecision, SignalType, StrategyParameters
from backend.strategy.regime import MarketRegimeFilter


class SignalEngine:
    def __init__(self, params: StrategyParameters | None = None) -> None:
        self.params = params or StrategyParameters()
        self.regime_filter = MarketRegimeFilter()

    def generate(self, snapshots: list[IndicatorSnapshot]) -> list[SignalDecision]:
        signals: list[SignalDecision] = []
        last_signal_index = -10_000
        last_direction = SignalType.HOLD

        for index, current in enumerate(snapshots):
            previous = snapshots[index - 1] if index > 0 else current
            cooldown_active = index - last_signal_index <= self.params.cooldown_bars

            cross_up = current.fast_vma > current.slow_vma and previous.fast_vma <= previous.slow_vma
            cross_down = current.fast_vma < current.slow_vma and previous.fast_vma >= previous.slow_vma
            above_bias = (
                current.close > current.upper_band
                and current.close > current.vwap
                and current.rsi >= self.params.min_rsi_long
            )
            below_bias = current.close < current.lower_band and current.rsi <= self.params.max_rsi_short
            breakout_up = current.breakout_up and current.volatility_expansion
            breakout_down = current.breakout_down and current.volatility_expansion

            signal = SignalType.HOLD
            reason = "No setup"
            confidence = min(100.0, current.trend_strength)
            duplicate_blocked = False

            if (
                not cooldown_active
                and self.regime_filter.allow_longs(current)
                and current.candle_close_confirmed
                and current.trend_strength >= self.params.min_trend_strength
                and cross_up
                and above_bias
                and breakout_up
            ):
                signal = SignalType.BUY
                reason = "Bullish fast/slow VMA crossover confirmed on candle close above upper band"
            elif (
                not cooldown_active
                and self.regime_filter.allow_shorts(current)
                and current.candle_close_confirmed
                and current.trend_strength >= self.params.min_trend_strength
                and cross_down
                and below_bias
                and breakout_down
            ):
                signal = SignalType.SELL
                reason = "Bearish fast/slow VMA crossover confirmed on candle close below lower band"
            elif current.regime.value == "SIDEWAYS":
                reason = "Suppressed in sideways regime"
            elif cooldown_active:
                reason = "Cooldown active"
            elif current.squeeze_on:
                reason = "Suppressed during squeeze"

            if signal != SignalType.HOLD and signal == last_direction:
                duplicate_blocked = True
                signal = SignalType.HOLD
                reason = "Duplicate direction suppressed"

            if signal != SignalType.HOLD:
                last_signal_index = index
                last_direction = signal

            signals.append(
                SignalDecision(
                    timestamp=current.timestamp,
                    signal=signal,
                    reason=reason,
                    price=current.close,
                    regime=current.regime,
                    confidence=round(confidence, 2),
                    execute_on_next_open=True,
                    cooldown_active=cooldown_active,
                    duplicate_blocked=duplicate_blocked,
                )
            )

        return signals
