"""
VMA (Variable Moving Average) - Standalone Calculator
LazyBear's VMA + Dual-VMA Crossover

Reads OHLC data from MongoDB and computes:
  - Short VMA  (e.g. length=5)
  - Long  VMA  (e.g. length=9)
  - Signal         : CE (bullish) / PE (bearish) / NONE
  - Confirm signal : previous bar's crossover (safer entry)
  - Quality score  : 0-5 (signal strength)
  - ATR, RSI, sideways flag

INSTALL (once):
  pip install pymongo python-dotenv

USAGE:
  python vma_calculator.py                           # interactive menu
  python vma_calculator.py --tf 3min                 # 3-min bars
  python vma_calculator.py --tf 3min --slen 5 --llen 9
  python vma_calculator.py --tf 1min --today         # today's IST bars only
  python vma_calculator.py --tf 5min --uri "mongodb+srv://..." --db mydb
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# CONFIG  -  MongoDB connection (hardcoded, no .env needed)
# ---------------------------------------------------------------------------
MONGO_URI = "mongodb+srv://Avneesh:Avneesh%4012345@cluster0.sqcsrv2.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME   = "trading_bot_db"

# Timeframe -> MongoDB collection name
COLLECTION_MAP = {
    "1min": "OHLC",
    "3min": "OHLC3",
    "5min": "OHLC5",
}

IST = timezone(timedelta(hours=5, minutes=30))

# Optional: load .env from the same folder as this script
try:
    from dotenv import load_dotenv
    _env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env):
        load_dotenv(_env)
        MONGO_URI = os.getenv("MONGO_URI", MONGO_URI)
        DB_NAME   = os.getenv("MONGO_DB",  DB_NAME)
except ImportError:
    pass


# ===========================================================================
#  DATA LAYER
# ===========================================================================

_TIME_FIELDS = ["timestamp", "datetime", "date", "time", "ts", "t", "open_time"]


def _pick_time_field(sample: dict) -> str:
    lower = {k.lower(): k for k in sample}
    for f in _TIME_FIELDS:
        if f in lower:
            return lower[f]
    return "timestamp"


def fetch_rows(timeframe: str, limit: int = 2000) -> list[dict]:
    """
    Fetch the latest `limit` OHLC bars from MongoDB.
    Returns list of dicts: {timestamp, open, high, low, close}
    Sorted oldest-first (chronological).
    """
    try:
        from pymongo import MongoClient, DESCENDING
    except ImportError:
        sys.exit("ERROR: pymongo not installed. Run: pip install pymongo")

    col_name = COLLECTION_MAP.get(timeframe)
    if not col_name:
        raise ValueError(
            f"Unknown timeframe '{timeframe}'. Choose from: {list(COLLECTION_MAP)}"
        )

    print(f"  Connecting: {MONGO_URI[:60]}")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    db     = client[DB_NAME]
    col    = db[col_name]

    sample = col.find_one()
    if not sample:
        raise RuntimeError(
            f"Collection '{col_name}' in DB '{DB_NAME}' is empty or does not exist."
        )

    ts_field = _pick_time_field(sample)
    docs = list(col.find({}, {"_id": 0}).sort([(ts_field, DESCENDING)]).limit(limit))
    docs.reverse()  # oldest first

    rows: list[dict] = []
    seen: set[str]   = set()

    for d in docs:
        norm  = {k.lower(): v for k, v in d.items()}
        ts    = ""
        for f in _TIME_FIELDS:
            if norm.get(f):
                ts = str(norm[f])
                break
        close = float(norm.get("close", 0) or 0)
        open_ = float(norm.get("open",  0) or 0)
        high  = float(norm.get("high",  0) or 0)
        low   = float(norm.get("low",   0) or 0)
        if not ts or ts in seen:
            continue
        seen.add(ts)
        rows.append({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close})

    client.close()
    return rows


# ===========================================================================
#  INDICATORS
# ===========================================================================

def _vma_series(rows: list[dict], length: int) -> list[float]:
    """
    LazyBear Variable Moving Average - exact TradingView match.
    Cold-start: vma[0] = close[0].  Uses 2000+ prior bars for warm-up.
    """
    if length <= 0:
        raise ValueError("length must be > 0")

    k                           = 1.0 / length
    pdmS = mdmS = pdiS = mdiS  = 0.0
    iS_val                      = 0.0
    vma                         = None
    iS_arr: list[float]         = []
    out:    list[float]         = []

    for i, r in enumerate(rows):
        src  = r["close"]
        prev = rows[i - 1]["close"] if i > 0 else src

        pdm   = max(src - prev, 0.0)
        mdm   = max(prev - src, 0.0)
        pdmS  = (1 - k) * pdmS + k * pdm
        mdmS  = (1 - k) * mdmS + k * mdm

        s     = pdmS + mdmS
        pdi   = (pdmS / s) if s else 0.0
        mdi   = (mdmS / s) if s else 0.0

        pdiS  = (1 - k) * pdiS + k * pdi
        mdiS  = (1 - k) * mdiS + k * mdi

        d     = abs(pdiS - mdiS)
        s1    = pdiS + mdiS
        ratio = (d / s1) if s1 else 0.0
        iS_val = (1 - k) * iS_val + k * ratio
        iS_arr.append(iS_val)

        win  = iS_arr[max(0, i - length + 1): i + 1]
        hhv  = max(win);  llv = min(win)
        rng  = hhv - llv
        vI   = ((iS_val - llv) / rng) if rng else 0.0

        vma  = src if vma is None else (1 - k * vI) * vma + k * vI * src
        out.append(round(vma, 4))

    return out


def _atr_series(rows: list[dict], period: int = 14) -> list[float]:
    """Wilder ATR - same as TradingView ta.atr()"""
    trs, atrs  = [], []
    prev_close = None
    atr        = 0.0
    for r in rows:
        hi, lo, cl = r["high"], r["low"], r["close"]
        if prev_close is None:
            tr = hi - lo
        else:
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        n = len(trs)
        if n < period:
            atr = sum(trs) / n
        elif n == period:
            atr = sum(trs) / period
        else:
            atr = (atr * (period - 1) + tr) / period
        atrs.append(round(atr, 4))
        prev_close = cl
    return atrs


def _rsi_series(rows: list[dict], period: int = 14) -> list[float]:
    """Wilder RSI - same as TradingView ta.rsi()"""
    closes             = [r["close"] for r in rows]
    gains, losses      = [], []
    rsis: list[float]  = []
    avg_gain = avg_loss = 0.0

    for i, cl in enumerate(closes):
        if i == 0:
            rsis.append(50.0)
            continue
        chg  = cl - closes[i - 1]
        gain = max(chg,  0.0)
        loss = max(-chg, 0.0)
        if i < period:
            gains.append(gain);   losses.append(loss)
            avg_gain = sum(gains) / len(gains)
            avg_loss = sum(losses) / len(losses)
        elif i == period:
            gains.append(gain);   losses.append(loss)
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_gain == 0.0 and avg_loss == 0.0:
            rsis.append(50.0)
        elif avg_loss == 0.0:
            rsis.append(100.0)
        else:
            rsis.append(round(100.0 - 100.0 / (1.0 + avg_gain / avg_loss), 2))

    return rsis


# ===========================================================================
#  DUAL-VMA CROSSOVER ENGINE
# ===========================================================================

def compute_dual_vma(
    rows:      list[dict],
    short_len: int = 5,
    long_len:  int = 9,
) -> list[dict]:
    """
    Compute Short-VMA and Long-VMA for every bar and tag crossover signals.

    Returns list of bar dicts:
        timestamp, open, high, low, close
        short_vma, long_vma
        signal         : CE | PE | NONE   (this bar's crossover)
        confirm_signal : CE | PE | NONE   (previous bar's signal - use for entry)
        quality        : 0-5              (signal strength)
        rsi, atr, is_sideways, position, svma_trend, short_slope, long_slope
    """
    if short_len >= long_len:
        raise ValueError("short_len must be < long_len")

    sv_arr  = _vma_series(rows, short_len)
    lv_arr  = _vma_series(rows, long_len)
    atr_arr = _atr_series(rows, 14)
    rsi_arr = _rsi_series(rows, 14)

    results: list[dict] = []

    for i, r in enumerate(rows):
        sv  = sv_arr[i]
        lv  = lv_arr[i]
        atr = atr_arr[i]
        rsi = rsi_arr[i]

        # --- Crossover detection ---
        if i == 0:
            signal = "NONE"
        else:
            prev_sv = sv_arr[i - 1]
            prev_lv = lv_arr[i - 1]
            if prev_sv <= prev_lv and sv > lv:
                signal = "CE"      # bullish crossover -> Buy CALL
            elif prev_sv >= prev_lv and sv < lv:
                signal = "PE"      # bearish crossover -> Buy PUT
            else:
                signal = "NONE"

        # --- Price-vs-VMA override (false-signal rejection) ---
        price_above = r["close"] > max(sv, lv)
        price_below = r["close"] < min(sv, lv)
        if signal == "PE" and price_above and rsi > 55:
            signal = "CE"
        elif signal == "CE" and price_below and rsi < 45:
            signal = "PE"

        # --- Confirm signal (previous bar's crossover - safer entry timing) ---
        confirm_signal = results[i - 1]["signal"] if i > 0 else "NONE"

        # --- Slopes (3-bar lookback) ---
        short_slope = round(sv - sv_arr[i - 3], 4) if i >= 3 else 0.0
        long_slope  = round(lv - lv_arr[i - 3], 4) if i >= 3 else 0.0

        # --- VMA spread & sideways flag ---
        vma_spread  = round(abs(sv - lv), 4)
        is_sideways = vma_spread < round(atr * 0.3, 4)

        # --- Short VMA trend ---
        if i == 0:
            svma_trend = "FLAT"
        elif sv > sv_arr[i - 1]:
            svma_trend = "UP"
        elif sv < sv_arr[i - 1]:
            svma_trend = "DOWN"
        else:
            svma_trend = "FLAT"

        # --- Relative position of short vs long ---
        position = "ABOVE" if sv > lv else ("BELOW" if sv < lv else "CROSS")

        # --- Quality score 0-5 ---
        quality = 0
        if confirm_signal != "NONE":
            quality += 1   # confirmed crossover
            if confirm_signal == "CE":
                if short_slope > 0: quality += 1   # short slope rising
                if long_slope  > 0: quality += 1   # long slope rising
                if rsi > 55:        quality += 1   # RSI confirms
            else:  # PE
                if short_slope < 0: quality += 1
                if long_slope  < 0: quality += 1
                if rsi < 45:        quality += 1
            if vma_spread >= round(atr * 0.5, 4):
                quality += 1   # lines separating well

        results.append({
            "timestamp":      r["timestamp"],
            "open":           round(r["open"],  4),
            "high":           round(r["high"],  4),
            "low":            round(r["low"],   4),
            "close":          round(r["close"], 4),
            "short_vma":      sv,
            "long_vma":       lv,
            "signal":         signal,
            "confirm_signal": confirm_signal,
            "svma_trend":     svma_trend,
            "position":       position,
            "atr":            atr,
            "rsi":            rsi,
            "is_sideways":    is_sideways,
            "short_slope":    short_slope,
            "long_slope":     long_slope,
            "quality":        quality,
        })

    return results


# ===========================================================================
#  TODAY FILTER  (matches TradingView - warm-up bars discarded from output)
# ===========================================================================

def filter_today(bars: list[dict]) -> list[dict]:
    """Return only bars whose timestamp falls on today (IST)."""
    today_str = datetime.now(IST).strftime("%Y-%m-%d")

    def is_today(ts) -> bool:
        if not ts:
            return False
        s = str(ts).strip()
        # Fast path: "YYYY-MM-DD ..." or "YYYY-MM-DDTHH:..."
        if s.startswith(today_str):
            return True
        # Slow path: parse full ISO string (handles UTC offsets)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(IST).strftime("%Y-%m-%d") == today_str
        except Exception:
            return False

    return [b for b in bars if is_today(b.get("timestamp", ""))]


# ===========================================================================
#  DISPLAY HELPERS
# ===========================================================================

def _sig(s: str) -> str:
    return {"CE": "[CE]", "PE": "[PE]", "NONE": " -- "}.get(s, s)


def print_table(bars: list[dict], show_last: int = 30) -> None:
    """Pretty-print the last N bars."""
    subset = bars[-show_last:]
    hdr = (
        f"{'Timestamp':<22} {'Close':>10} {'ShortVMA':>10} {'LongVMA':>10}"
        f" {'Signal':>7} {'Confirm':>7} {'Qual':>5} {'RSI':>6} {'ATR':>8} {'Side?':>6}"
    )
    sep = "-" * len(hdr)
    print("\n" + sep)
    print(hdr)
    print(sep)
    for b in subset:
        sw = "SDWY" if b["is_sideways"] else "    "
        print(
            f"{str(b['timestamp']):<22} {b['close']:>10.2f} {b['short_vma']:>10.4f}"
            f" {b['long_vma']:>10.4f} {_sig(b['signal']):>7} {_sig(b['confirm_signal']):>7}"
            f" {b['quality']:>5} {b['rsi']:>6.1f} {b['atr']:>8.2f} {sw:>6}"
        )
    print(sep)


def print_summary(bars: list[dict], tf: str, slen: int, llen: int) -> None:
    """Print summary stats and the current signal."""
    if not bars:
        print("No bars to summarise.")
        return

    last  = bars[-1]
    ce_ct = sum(1 for b in bars if b["signal"] == "CE")
    pe_ct = sum(1 for b in bars if b["signal"] == "PE")

    tradeable    = [b for b in bars if b["confirm_signal"] != "NONE" and b["quality"] >= 2]
    latest_trade = tradeable[-1] if tradeable else None

    div = "=" * 56
    print(f"\n{div}")
    print(f"  VMA  |  TF: {tf}  |  Short: {slen}  |  Long: {llen}")
    print(div)
    print(f"  Total bars shown  : {len(bars)}")
    print(f"  CE crossovers     : {ce_ct}")
    print(f"  PE crossovers     : {pe_ct}")
    print(f"{'-' * 56}")
    print(f"  CURRENT BAR  ->  {last['timestamp']}")
    print(f"    Close      : {last['close']:.2f}")
    print(f"    Short VMA  : {last['short_vma']:.4f}")
    print(f"    Long  VMA  : {last['long_vma']:.4f}")
    print(f"    Signal     : {_sig(last['signal'])}")
    print(f"    Confirm    : {_sig(last['confirm_signal'])}")
    print(f"    Quality    : {last['quality']} / 5")
    print(f"    RSI        : {last['rsi']:.2f}")
    print(f"    ATR        : {last['atr']:.2f}")
    print(f"    Position   : {last['position']}")
    print(f"    Sideways   : {'Yes - be careful' if last['is_sideways'] else 'No - trending'}")
    if latest_trade:
        print(f"{'-' * 56}")
        print(f"  LATEST TRADEABLE SIGNAL  ->  {latest_trade['timestamp']}")
        print(f"    Direction  : {_sig(latest_trade['confirm_signal'])}")
        print(f"    Quality    : {latest_trade['quality']} / 5")
        print(f"    Entry Close: {latest_trade['close']:.2f}")
    print(f"{div}\n")


# ===========================================================================
#  INTERACTIVE MENU
# ===========================================================================

def interactive_menu() -> argparse.Namespace:
    print("\n+--------------------------------------+")
    print("|     VMA Calculator - Setup           |")
    print("+--------------------------------------+")

    tf_choice = input("Timeframe  [1min / 3min / 5min]  (default: 3min): ").strip() or "3min"
    while tf_choice not in COLLECTION_MAP:
        tf_choice = input("  Invalid. Enter 1min, 3min, or 5min: ").strip() or "3min"

    slen_s = input("Short VMA length  (default: 5): ").strip()
    slen   = int(slen_s) if slen_s.isdigit() else 5

    llen_s = input("Long  VMA length  (default: 9): ").strip()
    llen   = int(llen_s) if llen_s.isdigit() else 9

    while slen >= llen:
        print(f"  ERROR: short_len ({slen}) must be < long_len ({llen})")
        slen = int(input("  Short VMA length: ").strip() or "5")
        llen = int(input("  Long  VMA length: ").strip() or "9")

    limit_s = input("Bars to fetch  (default: 2000): ").strip()
    limit   = int(limit_s) if limit_s.isdigit() else 2000

    today_s    = input("Show only today's bars? [y/n]  (default: y): ").strip().lower()
    today_only = today_s != "n"

    rows_s   = input("Print last N bars in table  (default: 20, 0=skip): ").strip()
    show_rows = int(rows_s) if rows_s.isdigit() else 20

    return argparse.Namespace(
        tf=tf_choice, slen=slen, llen=llen,
        limit=limit, today=today_only, all=False, rows=show_rows,
        uri=None, db=None,
    )


# ===========================================================================
#  MAIN
# ===========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="VMA crossover calculator from MongoDB OHLC data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tf",    default=None, choices=list(COLLECTION_MAP),
                        help="Timeframe: 1min | 3min | 5min")
    parser.add_argument("--slen",  type=int, default=5,  help="Short VMA length (default 5)")
    parser.add_argument("--llen",  type=int, default=9,  help="Long  VMA length (default 9)")
    parser.add_argument("--limit", type=int, default=2000,
                        help="Bars to fetch from MongoDB for VMA warm-up (default 2000)")
    parser.add_argument("--today", action="store_true",
                        help="Show only today's IST bars (default behaviour)")
    parser.add_argument("--all",   action="store_true",
                        help="Show ALL fetched bars including warm-up bars")
    parser.add_argument("--rows",  type=int, default=20,
                        help="Rows to print in the bar table (0 = skip table)")
    parser.add_argument("--uri",   default=None, help="Override MongoDB URI")
    parser.add_argument("--db",    default=None, help="Override MongoDB DB name")

    args = parser.parse_args()

    # Drop into interactive menu if no --tf given
    if args.tf is None:
        args = interactive_menu()

    # Apply overrides
    global MONGO_URI, DB_NAME
    if args.uri:
        MONGO_URI = args.uri
    if args.db:
        DB_NAME = args.db

    if args.slen >= args.llen:
        sys.exit(f"ERROR: short_len ({args.slen}) must be < long_len ({args.llen})")

    print(f"\nConnecting to MongoDB ...")
    print(f"DB: {DB_NAME}  |  Collection: {COLLECTION_MAP[args.tf]}  |  Limit: {args.limit}")

    try:
        rows = fetch_rows(args.tf, limit=args.limit)
    except Exception as exc:
        sys.exit(f"ERROR fetching data: {exc}")

    if not rows:
        sys.exit("ERROR: No OHLC data returned. Check MongoDB connection and collection name.")

    print(f"OK: Fetched {len(rows)} bars. Computing VMA (short={args.slen}, long={args.llen}) ...")

    # Compute VMA over ALL bars (warm-up), then filter for display
    all_bars = compute_dual_vma(rows, short_len=args.slen, long_len=args.llen)

    if args.all:
        display_bars = all_bars
        print(f"Showing all {len(display_bars)} bars (includes warm-up bars).")
    else:
        today_bars   = filter_today(all_bars)
        display_bars = today_bars if today_bars else all_bars
        if today_bars:
            print(f"Filtered to {len(display_bars)} today's IST bars.")
        else:
            print("Note: No today's bars found - showing all bars (weekend / test data?).")

    if args.rows > 0:
        print_table(display_bars, show_last=args.rows)

    print_summary(display_bars, args.tf, args.slen, args.llen)


if __name__ == "__main__":
    main()
