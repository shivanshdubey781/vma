from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse

from backend.config import settings
from backend.db.mongo import MongoRepository
from backend.models import RiskParameters, StrategyParameters
from backend.services.strategy_service import StrategyService
from backend.websocket.market import MarketStreamGateway


BASE_DIR = Path(__file__).resolve().parents[2]

app = FastAPI(
    title="Institutional VMA Algo Engine",
    version="1.0.0",
    description="Production-style intraday VMA strategy platform for analysis, backtesting, and paper trading.",
)

def get_repository() -> MongoRepository:
    return MongoRepository()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.env}


@app.get("/api/status")
def api_status() -> dict[str, str]:
    return {"status": "ok", "env": settings.env, "service": "vma-simulation-engine"}


@app.get("/api/vma")
def legacy_vma(
    tf: str = "5min",
    length: int = 9,
    limit: int = Query(default=750, le=5000),
) -> dict[str, object]:
    if tf != "5min":
        tf = "5min"

    try:
        rows = get_repository().fetch_ohlc5(limit=limit)
        params = StrategyParameters(fast_length=length)
        service = StrategyService(params=params, repository=get_repository())
        analyzed = service.analyze(rows)
        snapshots = analyzed["snapshots"]
        if not snapshots:
            return {
                "ok": True,
                "timeframe": tf,
                "length": length,
                "total_bars": 0,
                "current": {},
                "history": [],
            }

        last = snapshots[-1]
        prev = snapshots[-2] if len(snapshots) > 1 else snapshots[-1]
        history = []
        for row in snapshots[-50:]:
            trend = "UP" if row["fast_slope"] > 0 else "DOWN" if row["fast_slope"] < 0 else "FLAT"
            history.append(
                {
                    "timestamp": row["timestamp"],
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": row["close"],
                    "vma": row["fast_vma"],
                    "trend": trend,
                }
            )

        return {
            "ok": True,
            "timeframe": tf,
            "length": length,
            "total_bars": len(snapshots),
            "current": {
                "timestamp": last["timestamp"],
                "close": last["close"],
                "vma": last["fast_vma"],
                "prev_vma": prev["fast_vma"],
                "delta": round(last["fast_vma"] - prev["fast_vma"], 4),
                "trend": "UP" if last["fast_slope"] > 0 else "DOWN" if last["fast_slope"] < 0 else "FLAT",
            },
            "history": history,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/analyze/5min")
def analyze(
    symbol: str = settings.trading.symbol,
    fast_length: int = 9,
    slow_length: int = 21,
    atr_length: int = 14,
    rsi_length: int = 14,
    band_multiplier: float = 1.5,
    limit: int = Query(default=750, le=5000),
) -> dict[str, object]:
    try:
        rows = get_repository().fetch_ohlc5(limit=limit)
        params = StrategyParameters(
            fast_length=fast_length,
            slow_length=slow_length,
            atr_length=atr_length,
            rsi_length=rsi_length,
            band_multiplier=band_multiplier,
        )
        service = StrategyService(params=params, repository=get_repository())
        result = service.analyze(rows)
        result["symbol"] = symbol
        result["timeframe"] = "5min"
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/simulate/5min")
def simulate(
    symbol: str = settings.trading.symbol,
    fast_length: int = 9,
    slow_length: int = 21,
    atr_length: int = 14,
    rsi_length: int = 14,
    band_multiplier: float = 1.5,
    limit: int = Query(default=750, le=5000),
    persist: bool = False,
) -> dict[str, object]:
    try:
        repository = get_repository()
        rows = repository.fetch_ohlc5(limit=limit)
        params = StrategyParameters(
            fast_length=fast_length,
            slow_length=slow_length,
            atr_length=atr_length,
            rsi_length=rsi_length,
            band_multiplier=band_multiplier,
        )
        risk = RiskParameters(
            capital=settings.trading.capital,
            risk_per_trade=settings.trading.risk_per_trade,
            max_daily_loss_pct=settings.trading.max_daily_loss,
            max_trades_per_day=settings.trading.max_trades_per_day,
            cooldown_after_losses=settings.trading.cooldown_after_losses,
            brokerage_per_order=settings.trading.brokerage_per_order,
            slippage_bps=settings.trading.slippage_bps,
        )
        service = StrategyService(params=params, risk_params=risk, repository=repository)
        return service.simulate(symbol=symbol, rows=rows, persist=persist)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/backtest/5min")
def backtest(
    symbol: str = settings.trading.symbol,
    limit: int = Query(default=1500, le=10000),
    persist: bool = False,
) -> dict[str, object]:
    try:
        repository = get_repository()
        rows = repository.fetch_ohlc5(limit=limit)
        risk = RiskParameters(
            capital=settings.trading.capital,
            risk_per_trade=settings.trading.risk_per_trade,
            max_daily_loss_pct=settings.trading.max_daily_loss,
            max_trades_per_day=settings.trading.max_trades_per_day,
            cooldown_after_losses=settings.trading.cooldown_after_losses,
            brokerage_per_order=settings.trading.brokerage_per_order,
            slippage_bps=settings.trading.slippage_bps,
        )
        service = StrategyService(
            params=StrategyParameters(),
            risk_params=risk,
            repository=repository,
        )
        return service.backtest(symbol=symbol, rows=rows, persist=persist)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/v1/admin/bootstrap-indexes")
def bootstrap_indexes() -> dict[str, str]:
    repository = get_repository()
    repository.ensure_indexes()
    return {"status": "ok", "message": "MongoDB indexes created"}


@app.get("/api/v1/config")
def config_overview() -> dict[str, object]:
    return asdict(settings)


@app.websocket("/ws/market")
async def market_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_json(MarketStreamGateway().connect_message())
    await websocket.close()
