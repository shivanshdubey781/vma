from __future__ import annotations

from collections import deque
from statistics import mean

from backend.models import Candle, IndicatorSnapshot, MarketRegime, StrategyParameters


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


class VMAEngine:
    """Candle-close VMA engine tuned for 5-minute paper-trading simulation."""

    def __init__(self, params: StrategyParameters | None = None) -> None:
        self.params = params or StrategyParameters()

    def compute(self, rows: list[dict[str, float | str]]) -> list[IndicatorSnapshot]:
        candles = [Candle(**row) for row in rows]
        if not candles:
            return []

        fast_vma_series = self._compute_vma_series(candles, self.params.fast_length)
        slow_vma_series = self._compute_vma_series(candles, self.params.slow_length)
        atr_series = self._compute_atr(candles, self.params.atr_length)
        rsi_series = self._compute_rsi(candles, self.params.rsi_length)
        vwap_series = self._compute_vwap(candles)
        adx_series = self._compute_adx(candles, self.params.atr_length)
        atr_mean_series = self._rolling_mean(atr_series, self.params.slow_length)
        band_width_window: deque[float] = deque(maxlen=self.params.slow_length)
        snapshots: list[IndicatorSnapshot] = []

        for index, candle in enumerate(candles):
            middle_band = fast_vma_series[index]
            upper_band = middle_band + atr_series[index] * self.params.band_multiplier
            lower_band = middle_band - atr_series[index] * self.params.band_multiplier
            band_width = _safe_div(upper_band - lower_band, middle_band)
            band_width_window.append(band_width)
            fast_slope = fast_vma_series[index] - (fast_vma_series[index - 1] if index else fast_vma_series[index])
            slow_slope = slow_vma_series[index] - (slow_vma_series[index - 1] if index else slow_vma_series[index])
            spread = fast_vma_series[index] - slow_vma_series[index]
            trend_strength = min(100.0, max(
                0.0,
                abs(spread) * 35
                + abs(fast_slope - slow_slope) * 25
                + max(0.0, adx_series[index] - self.params.adx_threshold) * 1.4,
            ))
            squeeze_on = band_width < self.params.squeeze_threshold
            breakout_up = candle.close > upper_band
            breakout_down = candle.close < lower_band
            volatility_expansion = atr_series[index] > atr_mean_series[index] * 1.15 if atr_mean_series[index] else False
            regime = self._classify_regime(
                adx=adx_series[index],
                atr=atr_series[index],
                band_width=band_width,
                slope=fast_slope,
                trend_strength=trend_strength,
                volatility_expansion=volatility_expansion,
            )

            snapshots.append(
                IndicatorSnapshot(
                    timestamp=candle.timestamp,
                    close=round(candle.close, 4),
                    fast_vma=round(fast_vma_series[index], 4),
                    slow_vma=round(slow_vma_series[index], 4),
                    fast_slope=round(fast_slope, 4),
                    slow_slope=round(slow_slope, 4),
                    middle_band=round(middle_band, 4),
                    upper_band=round(upper_band, 4),
                    lower_band=round(lower_band, 4),
                    atr=round(atr_series[index], 4),
                    rsi=round(rsi_series[index], 4),
                    vwap=round(vwap_series[index], 4),
                    adx=round(adx_series[index], 4),
                    trend_strength=round(trend_strength, 4),
                    band_width=round(band_width, 6),
                    squeeze_on=squeeze_on,
                    breakout_up=breakout_up,
                    breakout_down=breakout_down,
                    volatility_expansion=volatility_expansion,
                    regime=regime,
                )
            )

        return snapshots

    def _compute_vma_series(self, candles: list[Candle], length: int) -> list[float]:
        if length <= 0:
            raise ValueError("length must be greater than 0")

        k = 1.0 / length
        pdm_s = mdm_s = pdi_s = mdi_s = i_s_val = 0.0
        vma = None
        i_s_window: list[float] = []
        series: list[float] = []

        for index, candle in enumerate(candles):
            src = candle.close
            prev = candles[index - 1].close if index > 0 else src

            pdm = max(src - prev, 0.0)
            mdm = max(prev - src, 0.0)

            pdm_s = (1 - k) * pdm_s + k * pdm
            mdm_s = (1 - k) * mdm_s + k * mdm

            direction_sum = pdm_s + mdm_s
            pdi = _safe_div(pdm_s, direction_sum)
            mdi = _safe_div(mdm_s, direction_sum)

            pdi_s = (1 - k) * pdi_s + k * pdi
            mdi_s = (1 - k) * mdi_s + k * mdi

            d_value = abs(pdi_s - mdi_s)
            s1 = pdi_s + mdi_s
            ratio = _safe_div(d_value, s1)
            i_s_val = (1 - k) * i_s_val + k * ratio
            i_s_window.append(i_s_val)

            window = i_s_window[max(0, index - length + 1): index + 1]
            highest_i_s = max(window)
            lowest_i_s = min(window)
            v_i = _safe_div(i_s_val - lowest_i_s, highest_i_s - lowest_i_s)

            if vma is None:
                vma = src
            else:
                vma = (1 - k * v_i) * vma + k * v_i * src

            series.append(vma)

        return series

    @staticmethod
    def _compute_atr(candles: list[Candle], length: int) -> list[float]:
        values: list[float] = []
        tr_values: list[float] = []
        atr = 0.0
        alpha = 1.0 / max(length, 1)

        for index, candle in enumerate(candles):
            prev_close = candles[index - 1].close if index > 0 else candle.close
            tr = max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
            tr_values.append(tr)
            atr = mean(tr_values[:length]) if index < length else (1 - alpha) * atr + alpha * tr
            values.append(atr)
        return values

    @staticmethod
    def _compute_rsi(candles: list[Candle], length: int) -> list[float]:
        gains: list[float] = []
        losses: list[float] = []
        avg_gain = avg_loss = 0.0
        series: list[float] = []

        for index, candle in enumerate(candles):
            prev_close = candles[index - 1].close if index > 0 else candle.close
            delta = candle.close - prev_close
            gain = max(delta, 0.0)
            loss = max(-delta, 0.0)
            gains.append(gain)
            losses.append(loss)

            if index < length:
                avg_gain = mean(gains)
                avg_loss = mean(losses)
            else:
                avg_gain = ((avg_gain * (length - 1)) + gain) / length
                avg_loss = ((avg_loss * (length - 1)) + loss) / length

            rs = _safe_div(avg_gain, avg_loss)
            series.append(100 - (100 / (1 + rs)) if avg_loss else 100.0)
        return series

    @staticmethod
    def _compute_vwap(candles: list[Candle]) -> list[float]:
        cumulative_pv = 0.0
        cumulative_volume = 0.0
        series: list[float] = []
        for candle in candles:
            typical_price = (candle.high + candle.low + candle.close) / 3
            volume = candle.volume or 1.0
            cumulative_pv += typical_price * volume
            cumulative_volume += volume
            series.append(_safe_div(cumulative_pv, cumulative_volume))
        return series

    @staticmethod
    def _compute_adx(candles: list[Candle], length: int) -> list[float]:
        if not candles:
            return []

        tr_ema = plus_dm_ema = minus_dm_ema = dx_ema = 0.0
        alpha = 1.0 / max(length, 1)
        series: list[float] = []

        for index, candle in enumerate(candles):
            prev = candles[index - 1] if index > 0 else candle
            up_move = candle.high - prev.high
            down_move = prev.low - candle.low
            plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
            minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
            tr = max(
                candle.high - candle.low,
                abs(candle.high - prev.close),
                abs(candle.low - prev.close),
            )

            tr_ema = (1 - alpha) * tr_ema + alpha * tr
            plus_dm_ema = (1 - alpha) * plus_dm_ema + alpha * plus_dm
            minus_dm_ema = (1 - alpha) * minus_dm_ema + alpha * minus_dm

            plus_di = 100 * _safe_div(plus_dm_ema, tr_ema)
            minus_di = 100 * _safe_div(minus_dm_ema, tr_ema)
            dx = 100 * _safe_div(abs(plus_di - minus_di), plus_di + minus_di)
            dx_ema = (1 - alpha) * dx_ema + alpha * dx
            series.append(dx_ema)

        return series

    @staticmethod
    def _rolling_mean(values: list[float], length: int) -> list[float]:
        window: deque[float] = deque(maxlen=length)
        output: list[float] = []
        for value in values:
            window.append(value)
            output.append(mean(window) if window else 0.0)
        return output

    @staticmethod
    def _classify_regime(
        *,
        adx: float,
        atr: float,
        band_width: float,
        slope: float,
        trend_strength: float,
        volatility_expansion: bool,
    ) -> MarketRegime:
        if adx >= 22 and abs(slope) > 0.05 and trend_strength >= 30:
            return MarketRegime.TRENDING
        if band_width <= 0.012 and not volatility_expansion and abs(slope) < 0.05 and trend_strength < 25:
            return MarketRegime.SIDEWAYS
        return MarketRegime.VOLATILE
