# 5-Minute VMA Paper Trading Architecture

## Folder structure

```text
backend/
├── api/
├── backtesting/
├── brokers/
├── db/
├── indicators/
├── models/
├── risk/
├── services/
├── simulation/
├── strategy/
├── utils/
├── websocket/
├── config.py
└── main.py
```

## System goal

This backend is intentionally focused on:

- 5-minute OHLC candle simulation only
- candle-close confirmed signals
- adaptive dual-VMA logic
- ATR-based upper/lower bands
- paper trading and backtesting
- no live order placement

## Core strategy stack

### 1. Dual VMA engine

- Fast VMA length: `9`
- Slow VMA length: `21`
- Base formula:

`VMA = (1 - k * vI) * VMA_prev + k * vI * close`

Where:

- `k = 1 / length`
- `vI` is the normalized adaptive directional volatility index

Outputs:

- fast VMA
- slow VMA
- fast slope
- slow slope
- trend strength

### 2. Adaptive volatility bands

- `MiddleBand = FastVMA`
- `UpperBand = FastVMA + ATR(14) * 1.5`
- `LowerBand = FastVMA - ATR(14) * 1.5`

Used for:

- breakout confirmation
- squeeze detection
- volatility expansion detection

### 3. Market regime filter

The engine classifies each closed 5-minute bar into:

- `TRENDING`
- `SIDEWAYS`
- `VOLATILE`

Inputs:

- ADX
- band width contraction
- ATR expansion/compression
- VMA slope
- trend strength score

### 4. Signal engine

Long setup:

- fast VMA crosses above slow VMA
- close above upper band
- RSI > 55
- close above VWAP
- market regime = `TRENDING`
- signal only after candle close

Short setup:

- fast VMA crosses below slow VMA
- close below lower band
- RSI < 45
- market regime != `SIDEWAYS`
- signal only after candle close

Trade hygiene:

- duplicate suppression
- cooldown bars
- no intrabar repainting

## Simulation design

The simulator replays 5-minute candles sequentially and uses next-candle open execution.

Execution rules:

- signal is produced on candle `n` close
- paper entry executes on candle `n+1` open
- slippage is applied
- brokerage is charged
- only one active position at a time

Position states:

- `NO_POSITION`
- `LONG`
- `SHORT`

Position controls:

- ATR-based initial stop
- ATR trailing stop
- risk-reward target
- reversal exit
- end-of-data closeout

## Risk management

The risk engine enforces:

- capital risk percentage per trade
- dynamic position sizing
- ATR stop sizing
- max daily loss
- max trades per day
- consecutive loss cooldown
- drawdown protection

## Backtesting framework

Backtest metrics:

- trades
- wins / losses
- win rate / loss rate
- gross profit / gross loss
- net profit
- profit factor
- sharpe ratio
- expectancy
- max drawdown
- equity curve

Execution realism:

- next candle open fill
- brokerage
- slippage
- stop / target / trailing stop evaluation on candle ranges

## MongoDB schema

Collections used:

- `OHLC5`
- `signals`
- `paper_trades`
- `positions`
- `performance`
- `logs`

Recommended indexes:

- `OHLC5`: `(symbol, timestamp desc)`
- `signals`: `(symbol, timestamp desc)`
- `paper_trades`: `(symbol, entry_time desc)`
- `positions`: `(symbol, status)`
- `performance`: `(symbol, generated_at desc)`

## API design

- `GET /health`
- `GET /api/status`
- `GET /api/v1/analyze/5min`
- `GET /api/v1/simulate/5min`
- `GET /api/v1/backtest/5min`
- `POST /api/v1/admin/bootstrap-indexes`
- `WS /ws/market`

## Dashboard architecture

Recommended frontend panels:

- 5-minute candle chart
- fast and slow VMA
- upper/lower ATR bands
- regime state
- last signal marker
- active paper position
- realized/unrealized PnL
- trade history
- equity curve
- drawdown and win-rate cards

## Performance optimizations

- signal generation is candle-close only
- rolling windows use `deque`
- indicator state is designed for incremental extension
- no live broker calls block the strategy path
- analysis, simulation, and backtest are separated from transport

## Future scaling roadmap

- move simulation runs to worker queues
- snapshot indicator state per symbol/day
- add multi-symbol orchestration for Nifty and BankNifty
- stream dashboard updates through websockets
- plug in broker adapters later without changing strategy internals
