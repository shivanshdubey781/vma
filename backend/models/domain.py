from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    SIDEWAYS = "SIDEWAYS"
    VOLATILE = "VOLATILE"


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class PositionSide(str, Enum):
    NO_POSITION = "NO_POSITION"
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(slots=True)
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(slots=True)
class StrategyParameters:
    fast_length: int = 9
    slow_length: int = 21
    atr_length: int = 14
    rsi_length: int = 14
    band_multiplier: float = 1.5
    adx_threshold: float = 20.0
    squeeze_threshold: float = 0.012
    breakout_lookback: int = 10
    cooldown_bars: int = 2
    entry_delay_bars: int = 1
    min_trend_strength: float = 28.0
    min_rsi_long: float = 55.0
    max_rsi_short: float = 45.0


@dataclass(slots=True)
class IndicatorSnapshot:
    timestamp: str
    close: float
    fast_vma: float
    slow_vma: float
    fast_slope: float
    slow_slope: float
    middle_band: float
    upper_band: float
    lower_band: float
    atr: float
    rsi: float
    vwap: float
    adx: float
    trend_strength: float
    band_width: float
    squeeze_on: bool
    breakout_up: bool
    breakout_down: bool
    volatility_expansion: bool
    regime: MarketRegime
    candle_close_confirmed: bool = True


@dataclass(slots=True)
class SignalDecision:
    timestamp: str
    signal: SignalType
    reason: str
    price: float
    regime: MarketRegime
    confidence: float
    execute_on_next_open: bool = True
    cooldown_active: bool = False
    duplicate_blocked: bool = False


@dataclass(slots=True)
class RiskParameters:
    capital: float = 500000.0
    risk_per_trade: float = 0.01
    atr_stop_multiple: float = 1.5
    trailing_atr_multiple: float = 1.0
    risk_reward_ratio: float = 2.0
    fixed_stop_points: float = 0.0
    fixed_target_points: float = 0.0
    trail_trigger_points: float = 0.0
    trail_lock_points: float = 0.0
    lot_size: int = 25
    max_daily_loss_pct: float = 0.03
    max_trades_per_day: int = 6
    cooldown_after_losses: int = 2
    max_drawdown_pct: float = 0.1
    brokerage_per_order: float = 20.0
    slippage_bps: float = 2.0


@dataclass(slots=True)
class PositionSnapshot:
    symbol: str
    side: PositionSide = PositionSide.NO_POSITION
    quantity: int = 0
    entry_time: str | None = None
    entry_price: float = 0.0
    current_stop: float = 0.0
    target_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    bars_held: int = 0
    last_update_time: str | None = None
    entry_index: int = -1


@dataclass(slots=True)
class SimulatedTrade:
    trade_id: str
    symbol: str
    side: PositionSide
    entry_time: str
    entry_price: float
    quantity: int
    stop_loss: float
    target: float
    entry_index: int
    exit_time: str | None = None
    exit_price: float | None = None
    exit_index: int | None = None
    pnl: float = 0.0
    pnl_after_costs: float = 0.0
    brokerage: float = 0.0
    slippage: float = 0.0
    duration_bars: int = 0
    status: str = "OPEN"
    meta: dict[str, float | str | bool] = field(default_factory=dict)


@dataclass(slots=True)
class PerformanceSnapshot:
    trades: int
    wins: int
    losses: int
    win_rate: float
    loss_rate: float
    gross_profit: float
    gross_loss: float
    net_profit: float
    profit_factor: float
    max_drawdown: float
    expectancy: float
    sharpe_ratio: float
    equity_curve: list[float]


@dataclass(slots=True)
class AlertEvent:
    created_at: datetime
    level: str
    title: str
    message: str
    payload: dict[str, str | float]
