from __future__ import annotations

from backend.models import IndicatorSnapshot, MarketRegime


class MarketRegimeFilter:
    def allow_longs(self, snapshot: IndicatorSnapshot) -> bool:
        return snapshot.regime == MarketRegime.TRENDING

    def allow_shorts(self, snapshot: IndicatorSnapshot) -> bool:
        return snapshot.regime != MarketRegime.SIDEWAYS

    def regime_summary(self, snapshot: IndicatorSnapshot) -> dict[str, object]:
        return {
            "regime": snapshot.regime,
            "trend_strength": snapshot.trend_strength,
            "adx": snapshot.adx,
            "band_width": snapshot.band_width,
            "squeeze_on": snapshot.squeeze_on,
            "volatility_expansion": snapshot.volatility_expansion,
        }
