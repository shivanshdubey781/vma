from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass(slots=True)
class MongoSettings:
    uri: str = os.getenv("MONGO_URI", "")
    database: str = os.getenv("MONGO_DB", "trading_bot_db")
    timeframe_collections: dict[str, str] = field(
        default_factory=lambda: {
            "1min": "OHLC",
            "3min": "OHLC3",
            "5min": "OHLC5",
        }
    )
    signals_collection: str = "signals"
    trades_collection: str = "paper_trades"
    positions_collection: str = "positions"
    performance_collection: str = "performance"
    logs_collection: str = "logs"
    alerts_collection: str = "alerts"


@dataclass(slots=True)
class TradingSettings:
    symbol: str = os.getenv("DEFAULT_SYMBOL", "NIFTY")
    capital: float = float(os.getenv("SIM_CAPITAL", "500000"))
    risk_per_trade: float = float(os.getenv("RISK_PER_TRADE", "0.01"))
    max_daily_loss: float = float(os.getenv("MAX_DAILY_LOSS", "0.03"))
    max_trades_per_day: int = int(os.getenv("MAX_TRADES_PER_DAY", "6"))
    cooldown_after_losses: int = int(os.getenv("COOLDOWN_AFTER_LOSSES", "2"))
    brokerage_per_order: float = float(os.getenv("BROKERAGE_PER_ORDER", "20"))
    slippage_bps: float = float(os.getenv("SLIPPAGE_BPS", "2"))
    default_timeframe: str = "5min"
    default_lot_size: int = int(os.getenv("DEFAULT_LOT_SIZE", "25"))


@dataclass(slots=True)
class ApiSettings:
    host: str = os.getenv("API_HOST", "0.0.0.0")
    port: int = int(os.getenv("API_PORT", "8012"))
    reload: bool = os.getenv("API_RELOAD", "false").lower() == "true"


@dataclass(slots=True)
class Settings:
    env: str = os.getenv("ENV", "dev")
    mongo: MongoSettings = field(default_factory=MongoSettings)
    trading: TradingSettings = field(default_factory=TradingSettings)
    api: ApiSettings = field(default_factory=ApiSettings)


settings = Settings()
