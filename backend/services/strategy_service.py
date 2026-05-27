from __future__ import annotations

from dataclasses import asdict

from backend.backtesting.engine import BacktestAnalytics
from backend.db.mongo import MongoRepository
from backend.indicators.vma import VMAEngine
from backend.models import Candle, RiskParameters, StrategyParameters
from backend.risk.engine import RiskEngine
from backend.simulation.engine import PaperTradingSimulator
from backend.strategy.signals import SignalEngine


class StrategyService:
    def __init__(
        self,
        params: StrategyParameters | None = None,
        risk_params: RiskParameters | None = None,
        repository: MongoRepository | None = None,
    ) -> None:
        self.params = params or StrategyParameters()
        self.risk_params = risk_params or RiskParameters()
        self.repository = repository
        self.vma_engine = VMAEngine(self.params)
        self.signal_engine = SignalEngine(self.params)
        self.risk_engine = RiskEngine(self.risk_params)
        self.simulator = PaperTradingSimulator(self.risk_engine)
        self.analytics = BacktestAnalytics()

    def analyze(self, rows: list[dict[str, float | str]]) -> dict[str, object]:
        snapshots = self.vma_engine.compute(rows)
        signals = self.signal_engine.generate(snapshots)
        return {
            "snapshots": [asdict(snapshot) for snapshot in snapshots],
            "signals": [asdict(signal) for signal in signals],
            "latest": {
                "snapshot": asdict(snapshots[-1]) if snapshots else None,
                "signal": asdict(signals[-1]) if signals else None,
            },
        }

    def simulate(self, *, symbol: str, rows: list[dict[str, float | str]], persist: bool = False) -> dict[str, object]:
        candles = [Candle(**row) for row in rows]
        snapshots = self.vma_engine.compute(rows)
        signals = self.signal_engine.generate(snapshots)
        simulation = self.simulator.run(
            symbol=symbol,
            candles=candles,
            snapshots=snapshots,
            signals=signals,
        )
        performance = self.analytics.build(simulation["trades"], simulation["equity_curve"])
        result = {
            "symbol": symbol,
            "timeframe": "5min",
            "parameters": asdict(self.params),
            "risk": asdict(self.risk_params),
            "performance": asdict(performance),
            "trades": simulation["trades"],
            "signals": simulation["signals"],
            "final_position": simulation["final_position"],
            "latest_snapshot": asdict(snapshots[-1]) if snapshots else None,
        }
        if persist and self.repository is not None:
            self.repository.insert_many("signals", simulation["signals"])
            self.repository.insert_many("paper_trades", simulation["trades"])
            self.repository.replace_position(symbol, simulation["final_position"])
            self.repository.insert_one("performance", {"symbol": symbol, "generated_at": candles[-1].timestamp if candles else None, **asdict(performance)})
        return result

    def backtest(self, *, symbol: str, rows: list[dict[str, float | str]], persist: bool = False) -> dict[str, object]:
        return self.simulate(symbol=symbol, rows=rows, persist=persist)
