from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from pymongo import ASCENDING, DESCENDING, MongoClient
from dotenv import load_dotenv
from datetime import datetime, date, time as dt_time, timedelta, timezone
import base64
import hashlib
import hmac
import json
import numpy as np
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, static_folder=str(BASE_DIR / "static"), static_url_path="/static")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600
CORS(app)

MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = os.getenv("MONGO_DB", "trading_bot_db").strip() or "trading_bot_db"
VMA_RESULTS_COLLECTION = "vma_results"
VMA_TRADES_COLLECTION = "vma_trades"
VMA_ACTIVE_TRADES_COLLECTION = "vma_active_trades"
IST = timezone(timedelta(hours=5, minutes=30))
COLLECTION_MAP = {
    "1min": "OHLC",
    "3min": "OHLC3",
    "5min": "OHLC5",
}
TIME_FIELDS = ("timestamp_ist", "timestamp", "time", "datetime", "date")

ANGEL_BASE_URL = "https://apiconnect.angelone.in"
ANGEL_LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
ANGEL_LTP_PATH = "/rest/secure/angelbroking/order/v1/getLtpData"
ANGEL_INSTRUMENTS_URL = (
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
)


def _totp_now(secret: str, step: int = 30, digits: int = 6) -> str:
    normalized = "".join(secret.strip().split()).upper()
    padding = "=" * (-len(normalized) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    counter = int(time.time() // step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


class _LTPError(Exception):
    """Raised when Angel One LTP fetch fails for an eligible options bar.
    The tick loop catches this and retries the same bar on the next tick
    (last_ts is NOT advanced), rather than permanently skipping the signal.
    """

class AngelOneClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("ANGEL_API_KEY", "").strip()
        self.client_id = os.getenv("ANGEL_CLIENT_ID", "").strip()
        self.mpin = os.getenv("ANGEL_MPIN", "").strip()
        self.totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()
        self._jwt_token: str | None = None
        self._token_ts = 0.0
        self._instruments_cache: list[dict] | None = None
        self._instruments_loaded_at = 0.0

    def is_configured(self) -> bool:
        return all((self.api_key, self.client_id, self.mpin, self.totp_secret))

    def _headers(self, authorized: bool = False) -> dict[str, str]:
        local_ip = "127.0.0.1"
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            pass

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-PrivateKey": self.api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": local_ip,
            "X-ClientPublicIP": local_ip,
            "X-MACAddress": ":".join(re.findall("..", f"{uuid.getnode():012x}"))
            if "re" in globals()
            else "00:00:00:00:00:00",
        }
        if authorized and self._jwt_token:
            headers["Authorization"] = f"Bearer {self._jwt_token}"
        return headers

    def _post_json(self, path: str, payload: dict, *, authorized: bool = False) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            ANGEL_BASE_URL + path,
            data=body,
            headers=self._headers(authorized=authorized),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Angel One HTTP {exc.code}: {message}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Angel One network error: {exc.reason}") from exc

    def _ensure_session(self, force: bool = False) -> None:
        if not self.is_configured():
            raise RuntimeError("Angel One credentials are missing in .env")
        if not force and self._jwt_token and (time.time() - self._token_ts) < 6 * 3600:
            return

        payload = {
            "clientcode": self.client_id,
            "password": self.mpin,
            "totp": _totp_now(self.totp_secret),
        }
        response = self._post_json(ANGEL_LOGIN_PATH, payload, authorized=False)
        data = response.get("data") or {}
        jwt_token = data.get("jwtToken")
        if not response.get("status") or not jwt_token:
            raise RuntimeError(response.get("message") or response.get("errorcode") or "Angel One login failed")
        self._jwt_token = jwt_token
        self._token_ts = time.time()

    def _should_retry_login(self, message: str) -> bool:
        normalized = (message or "").lower()
        retry_markers = (
            "ag8001",
            "ab1010",
            "invalid jwt",
            "invalid token",
            "token expired",
            "session expired",
            "permission denied",
            "access denied",
            "unauthorized",
            "forbidden",
            "http 401",
            "http 403",
        )
        return any(marker in normalized for marker in retry_markers)

    def _load_instruments(self) -> list[dict]:
        if self._instruments_cache and (time.time() - self._instruments_loaded_at) < 6 * 3600:
            return self._instruments_cache

        try:
            with urllib.request.urlopen(ANGEL_INSTRUMENTS_URL, timeout=20) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Unable to download Angel One instrument master: {exc.reason}") from exc

        def parse_expiry(value: str) -> date | None:
            raw_value = (value or "").strip()
            for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    return datetime.strptime(raw_value, fmt).date()
                except ValueError:
                    continue
            return None

        nifty_options = []
        for row in raw:
            if row.get("exch_seg") != "NFO":
                continue
            if row.get("name") != "NIFTY":
                continue
            if row.get("instrumenttype") not in {"OPTIDX", "CE", "PE"}:
                continue
            try:
                strike = float(row.get("strike") or 0) / 100.0
            except (TypeError, ValueError):
                continue
            option_type = row.get("symbol", "")[-2:]
            if option_type not in {"CE", "PE"}:
                option_type = row.get("instrumenttype", "")[-2:]
            if option_type not in {"CE", "PE"}:
                continue
            nifty_options.append(
                {
                    "symboltoken": str(row.get("token")),
                    "tradingsymbol": row.get("symbol"),
                    "name": row.get("name"),
                    "expiry": row.get("expiry"),
                    "expiry_date": parse_expiry(row.get("expiry", "")),
                    "strike": strike,
                    "lot_size": int(float(row.get("lotsize") or 0) or 0),
                    "option_type": option_type,
                    "exch_seg": row.get("exch_seg"),
                }
            )

        if not nifty_options:
            raise RuntimeError("No NIFTY option contracts found in Angel One instrument master")

        self._instruments_cache = nifty_options
        self._instruments_loaded_at = time.time()
        return nifty_options

    def resolve_nifty_option(self, side: str, spot_price: float) -> dict:
        contracts = self._load_instruments()
        side = side.upper()
        if side not in {"CE", "PE"}:
            raise RuntimeError(f"Unsupported option side: {side}")

        today = date.today()
        base_strike = round(spot_price / 50.0) * 50.0
        strike = base_strike
        candidates = [
            c for c in contracts
            if c["option_type"] == side and c["strike"] == strike and c["expiry_date"] and c["expiry_date"] >= today
        ]
        if not candidates:
            raise RuntimeError(f"No {side} contract found for NIFTY strike {strike:.0f}")
        candidates.sort(key=lambda c: (c["expiry_date"], c["tradingsymbol"]))
        return candidates[0]

    def get_ltp(self, exchange: str, tradingsymbol: str, symboltoken: str) -> float:
        payload = {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "symboltoken": str(symboltoken),
        }
        last_error = "Angel One LTP request failed"

        for attempt in range(3):
            try:
                self._ensure_session(force=attempt > 0)
                response = self._post_json(ANGEL_LTP_PATH, payload, authorized=True)
                if response.get("status"):
                    data = response.get("data") or {}
                    ltp = data.get("ltp")
                    if ltp in (None, ""):
                        raise RuntimeError("Angel One returned no LTP for the selected instrument")
                    return float(ltp)
                last_error = response.get("message") or response.get("errorcode") or last_error
            except RuntimeError as exc:
                last_error = str(exc)

            if attempt < 2:
                time.sleep(1.0)
                continue
            raise RuntimeError(last_error)

        raise RuntimeError(last_error)


    def get_nifty_option_ltp(self, side: str, spot_price: float) -> dict:
        contract = self.resolve_nifty_option(side, spot_price)
        ltp = self.get_ltp("NFO", contract["tradingsymbol"], contract["symboltoken"])
        contract_payload = {k: v for k, v in contract.items() if k != "expiry_date"}
        return {
            "exchange": "NFO",
            "ltp": round(ltp, 2),
            **contract_payload,
        }


angel_client = AngelOneClient()
_mongo_housekeeping_ready = False


def get_db():
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is missing in the environment")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=8000)
    return client[DB_NAME]


def ensure_vma_housekeeping():
    global _mongo_housekeeping_ready
    if _mongo_housekeeping_ready:
        return

    db = get_db()
    db[VMA_RESULTS_COLLECTION].create_index(
        [("expires_at", ASCENDING)],
        expireAfterSeconds=0,
        name="vma_results_ttl",
    )
    db[VMA_TRADES_COLLECTION].create_index(
        [("source_result_id", ASCENDING)],
        unique=True,
        sparse=True,
        name="vma_trades_source_result_id",
    )
    db[VMA_TRADES_COLLECTION].create_index(
        [("saved_at", ASCENDING)],
        name="vma_trades_saved_at",
    )
    db[VMA_TRADES_COLLECTION].create_index(
        [("trade_key", ASCENDING)],
        unique=True,
        sparse=True,
        name="vma_trades_trade_key",
    )
    db[VMA_ACTIVE_TRADES_COLLECTION].create_index(
        [("session_id", ASCENDING)],
        unique=True,
        name="vma_active_trades_session_id",
    )
    db[VMA_ACTIVE_TRADES_COLLECTION].create_index(
        [("updated_at", DESCENDING)],
        name="vma_active_trades_updated_at",
    )
    db["vma_logs"].create_index(
        [("timestamp", DESCENDING)],
        name="vma_logs_timestamp",
    )
    _mongo_housekeeping_ready = True


def log_to_db(level: str, message: str, exc: Exception | None = None):
    """Log an event or error to MongoDB `vma_logs` collection."""
    try:
        db = get_db()
        doc = {
            "timestamp": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": message,
            "exception": str(exc) if exc else None,
            "session_id": getattr(_sim, "session_id", None),
        }
        db["vma_logs"].insert_one(doc)
    except Exception:
        pass



def archive_expired_vma_results():
    ensure_vma_housekeeping()
    db = get_db()
    cutoff = datetime.utcnow()
    today_ist = datetime.now(IST).date()
    start_of_today_ist_utc = datetime.combine(today_ist, dt_time.min, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)
    db[VMA_RESULTS_COLLECTION].delete_many(
        {
            "$or": [
                {"expires_at": {"$lte": cutoff}},
                {"saved_at": {"$lt": start_of_today_ist_utc}},
            ]
        }
    )
    # Delete active trades older than today.
    # Only compare against updated_at (a real UTC datetime stored by the server).
    # opened_at can be a plain IST string set by the sim, which MongoDB cannot
    # compare against a datetime — so we exclude it from this filter.
    db[VMA_ACTIVE_TRADES_COLLECTION].delete_many(
        {"updated_at": {"$lt": start_of_today_ist_utc}}
    )
    # Delete any closed trades in active trades collection
    db[VMA_ACTIVE_TRADES_COLLECTION].delete_many({"status": "CLOSED"})
    
    # Delete logs older than 3 days to keep DB size small
    try:
        log_cutoff = (datetime.now(IST) - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        db["vma_logs"].delete_many({"timestamp": {"$lt": log_cutoff}})
    except Exception:
        pass



def prune_non_trade_vma_history():
    """
    Remove misfiled snapshot documents from the trades collection.
    Only actual trade records should live in VMA_TRADES_COLLECTION.
    """
    ensure_vma_housekeeping()
    db = get_db()
    db[VMA_TRADES_COLLECTION].delete_many(
        {
            "$or": [
                {"kind": {"$in": ["dual_vma", "single_vma"]}},
                {
                    "$and": [
                        {"source_result_id": {"$exists": True}},
                        {"trade_key": {"$exists": False}},
                    ]
                },
            ]
        }
    )


def save_vma_result_snapshot(payload: dict):
    ensure_vma_housekeeping()
    archive_expired_vma_results()
    db = get_db()
    saved_at = datetime.utcnow()
    saved_at_ist = saved_at.replace(tzinfo=timezone.utc).astimezone(IST)
    next_ist_midnight = datetime.combine(saved_at_ist.date() + timedelta(days=1), dt_time.min, tzinfo=IST)
    expires_at = next_ist_midnight.astimezone(timezone.utc).replace(tzinfo=None)
    document = {
        **payload,
        "saved_at": saved_at,
        "expires_at": expires_at,
    }
    db[VMA_RESULTS_COLLECTION].insert_one(document)


def save_vma_trades(payload: dict):
    ensure_vma_housekeeping()
    archive_expired_vma_results()
    prune_non_trade_vma_history()
    db = get_db()
    saved_at = datetime.utcnow()
    trades = payload.get("trades") or []
    inserted = 0
    updated = 0
    meta = payload.get("meta") or {}
    for trade in trades:
        trade.pop('mode', None)
        trade_key = build_trade_key(trade, meta)
        document = {
            **trade,
            "trade_key": trade_key,
            "saved_at": saved_at,
            "source": "simulation_ui",
            "meta": meta,
        }
        result = db[VMA_TRADES_COLLECTION].replace_one(
            {"trade_key": trade_key},
            document,
            upsert=True,
        )
        if result.upserted_id is not None:
            inserted += 1
        elif result.modified_count:
            updated += 1
    return {"inserted": inserted, "updated": updated, "total": len(trades)}


def build_trade_key(trade: dict, meta: dict) -> str:
    raw = json.dumps(
        {
            "started_at": meta.get("started_at"),
            "timeframe": meta.get("timeframe"),
            "params": meta.get("params"),
            "trade": {
                "type": trade.get("type"),
                "entryTs": trade.get("entryTs"),
                "entryPrice": trade.get("entryPrice"),
                "exitTs": trade.get("exitTs"),
                "exitPrice": trade.get("exitPrice"),
                "reason": trade.get("reason"),
                "instrument": trade.get("instrument"),
                "contract": trade.get("contract"),
            },
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fetch_saved_vma_trades(limit: int = 100) -> list[dict]:
    ensure_vma_housekeeping()
    archive_expired_vma_results()
    prune_non_trade_vma_history()
    db = get_db()
    docs = list(
        db[VMA_TRADES_COLLECTION]
        .find({"trade_key": {"$exists": True}}, {"_id": 0})
        .sort([("saved_at", DESCENDING)])
        .limit(limit)
    )
    docs.reverse()
    return docs


def save_active_vma_trade(payload: dict) -> dict:
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        raise ValueError("session_id is required")

    status = str(payload.get("status") or "ACTIVE").upper()
    if status not in {"ACTIVE", "CLOSED"}:
        raise ValueError("status must be ACTIVE or CLOSED")

    ensure_vma_housekeeping()
    db = get_db()
    
    if status == "CLOSED":
        # Delete this trade (and any other CLOSED ones) completely
        db[VMA_ACTIVE_TRADES_COLLECTION].delete_many(
            {"$or": [{"session_id": session_id}, {"status": "CLOSED"}]}
        )
        return {"session_id": session_id, "status": "CLOSED (DELETED)"}

    # Delete all other active trades (so only one remains) and any CLOSED trades
    db[VMA_ACTIVE_TRADES_COLLECTION].delete_many(
        {"$or": [{"session_id": {"$ne": session_id}}, {"status": "CLOSED"}]}
    )

    now = datetime.utcnow()
    document = {
        "session_id": session_id,
        "status": status,
        "position": payload.get("position"),
        "trade": payload.get("trade"),
        "meta": payload.get("meta") or {},
        "updated_at": now,
        "opened_at": payload.get("opened_at") or now,
        "source": "simulation_ui",
    }

    db[VMA_ACTIVE_TRADES_COLLECTION].replace_one(
        {"session_id": session_id},
        document,
        upsert=True,
    )
    return {"session_id": session_id, "status": status}


def fetch_active_vma_trade() -> dict | None:
    ensure_vma_housekeeping()
    db = get_db()
    trade = db[VMA_ACTIVE_TRADES_COLLECTION].find_one(
        {"status": "ACTIVE"},
        {"_id": 0},
        sort=[("updated_at", DESCENDING)],
    )
    if trade:
        opened_at = trade.get("opened_at")
        if opened_at:
            if isinstance(opened_at, str):
                # Strings stored by the sim are naive IST (e.g. "2026-06-12 10:00:00").
                # Attach IST timezone explicitly — do NOT use UTC.
                opened_at_dt = parse_timestamp(opened_at).replace(tzinfo=IST)
            else:
                # datetime objects from MongoDB are UTC-naive; attach UTC.
                opened_at_dt = opened_at if opened_at.tzinfo else opened_at.replace(tzinfo=timezone.utc)

            opened_at_ist = opened_at_dt.astimezone(IST)
            today_ist = datetime.now(IST).date()
            if opened_at_ist.date() != today_ist:
                return None
    return trade


def parse_timestamp(value: str) -> datetime:
    """
    Parse a timestamp string into a naive datetime object.
    Handles ISO-8601 with/without fractional seconds and
    simple 'YYYY-MM-DD HH:MM:SS' strings (treated as naive local/IST).
    """
    if not value:
        raise ValueError("Empty timestamp string")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(value.split("+")[0].split("Z")[0].strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised timestamp format: {value!r}")


def _pick_time_field(document: dict) -> str | None:
    key_map = {key.lower(): key for key in document}
    for field in TIME_FIELDS:
        if field in key_map:
            return key_map[field]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SERVER-SIDE SIMULATION ENGINE
# All simulation logic now runs on the server so every browser tab sees the
# exact same trades, positions, and signals. Browsers are display-only.
# ─────────────────────────────────────────────────────────────────────────────

class SimState:
    """Singleton that holds the live simulation state on the server."""
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.active        = False
        self.params        = {}          # sl, target, trailTrigger, trailLock, lotSize, slen, llen, instrument, sidewaysFilter, confirmCandle, minQuality, delta
        self.tf            = "3min"
        self.session_id    = None
        self.started_at    = None        # IST timestamp string of first bar processed
        self.position      = None        # dict: type, instrument, entry, entry_ts, init_sl, cur_sl, tgt, contract, expiry, lot_size, last_price
        self.trades        = []          # completed trades this session (also in MongoDB)
        self.last_ts       = None        # timestamp of last bar processed
        self.last_exit_ts  = None        # timestamp of bar on which last trade closed
        self.last_trade_type = None      # 'CE' | 'PE' | None  (alternation guard)
        self.manual_pause  = False
        self.tick_timer    = None        # threading.Timer handle
        self.status_msg    = ""          # human-readable status for UI
        self.skip_log      = []          # rolling log of skipped bars [{ts, reason}, ...]

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "active":           self.active,
                "params":           self.params,
                "tf":               self.tf,
                "session_id":       self.session_id,
                "started_at":       self.started_at,
                "position":         self.position,
                "trades":           list(self.trades),
                "last_ts":          self.last_ts,
                "last_exit_ts":     self.last_exit_ts,
                "last_trade_type":  self.last_trade_type,
                "manual_pause":     self.manual_pause,
                "status_msg":       self.status_msg,
                "skip_log":         list(self.skip_log[-20:]),   # last 20 skipped bars
            }


_sim = SimState()


def _sim_log_skip(bar: dict, reason: str) -> None:
    """Record a skipped bar with its reason in _sim.skip_log and status_msg."""
    entry = {"ts": bar.get("timestamp", "?"), "reason": reason}
    _sim.skip_log.append(entry)
    # Keep the log bounded — retain last 50 entries
    if len(_sim.skip_log) > 50:
        _sim.skip_log = _sim.skip_log[-50:]
    _sim.status_msg = f"Skipped [{entry['ts']}]: {reason}"


# ── EOD date-guards (prevent double-firing on the same calendar day) ─────────
_eod_exit_date = None   # date string 'YYYY-MM-DD' of last auto-exit
_eod_stop_date = None   # date string 'YYYY-MM-DD' of last auto-stop


def _round2(v) -> float:
    return round(float(v or 0), 2)


def _sim_get_entry_signal(bar: dict, params: dict) -> str:
    """Mirror of JS getEntrySignal(): returns 'CE', 'PE', or 'NONE'."""
    signal = bar.get("signal", "NONE")
    confirm = bar.get("confirm_signal", "NONE")
    if params.get("confirmCandle"):
        return confirm if confirm in ("CE", "PE") else "NONE"
    if (params.get("minQuality", 0) or 0) > 0 and confirm in ("CE", "PE"):
        return confirm
    return signal if signal in ("CE", "PE") else "NONE"


def _sim_complete_trade(exit_price: float, exit_ts: str, reason: str):
    """Close the current open position, push trade to list + MongoDB."""
    pos = _sim.position
    if not pos:
        return
    params = _sim.params
    lot_size = int(params.get("lotSize", 65))
    entry = float(pos["entry"])

    if pos.get("instrument") == "options":
        pts = exit_price - entry
    else:
        direction = 1 if pos["type"] == "CE" else -1
        pts = (exit_price - entry) * direction * float(params.get("delta", 0.5))

    init_sl  = float(pos.get("init_sl", entry))
    cur_sl   = float(pos.get("cur_sl", init_sl))
    trail_sl = _round2(cur_sl) if abs(_round2(cur_sl) - _round2(init_sl)) > 0.01 else None

    trade = {
        "type":       pos["type"],
        "instrument": pos.get("instrument", "options"),
        "contract":   pos.get("contract"),
        "expiry":     pos.get("expiry"),
        "entryTs":    pos.get("entry_ts"),
        "entryPrice": _round2(entry),
        "exitTs":     exit_ts,
        "exitPrice":  _round2(exit_price),
        "sl":         _round2(init_sl),
        "tgt":        _round2(pos.get("tgt", entry)),
        "trailSL":    trail_sl,
        "lotSize":    lot_size,
        "pts":        _round2(pts),
        "grossPnl":   _round2(pts * lot_size),
        "reason":     reason,
    }

    # Record last-exit bar to prevent re-entry on same bar
    entry_bar_ts = pos.get("entry_ts")
    if exit_ts and entry_bar_ts and exit_ts > entry_bar_ts:
        _sim.last_exit_ts = exit_ts
    else:
        _sim.last_exit_ts = entry_bar_ts

    _sim.last_trade_type = pos["type"]
    _sim.position = None
    _sim.trades.append(trade)

    # Persist to MongoDB
    try:
        save_vma_trades({
            "trades": [trade],
            "meta": {
                "timeframe": _sim.tf,
                "started_at": _sim.started_at,
                "params": {
                    "confirmCandle":  bool(_sim.params.get("confirmCandle")),
                    "delta":          float(_sim.params.get("delta", 0.5)),
                    "instrument":     _sim.params.get("instrument", "options"),
                    "llen":           int(_sim.params.get("llen", 9)),
                    "lotSize":        int(_sim.params.get("lotSize", 65)),
                    "minQuality":     int(_sim.params.get("minQuality", 3)),
                    "sidewaysFilter": bool(_sim.params.get("sidewaysFilter")),
                    "sl":             float(_sim.params.get("sl", 40)),
                    "slen":           int(_sim.params.get("slen", 5)),
                    "target":         float(_sim.params.get("target", 60)),
                    "trailLock":      float(_sim.params.get("trailLock", 15)),
                    "trailTrigger":   float(_sim.params.get("trailTrigger", 25)),
                },
            },
        })
        save_active_vma_trade({
            "session_id": _sim.session_id,
            "status": "CLOSED",
        })
    except Exception:
        pass  # don't crash the tick if DB write fails

    _sim.status_msg = f"Trade closed: {reason} @ {_round2(exit_price)}"
    log_to_db("INFO", f"Trade closed: {pos['type']} @ exit {_round2(exit_price)} ({reason})")



def _get_live_ltp(pos: dict, fallback_bar: dict | None = None) -> float:
    """Fetch live LTP for the open position; fall back to last_price / entry."""
    if pos.get("instrument") == "options":
        try:
            return float(angel_client.get_ltp(
                "NFO", pos["contract"], str(pos.get("symboltoken", ""))
            ))
        except Exception:
            pass
    if fallback_bar and fallback_bar.get("close"):
        return float(fallback_bar["close"])
    return float(pos.get("last_price") or pos.get("entry", 0))


def _handle_eod():
    """
    End-of-day auto-controls (runs inside _sim.lock every tick).

    Timeline (IST):
      15:22 – 15:24  ➜ Auto square-off active position (once per day)
      15:25+         ➜ Stop the bot for the day      (once per day)
      15:22+         ➜ Clear last_trade_type so tomorrow starts fresh
    """
    global _eod_exit_date, _eod_stop_date

    now   = datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    hhmm  = now.hour * 100 + now.minute   # e.g. 1522 for 15:22

    # ── Phase 1: Auto square-off 15:22 – 15:24 ───────────────────────────────
    if 1522 <= hhmm < 1525 and _eod_exit_date != today:
        _eod_exit_date = today          # mark done for today
        if _sim.position:
            ts    = now.strftime("%Y-%m-%d %H:%M:%S")
            price = _get_live_ltp(_sim.position)
            _sim_complete_trade(price, ts, "AUTO_EOD_EXIT")
            _sim.status_msg = "[EOD] Position auto-squared off at 15:22 IST."

    # ── Phase 2: Stop bot at 15:25+ ──────────────────────────────────────────
    if hhmm >= 1525 and _eod_stop_date != today:
        _eod_stop_date = today          # mark done for today
        _sim.active    = False
        if _sim.tick_timer:
            _sim.tick_timer.cancel()
            _sim.tick_timer = None
        _sim.status_msg = "[EOD] Bot auto-stopped at 15:25 IST. Will resume tomorrow."

    # ── Phase 3: Clear side-lock AND last_exit_ts for fresh next-day signal ──
    # last_exit_ts is cleared here so that tomorrow's morning bars are never
    # blocked by today's EOD exit timestamp (belt-and-suspenders alongside
    # the reset() call in _start_server_sim).
    if hhmm >= 1522:
        _sim.last_trade_type = None
        _sim.last_exit_ts    = None



def _sim_process_bar(bar: dict):
    """
    Mirror of JS processBar() + updateOpenPosition() — runs on the server.
    Mutates _sim in place (caller must hold _sim.lock).
    _handle_eod() runs separately in _server_tick and handles exit at 15:22
    and bot-stop at 15:25, so this function only needs the entry cutoff.
    """
    params   = _sim.params
    now_ist  = datetime.now(IST)
    now_hhmm = now_ist.hour * 100 + now_ist.minute   # HHMM e.g. 1522

    # Parse bar time to enforce EOD and entry cutoffs historically/in replay
    ts = bar.get("timestamp")
    bar_hhmm = 0
    if ts:
        try:
            bar_dt = parse_timestamp(ts)
            bar_hhmm = bar_dt.hour * 100 + bar_dt.minute
        except Exception:
            pass

    # ── If we have an open position: update SL/trail/check exit ─────────────
    if _sim.position:
        pos = _sim.position
        ts = bar["timestamp"]

        # EOD exit check using bar's timestamp
        if bar_hhmm >= 1522:
            price = _get_live_ltp(pos, fallback_bar=bar)
            _sim_complete_trade(price, ts, "AUTO_EOD_EXIT")
            return


        if pos.get("instrument") == "options":
            # Fetch live option LTP for the existing contract
            try:
                ltp_data = angel_client.get_ltp(
                    "NFO",
                    pos["contract"],
                    str(pos.get("symboltoken", "")),
                )
                price = float(ltp_data)
            except Exception:
                price = float(pos.get("last_price") or pos["entry"])

            pos["last_price"] = price
            trail_trigger = float(params.get("trailTrigger", 0))
            trail_lock    = float(params.get("trailLock", 0))
            if trail_trigger > 0 and price - pos["entry"] >= trail_trigger:
                pos["cur_sl"] = max(pos["cur_sl"], pos["entry"] + trail_lock)
            if price <= pos["cur_sl"]:
                reason = "TRAILING_SL" if pos["cur_sl"] > pos["init_sl"] else "SL"
                _sim_complete_trade(pos["cur_sl"], ts, reason)
                return
            if price >= pos["tgt"]:
                _sim_complete_trade(pos["tgt"], ts, "TARGET")
                return
        else:
            # Futures/index: use bar OHLC
            pos["last_price"] = bar["close"]
            trail_trigger = float(params.get("trailTrigger", 0))
            trail_lock    = float(params.get("trailLock", 0))
            if pos["type"] == "CE":
                if trail_trigger > 0 and bar["high"] - pos["entry"] >= trail_trigger:
                    pos["cur_sl"] = max(pos["cur_sl"], bar["close"] - trail_lock)
                if bar["low"] <= pos["cur_sl"]:
                    reason = "TRAILING_SL" if pos["cur_sl"] > pos["init_sl"] else "SL"
                    _sim_complete_trade(pos["cur_sl"], ts, reason)
                    return
                if bar["high"] >= pos["tgt"]:
                    _sim_complete_trade(pos["tgt"], ts, "TARGET")
                    return
            else:  # PE
                if trail_trigger > 0 and pos["entry"] - bar["low"] >= trail_trigger:
                    pos["cur_sl"] = min(pos["cur_sl"], bar["close"] + trail_lock)
                if bar["high"] >= pos["cur_sl"]:
                    reason = "TRAILING_SL" if pos["cur_sl"] < pos["init_sl"] else "SL"
                    _sim_complete_trade(pos["cur_sl"], ts, reason)
                    return
                if bar["low"] <= pos["tgt"]:
                    _sim_complete_trade(pos["tgt"], ts, "TARGET")
                    return
        return  # position still open — only exit via SL / target / EOD


    # ── No open position: check for entry signal ─────────────────────────────
    # No new entries after 15:15 IST (handled here, after position-management)
    if now_hhmm >= 1515 or (bar_hhmm >= 1515 if bar_hhmm > 0 else False):
        _sim_log_skip(bar, f"Entry cutoff — time {bar_hhmm // 100:02d}:{bar_hhmm % 100:02d} >= 15:15 or live clock past 15:15")
        return

    signal = _sim_get_entry_signal(bar, params)
    if signal not in ("CE", "PE"):
        raw_sig = bar.get("signal", "NONE")
        raw_cnf = bar.get("confirm_signal", "NONE")
        # No crossover on this bar — routine, don't clutter the skip log.
        return

    # Fresh-signal guard: skip bars at or before the last exit bar.
    # If the crossover signal occurs on the exact same bar as the exit,
    # block entry ONLY if it is in the same direction as the exited trade
    # to prevent instant duplicate re-entry while allowing trend reversals.
    if _sim.last_exit_ts:
        if bar["timestamp"] < _sim.last_exit_ts:
            _sim_log_skip(bar, f"Fresh-signal guard: bar {bar['timestamp']} < last_exit_ts {_sim.last_exit_ts}")
            return
        if bar["timestamp"] == _sim.last_exit_ts and signal == _sim.last_trade_type:
            _sim_log_skip(bar, f"Same-bar duplicate guard: {signal} re-entry on exit bar {_sim.last_exit_ts}")
            return

    # Two-sides conflict: if the bar has a direct signal AND an opposite
    # confirm_signal simultaneously, both CE and PE are firing at the same
    # point — skip the trade entirely.
    direct_sig  = bar.get("signal", "NONE")
    confirm_sig = bar.get("confirm_signal", "NONE")
    if (
        direct_sig  in ("CE", "PE")
        and confirm_sig in ("CE", "PE")
        and direct_sig != confirm_sig
    ):
        _sim_log_skip(bar, f"Two sides at same point (signal={direct_sig}, confirm={confirm_sig}) — trade skipped")
        return

    # Sideways filter
    if params.get("sidewaysFilter") and bar.get("is_sideways"):
        _sim_log_skip(bar, "Sideways filter blocked — market is sideways")
        return

    # Quality filter
    if (bar.get("quality") or 0) < (params.get("minQuality") or 0):
        _sim_log_skip(bar, f"Quality filter: bar quality {bar.get('quality', 0)} < minQuality {params.get('minQuality', 0)}")
        return

    # ── Open a new position ───────────────────────────────────────────────────
    sl_pts     = float(params.get("sl", 40))
    tgt_pts    = float(params.get("target", 60))
    lot_size   = int(params.get("lotSize", 65))
    instrument = params.get("instrument", "options")
    ts         = bar["timestamp"]

    if instrument == "options":
        try:
            quote = angel_client.get_nifty_option_ltp(signal, float(bar["close"]))
            entry    = float(quote["ltp"])
            contract = quote.get("tradingsymbol")
            expiry   = quote.get("expiry")
            sym_tok  = quote.get("symboltoken")
        except Exception as exc:
            # LTP fetch failed (common at market-open due to Angel One API
            # rate-limiting or a stale login token).
            # Raise _LTPError so the tick loop can hold last_ts at the
            # PREVIOUS bar and retry THIS bar on the next tick — preserving
            # the signal rather than permanently skipping it.
            _sim.status_msg = f"LTP fetch failed — will retry: {exc}"
            log_to_db("WARNING", f"LTP fetch failed for options {signal} on bar {ts} — will retry", exc)
            raise _LTPError(str(exc)) from exc

        _sim.position = {
            "type":        signal,
            "instrument":  "options",
            "entry":       entry,
            "entry_ts":    ts,
            "entry_tf":    _sim.tf,      # TF that generated the entry signal
            "init_sl":     entry - sl_pts,
            "cur_sl":      entry - sl_pts,
            "tgt":         entry + tgt_pts,
            "contract":    contract,
            "expiry":      expiry,
            "symboltoken": sym_tok,
            "lot_size":    lot_size,
            "last_price":  entry,
        }
    else:
        # Futures/index — entry at bar close
        entry  = float(bar["close"])
        delta  = float(params.get("delta", 0.5))
        direction = 1 if signal == "CE" else -1
        _sim.position = {
            "type":        signal,
            "instrument":  "futures",
            "entry":       entry,
            "entry_ts":    ts,
            "entry_tf":    _sim.tf,      # TF that generated the entry signal
            "init_sl":     entry - direction * sl_pts,
            "cur_sl":      entry - direction * sl_pts,
            "tgt":         entry + direction * tgt_pts,
            "contract":    None,
            "expiry":      None,
            "symboltoken": None,
            "lot_size":    lot_size,
            "last_price":  entry,
        }

    _sim.status_msg = f"Position opened: {signal} @ {_round2(entry)}"
    log_to_db("INFO", f"Position opened: {signal} @ {entry} (option contract: {contract})")


    # Persist active trade to MongoDB so it survives a server restart
    try:
        save_active_vma_trade({
            "session_id": _sim.session_id,
            "status": "ACTIVE",
            "position": _sim.position,
            "opened_at": ts,
            "meta": {
                "timeframe": _sim.tf,
                "started_at": _sim.started_at,
                "params": _sim.params,
            },
        })
    except Exception:
        pass


def _server_tick():
    """
    Background tick: fetches new bars from MongoDB, runs the simulation loop.
    Reschedules itself using threading.Timer.
    """
    with _sim.lock:
        if not _sim.active:
            return
        try:
            rows = fetch_closes(_sim.tf, limit=2000)
            data = compute_dual_vma(
                rows,
                int(_sim.params.get("slen", 5)),
                int(_sim.params.get("llen", 9)),
            )

            start_ts = _sim.started_at
            new_bars = [
                b for b in data
                if b["timestamp"]
                and start_ts
                and b["timestamp"] >= start_ts
                and (not _sim.last_ts or b["timestamp"] > _sim.last_ts)
            ]

            for bar in new_bars:
                try:
                    _sim_process_bar(bar)
                    # Advance last_ts after successful (or cleanly-skipped) processing
                    _sim.last_ts = bar["timestamp"]
                except _LTPError as exc:
                    # Angel One LTP unavailable — advance last_ts so we don't
                    # retry the same bar every tick forever. The next bar (which
                    # often has the same signal via confirm_signal) is tried
                    # immediately on this same tick via 'continue'.
                    _sim.last_ts = bar["timestamp"]
                    _sim_log_skip(bar, f"LTP fetch failed — bar skipped: {exc}")
                    continue
                except Exception as exc:
                    # Unexpected error: advance last_ts to avoid infinite retry
                    _sim.last_ts = bar["timestamp"]
                    _sim.status_msg = f"Bar error (skipped): {exc}"
                    break

            # ── EOD auto-controls (exit 15:22, stop 15:25) ────────────────────
            _handle_eod()

        except Exception as exc:
            _sim.status_msg = f"Tick error: {exc}"
            log_to_db("ERROR", "Background tick error", exc)


    # Reschedule — stop ticking after EOD window
    now_hhmm = datetime.now(IST).hour * 100 + datetime.now(IST).minute
    interval = int(_sim.params.get("refreshInterval", 10000)) / 1000.0
    interval = max(5.0, min(interval, 60.0))
    with _sim.lock:
        if _sim.active and now_hhmm < 1530:
            _sim.tick_timer = threading.Timer(interval, _server_tick)
            _sim.tick_timer.daemon = True
            _sim.tick_timer.start()
        elif _sim.active and now_hhmm >= 1530:
            # _handle_eod() will have already stopped the bot at 15:25;
            # this is a final safety net in case it didn't.
            _handle_eod()


def _start_server_sim(params: dict, tf: str, refresh_ms: int = 10000,
                      carry_position: dict | None = None):
    """
    Start (or restart) the server-side simulation.

    carry_position: when provided (e.g. on a TF switch) the existing open
    position is preserved into the new sim state instead of being discarded.
    The position will continue to be managed by the tick loop under the new TF.
    """
    with _sim.lock:
        # Cancel any running timer
        if _sim.tick_timer:
            _sim.tick_timer.cancel()
            _sim.tick_timer = None

        _sim.reset()
        _sim.active      = True
        _sim.params      = dict(params)
        _sim.params["refreshInterval"] = refresh_ms
        _sim.tf          = tf
        _sim.session_id  = "srv-" + hashlib.sha256(
            (tf + json.dumps(params, sort_keys=True) + str(time.time())).encode()
        ).hexdigest()[:12]

        # ── started_at: market-open anchor in the morning, candle-floor mid-day ──
        #
        # Problem: if the user clicks Start at e.g. 9:21 on a 3-min TF, the
        # candle-floor gives started_at = "09:21:00".  The morning crossover
        # (9:15 bar) AND its confirm bar (9:18 bar) are both < "09:21:00" and
        # get filtered out → first trade of the day is permanently missed.
        #
        # Fix: during the morning session (before 11:00 IST) always anchor
        # started_at to market open (09:15:00) so every bar from the first
        # candle onwards is eligible.  After 11:00 we fall back to the
        # candle-floor approach to prevent stale-signal replay on mid-day
        # restarts / TF-switches.
        now_ist = datetime.now(IST)
        tf_minutes = {"1min": 1, "3min": 3, "5min": 5, "15min": 15}.get(tf, 3)
        market_open_cutoff = now_ist.replace(hour=11, minute=0, second=0, microsecond=0)
        if now_ist <= market_open_cutoff:
            # Morning session: anchor to 09:15 so we never miss the first bar
            _sim.started_at = now_ist.strftime("%Y-%m-%d") + " 09:15:00"
        else:
            # Mid-day: floor to the current candle to avoid stale trade replay
            floored_minute = (now_ist.minute // tf_minutes) * tf_minutes
            candle_open = now_ist.replace(minute=floored_minute, second=0, microsecond=0)
            _sim.started_at = candle_open.strftime("%Y-%m-%d %H:%M:%S")

        if carry_position:
            # Re-inject the position that was open before the TF switch.
            # Keep its original entry / SL / target untouched; only the
            # signal-generation timeframe changes.  The tick loop will
            # keep fetching live LTP and enforcing SL/target as before.
            _sim.position   = carry_position
            # Set last_ts to the position's entry bar so the new-bars filter
            # in _server_tick doesn't replay bars before the entry.
            _sim.last_ts    = carry_position.get("entry_ts")
            _sim.status_msg = (
                f"TF switched to {tf}. Existing "
                f"{carry_position.get('type')} position carried forward "
                f"(entry @ {carry_position.get('entry')})."
            )
        else:
            _sim.status_msg = "Simulation started."
            log_to_db("INFO", f"Simulation session started: TF={tf}, params={_sim.params}")


    # Kick off the first tick immediately (outside lock to avoid deadlock)
    threading.Thread(target=_server_tick, daemon=True).start()


def _stop_server_sim(reason: str = "MANUAL"):
    """Stop the simulation, properly closing any open position first."""
    # ── Step 1: capture the open position OUTSIDE the main op so we can call
    #            _sim_complete_trade (which itself mutates _sim) safely.
    with _sim.lock:
        if _sim.tick_timer:
            _sim.tick_timer.cancel()
            _sim.tick_timer = None
        _sim.active = False
        has_position = bool(_sim.position)

    # ── Step 2: if there was an open position, close it properly so it is
    #            saved to MongoDB (Bug 2 fix — was silently lost before).
    if has_position:
        ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        with _sim.lock:
            _sim_complete_trade(
                float(_sim.position.get("last_price") or _sim.position.get("entry", 0)),
                ts,
                reason,
            ) if _sim.position else None
        _sim.status_msg = f"Simulation stopped ({reason}). Open position closed and saved."
        log_to_db("INFO", f"Simulation stopped ({reason}). Active position was closed.")
    else:
        _sim.status_msg = f"Simulation stopped ({reason})."
        log_to_db("INFO", f"Simulation stopped ({reason}).")



def restore_sim_from_db():
    """
    Called once at server startup (Bug 3 fix).
    If there is an ACTIVE trade document in MongoDB that was opened today,
    re-hydrate _sim so the server continues managing the position without
    needing the browser to restart the simulation.
    """
    try:
        trade_doc = fetch_active_vma_trade()   # returns None if stale/missing
        if not trade_doc:
            return
        pos  = trade_doc.get("position")
        meta = trade_doc.get("meta") or {}
        if not pos:
            return

        with _sim.lock:
            if _sim.active:        # already running — don't clobber
                return
            _sim.active      = True
            _sim.position    = pos
            _sim.params      = meta.get("params") or {}
            _sim.tf          = meta.get("timeframe") or "3min"
            _sim.started_at  = meta.get("started_at") or (
                datetime.now(IST).strftime("%Y-%m-%d") + " 09:16:00"
            )
            _sim.session_id  = trade_doc.get("session_id") or (
                "restore-" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:12]
            )
            _sim.last_ts     = pos.get("entry_ts")   # so tick starts after entry bar
            _sim.last_exit_ts = None
            _sim.status_msg  = "Position restored after server restart."

        # Start ticking again immediately
        threading.Thread(target=_server_tick, daemon=True).start()
        print(f"[VMA] Restored active trade from DB: {pos.get('type')} @ {pos.get('entry')}")
    except Exception as exc:
        print(f"[VMA] restore_sim_from_db failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Server-startup hook: restore sim from DB on first request (Gunicorn restart
# recovery). We use an _startup_done flag + lock so it only runs once even
# with multiple workers (both workers will race, but the second will see _sim.active).
_startup_done = False
_startup_lock = threading.Lock()

@app.before_request
def _on_first_request():
    global _startup_done
    if _startup_done:
        return
    with _startup_lock:
        if not _startup_done:
            restore_sim_from_db()
            _startup_done = True



@app.route("/api/sim-state")
def api_sim_state():
    """Return the full server-side simulation state. All browsers poll this."""
    return jsonify({"ok": True, "sim": _sim.to_dict()})


@app.route("/api/logs")
def api_logs():
    """Expose MongoDB vma_logs collection to browser for easy debugging."""
    limit = int(request.args.get("limit", 150))
    try:
        db = get_db()
        logs = list(db["vma_logs"].find({}, {"_id": 0}).sort("timestamp", -1).limit(limit))
        return jsonify({"ok": True, "logs": logs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/api/sim-control", methods=["POST"])
def api_sim_control():
    """
    Control the server-side simulation.

    Body: { "action": "start" | "stop" | "squareoff", "params": {...}, "tf": "...", "refresh_ms": N }
    """
    payload = request.get_json(silent=True) or {}
    action  = str(payload.get("action") or "").lower()

    if action == "start":
        params     = payload.get("params") or {}
        tf         = str(payload.get("tf") or "3min")
        refresh_ms = int(payload.get("refresh_ms") or 10000)
        if not params:
            return jsonify({"ok": False, "error": "params required"}), 400

        # ── Idempotency guard: if the sim is already running with the same tf
        #    AND started_at belongs to TODAY, just return success without
        #    resetting state (Bug 4 fix).
        #
        #    The date check is critical: if the process runs 24/7 and the bot
        #    was somehow left active from a previous calendar day (e.g. EOD
        #    stop failed), we must NOT treat it as already-active — we need a
        #    fresh reset() so started_at and last_exit_ts are cleared for the
        #    new trading day, ensuring the morning's first bar is never skipped.
        today_ist_str = datetime.now(IST).strftime("%Y-%m-%d")
        with _sim.lock:
            started_at_date = (
                _sim.started_at[:10] if _sim.started_at and len(_sim.started_at) >= 10
                else ""
            )
            already_active = (
                _sim.active
                and _sim.tf == tf
                and _sim.params.get("sl") == params.get("sl")
                and _sim.params.get("target") == params.get("target")
                and _sim.params.get("instrument") == params.get("instrument")
                and started_at_date == today_ist_str   # ← must be TODAY's session
            )
            current_session = _sim.session_id
        if already_active:
            return jsonify({"ok": True, "session_id": current_session, "noop": True})

        _start_server_sim(params, tf, refresh_ms)
        return jsonify({"ok": True, "session_id": _sim.session_id})


    if action == "stop":
        _stop_server_sim("MANUAL")
        return jsonify({"ok": True})

    if action == "squareoff":
        with _sim.lock:
            if _sim.position:
                pos = _sim.position
                exit_price = float(pos.get("last_price") or pos.get("entry", 0))
                ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
                _sim_complete_trade(exit_price, ts, "MANUAL")
            else:
                pass
        return jsonify({"ok": True})

    if action == "tf_switch":
        tf = str(payload.get("tf") or _sim.tf)
        with _sim.lock:
            # Snapshot the open position (if any) so we can carry it forward.
            # We intentionally do NOT square it off — the user only changed the
            # signal-generation timeframe; the existing trade should keep running
            # under its original entry / SL / target until it closes naturally.
            open_position = dict(_sim.position) if _sim.position else None
            old_params    = dict(_sim.params)
            refresh_ms    = int(_sim.params.get("refreshInterval", 10000))
            was_active    = _sim.active
        if was_active:
            _start_server_sim(old_params, tf, refresh_ms, carry_position=open_position)
        return jsonify({"ok": True, "carried_position": bool(open_position)})

    if action == "patch_params":
        """
        Hot-patch mutable filter/risk params on a running simulation.
        This lets the browser push UI changes (sidewaysFilter, minQuality,
        confirmCandle, sl, target, trailTrigger, trailLock, lotSize, delta)
        to the server in real-time WITHOUT a stop/restart cycle.
        Only keys that are present in the request payload are updated.
        VMA lengths (slen, llen) and instrument/tf are intentionally excluded
        because changing those requires a full restart.
        """
        patch = payload.get("params") or {}
        PATCHABLE = {
            "sidewaysFilter", "minQuality", "confirmCandle",
            "sl", "target", "trailTrigger", "trailLock", "lotSize", "delta",
        }
        with _sim.lock:
            for key, val in patch.items():
                if key in PATCHABLE:
                    _sim.params[key] = val
            _sim.status_msg = "Params updated live: " + ", ".join(
                f"{k}={v}" for k, v in patch.items() if k in PATCHABLE
            )
        return jsonify({"ok": True, "patched": {k: v for k, v in patch.items() if k in PATCHABLE}})

    return jsonify({"ok": False, "error": f"Unknown action: {action}"}), 400


# ─── End of server-side simulation engine ────────────────────────────────────


def fetch_closes(timeframe: str, limit: int = 500):
    col_name = COLLECTION_MAP.get(timeframe)
    if not col_name:
        raise ValueError(f"Unknown timeframe: {timeframe}")
    db = get_db()
    col = db[col_name]

    sample = col.find_one()
    ts_field = _pick_time_field(sample) if sample else None

    sort_desc = [(ts_field, -1)] if ts_field else [("_id", -1)]
    docs = list(col.find({}, {"_id": 0}).sort(sort_desc).limit(limit))
    docs.reverse()

    rows_by_timestamp = {}
    for d in docs:
        norm = {k.lower(): v for k, v in d.items()}
        close = float(norm.get("close", 0) or 0)
        open_ = float(norm.get("open", 0) or 0)
        high  = float(norm.get("high", 0) or 0)
        low   = float(norm.get("low", 0) or 0)

        ts_val = None
        for field in TIME_FIELDS:
            if norm.get(field):
                ts_val = str(norm[field])
                break

        row = {
            "timestamp": ts_val,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
        }

        dedupe_key = ts_val or f"row-{len(rows_by_timestamp)}"
        rows_by_timestamp[dedupe_key] = row

    return list(rows_by_timestamp.values())


# ─────────────────────────────────────────────────────────────
# Single VMA (LazyBear)
# ─────────────────────────────────────────────────────────────

def compute_vma(rows, length: int = 6):
    if length <= 0:
        raise ValueError("length must be greater than 0")

    k = 1.0 / length
    pdmS = mdmS = pdiS = mdiS = iS_val = 0.0
    vma = None
    iS_arr = []
    results = []
    prev_vma_raw = None

    for i, r in enumerate(rows):
        src = r["close"]
        prev = rows[i - 1]["close"] if i > 0 else src

        pdm = max(src - prev, 0.0)
        mdm = max(prev - src, 0.0)

        # Match TradingView's recursive smoothing with nz(previous) semantics.
        pdmS = (1 - k) * pdmS + k * pdm
        mdmS = (1 - k) * mdmS + k * mdm

        s = pdmS + mdmS
        pdi = (pdmS / s) if s else 0.0
        mdi = (mdmS / s) if s else 0.0

        pdiS = (1 - k) * pdiS + k * pdi
        mdiS = (1 - k) * mdiS + k * mdi

        d = abs(pdiS - mdiS)
        s1 = pdiS + mdiS
        ratio = (d / s1) if s1 else 0.0
        iS_val = (1 - k) * iS_val + k * ratio
        iS_arr.append(iS_val)

        win = iS_arr[max(0, i - length + 1): i + 1]
        hhv, llv = max(win), min(win)
        rng = hhv - llv
        vI = ((iS_val - llv) / rng) if rng else 0.0

        # Seed from the first price so the line starts on-chart instead of at 0.
        if vma is None:
            vma = src
        else:
            vma = (1 - k * vI) * vma + k * vI * src

        prev_vma = prev_vma_raw if prev_vma_raw is not None else vma
        if vma > prev_vma:
            trend = "UP"
        elif vma < prev_vma:
            trend = "DOWN"
        else:
            trend = "FLAT"

        prev_vma_raw = vma

        results.append({
            "timestamp": r["timestamp"],
            "open":  round(r["open"],  4),
            "high":  round(r["high"],  4),
            "low":   round(r["low"],   4),
            "close": round(src,        4),
            "vma":   round(vma,        4),
            "trend": trend,
        })

    return results


@app.route("/api/vma")
def api_vma():
    timeframe = request.args.get("tf", "1min")
    length    = int(request.args.get("length", 6))
    try:
        rows    = fetch_closes(timeframe, limit=2000)
        data    = compute_vma(rows, length)
        last    = data[-1]  if data else {}
        prev    = data[-2]  if len(data) > 1 else last
        delta   = round(last.get("vma", 0) - prev.get("vma", 0), 4) if data else 0
        response = {
            "ok":         True,
            "timeframe":  timeframe,
            "length":     length,
            "total_bars": len(data),
            "current": {
                "timestamp": last.get("timestamp"),
                "close":     last.get("close"),
                "vma":       last.get("vma"),
                "prev_vma":  prev.get("vma"),
                "delta":     delta,
                "trend":     last.get("trend"),
            },
            "history": data[-50:],
        }
        save_vma_result_snapshot({
            "kind": "single_vma",
            "timeframe": timeframe,
            "length": length,
            "total_bars": len(data),
            "current": response["current"],
        })
        return jsonify(response)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# Dual-VMA crossover helpers
# ─────────────────────────────────────────────────────────────

def _atr_series(rows: list[dict], period: int = 14) -> list[float]:
    """Wilder's ATR — same as TradingView's built-in ATR()"""
    trs, atrs = [], []
    prev_close = None
    atr = 0.0
    for i, r in enumerate(rows):
        hi, lo, cl = r["high"], r["low"], r["close"]
        if prev_close is None:
            tr = hi - lo
        else:
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        if len(trs) < period:
            atr = sum(trs) / len(trs)
        else:
            if len(trs) == period:
                atr = sum(trs) / period
            else:
                atr = (atr * (period - 1) + tr) / period
        atrs.append(round(atr, 4))
        prev_close = cl
    return atrs


def _rsi_series(rows: list[dict], period: int = 14) -> list[float]:
    """Wilder RSI"""
    closes = [r["close"] for r in rows]
    gains, losses = [], []
    rsis = []
    avg_gain = avg_loss = 0.0
    for i, cl in enumerate(closes):
        if i == 0:
            rsis.append(50.0)
            continue
        chg = cl - closes[i - 1]
        gain = max(chg, 0.0)
        loss = max(-chg, 0.0)
        if i < period:
            gains.append(gain)
            losses.append(loss)
            avg_gain = sum(gains) / len(gains)
            avg_loss = sum(losses) / len(losses)
        else:
            if i == period:
                gains.append(gain)
                losses.append(loss)
                avg_gain = sum(gains) / period
                avg_loss = sum(losses) / period
            else:
                avg_gain = (avg_gain * (period - 1) + gain) / period
                avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_gain == 0.0 and avg_loss == 0.0:
            rsi_val = 50.0
        elif avg_loss == 0.0:
            rsi_val = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_val = round(100.0 - 100.0 / (1.0 + rs), 2)
        rsis.append(rsi_val)
    return rsis


def _vma_series(rows: list[dict], length: int) -> list[float]:
    """
    Python port of TradingView VMA_LB (LazyBear) Pine Script with warm-up seeding.

    Pine Script (verbatim):
        k = 1.0/l
        pdm  = max((src - src[1]), 0)
        mdm  = max((src[1] - src), 0)
        pdmS = ((1 - k)*nz(pdmS[1]) + k*pdm)
        mdmS = ((1 - k)*nz(mdmS[1]) + k*mdm)
        s    = pdmS + mdmS  ;  pdi = pdmS/s  ;  mdi = mdmS/s
        pdiS = ((1 - k)*nz(pdiS[1]) + k*pdi)
        mdiS = ((1 - k)*nz(mdiS[1]) + k*mdi)
        d    = abs(pdiS - mdiS)  ;  s1 = pdiS + mdiS
        iS   = ((1 - k)*nz(iS[1]) + k*d/s1)
        hhv  = highest(iS, l)  ;  llv = lowest(iS, l)
        vI   = (iS - llv)/(hhv - llv)
        vma  = (1 - k*vI)*nz(vma[1]) + k*vI*src

    WARM-UP SEEDING:
        TradingView has years of history so nz(vma[1])=0 only on the very
        first ever bar (e.g. 2010). By today the VMA is fully converged.
        We only have today's bars (~46 for a 3-min day). If we start vma=0,
        the formula vma = (1-k*vI)*0 + k*vI*src converges VERY slowly
        (alpha = k*vI is typically small), leaving VMA hundreds of points
        below the actual price.

        Fix: seed vma_prev = first bar's close.  The pdmS/mdmS/pdiS/mdiS/iS
        series all start from 0 and converge within a handful of bars
        (decay rate (1-k)^n for those EMA-like series is fast).  Only the
        VMA line itself needs warm-start seeding because its effective alpha
        k*vI is small and convergence from 0 takes far too long.
    """
    if length <= 0:
        raise ValueError("length must be > 0")

    k = 1.0 / length
    pdmS = mdmS = pdiS = mdiS = iS_val = 0.0
    # Seed VMA at first bar's close so the line starts on-chart immediately.
    # This mirrors TradingView which already has a fully-converged VMA from
    # years of prior history before today's session begins.
    vma_prev = rows[0]["close"] if rows else 0.0
    iS_arr: list[float] = []
    out: list[float] = []

    for i, r in enumerate(rows):
        src  = r["close"]
        prev = rows[i - 1]["close"] if i > 0 else src  # bar 0: no prior bar → pdm=0

        # Directional movement
        pdm  = max(src - prev, 0.0)
        mdm  = max(prev - src, 0.0)

        # Smoothed DM (EMA-style, alpha=k)
        pdmS = (1 - k) * pdmS + k * pdm
        mdmS = (1 - k) * mdmS + k * mdm

        s   = pdmS + mdmS
        pdi = (pdmS / s) if s else 0.0
        mdi = (mdmS / s) if s else 0.0

        # Smoothed DI
        pdiS = (1 - k) * pdiS + k * pdi
        mdiS = (1 - k) * mdiS + k * mdi

        # Volatility index iS
        d      = abs(pdiS - mdiS)
        s1     = pdiS + mdiS
        ratio  = (d / s1) if s1 else 0.0
        iS_val = (1 - k) * iS_val + k * ratio
        iS_arr.append(iS_val)

        # Rolling highest/lowest of iS over `length` bars (Pine: highest/lowest)
        win = iS_arr[max(0, i - length + 1): i + 1]
        hhv, llv = max(win), min(win)
        rng = hhv - llv
        vI  = ((iS_val - llv) / rng) if rng else 0.0

        # VMA update — seeded from first bar's close (see warm-up note above)
        vma_val  = (1 - k * vI) * vma_prev + k * vI * src
        vma_prev = vma_val
        out.append(round(vma_val, 4))

    return out


def compute_dual_vma(rows: list[dict], short_len: int = 9, long_len: int = 21) -> list[dict]:
    """
    Compute Short-VMA and Long-VMA series for the same candle list.
    Tag each bar with a crossover signal and professional filters.
    """
    short_vals = _vma_series(rows, short_len)
    long_vals  = _vma_series(rows, long_len)
    atr_vals   = _atr_series(rows, 14)
    rsi_vals   = _rsi_series(rows, 14)

    results = []
    for i, r in enumerate(rows):
        sv = short_vals[i]
        lv = long_vals[i]
        atr = atr_vals[i]
        rsi = rsi_vals[i]

        # crossover detection
        if i == 0:
            signal = "NONE"
        else:
            prev_sv = short_vals[i - 1]
            prev_lv = long_vals[i - 1]
            if prev_sv <= prev_lv and sv > lv:
                signal = "CE"   # bullish crossover → buy CALL
            elif prev_sv >= prev_lv and sv < lv:
                signal = "PE"   # bearish crossover → buy PUT
            else:
                signal = "NONE"

        # Note: Price-vs-VMA override removed — flipping crossover signals based
        # on instantaneous price position produced incorrect CE/PE on fast timeframes.

        # slopes (3-bar lookback)
        short_slope = round(sv - short_vals[i - 3], 4) if i >= 3 else 0.0
        long_slope  = round(lv - long_vals[i - 3], 4) if i >= 3 else 0.0

        # vma spread & sideways flag
        vma_spread = round(abs(sv - lv), 4)
        is_sideways = bool(vma_spread < round(atr * 0.3, 4))

        # upper and lower bands
        upper_band = round(sv + atr * 1.5, 4)
        lower_band = round(sv - atr * 1.5, 4)

        # short-vma slope for display colouring
        if i == 0:
            svma_trend = "FLAT"
        elif sv > short_vals[i - 1]:
            svma_trend = "UP"
        elif sv < short_vals[i - 1]:
            svma_trend = "DOWN"
        else:
            svma_trend = "FLAT"

        # relative position of short vs long
        position = "ABOVE" if sv > lv else ("BELOW" if sv < lv else "CROSS")

        # confirm_signal is signal from the previous bar (used in confirmCandle mode)
        confirm_signal = results[i - 1]["signal"] if i > 0 else "NONE"

        # Quality score (0-5) — scored against the CURRENT bar's signal so that
        # the crossover bar itself gets a non-zero quality reading.
        # (Previously this used confirm_signal, meaning every fresh-crossover bar
        # had quality=0 because confirm_signal was still "NONE" on that bar.)
        active_signal = signal if signal != "NONE" else confirm_signal
        quality = 0
        if active_signal != "NONE":
            quality += 1  # Crossover present (current or confirmed)

            # Short slope in signal direction
            if active_signal == "CE" and short_slope > 0:
                quality += 1
            elif active_signal == "PE" and short_slope < 0:
                quality += 1

            # Long slope in signal direction
            if active_signal == "CE" and long_slope > 0:
                quality += 1
            elif active_signal == "PE" and long_slope < 0:
                quality += 1

            # VMA spread >= ATR * 0.5 (lines separating)
            if vma_spread >= round(atr * 0.5, 4):
                quality += 1

            # RSI confirms (CE: RSI > 55, PE: RSI < 45)
            if active_signal == "CE" and rsi > 55:
                quality += 1
            elif active_signal == "PE" and rsi < 45:
                quality += 1

        results.append({
            "timestamp":      r["timestamp"],
            "open":           round(r["open"],  4),
            "high":           round(r["high"],  4),
            "low":            round(r["low"],   4),
            "close":          round(r["close"], 4),
            "short_vma":      sv,
            "long_vma":       lv,
            "signal":         signal,      # CE / PE / NONE
            "confirm_signal": confirm_signal,
            "svma_trend":     svma_trend,  # UP / DOWN / FLAT
            "position":       position,    # ABOVE / BELOW / CROSS
            "atr":            atr,
            "rsi":            rsi,
            "upper_band":     upper_band,
            "lower_band":     lower_band,
            "is_sideways":    is_sideways,
            "short_slope":    short_slope,
            "long_slope":     long_slope,
            "quality":        quality,
        })

    return results


@app.route("/api/dual-vma")
def api_dual_vma():
    """
    Dual VMA crossover endpoint.

    Query params
    ────────────
    tf        : '1min' | '3min' | '5min'   (default '1min')
    short_len : int  (default 5)
    long_len  : int  (default 20)
    limit     : int  bars to fetch (default 2000)
    """
    timeframe = request.args.get("tf",        "3min")
    short_len = int(request.args.get("short_len", 9))
    long_len  = int(request.args.get("long_len",  21))
    limit     = int(request.args.get("limit",    2000))

    if short_len <= 0 or long_len <= 0:
        return jsonify({"ok": False, "error": "lengths must be > 0"}), 400
    if short_len >= long_len:
        return jsonify({"ok": False, "error": "short_len must be < long_len"}), 400

    try:
        rows = fetch_closes(timeframe, limit=limit)
        data = compute_dual_vma(rows, short_len, long_len)

        # ── VMA Warm-up Filter ──────────────────────────────────────────────────
        # compute_dual_vma runs over ALL fetched bars so the VMA is properly
        # "warmed up" by historical data before the current session begins.
        # We then restrict the history returned to the frontend to TODAY's IST
        # bars only, so the signals visible on the dashboard match TradingView
        # (which also has a full history warm-up).  Without this filter, the
        # first 10-15 bars of the day would show incorrect/delayed signals
        # because the VMA had not yet converged.
        today_ist_str = datetime.now(IST).strftime("%Y-%m-%d")  # e.g. "2026-06-10"

        def _is_today_ist(ts_str) -> bool:
            """Return True when the bar's timestamp falls on today (IST)."""
            if not ts_str:
                return False
            ts = str(ts_str).strip()
            # Fast path: most timestamps from MongoDB are "YYYY-MM-DD ..." or "YYYY-MM-DDTHH:..."
            if ts.startswith(today_ist_str):
                return True
            # Slow path: full parse for ISO strings with timezone info
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.astimezone(IST).strftime("%Y-%m-%d") == today_ist_str
            except Exception:
                return False

        today_data = [bar for bar in data if _is_today_ist(bar.get("timestamp"))]
        # If no today bars found (e.g. weekend / testing), fall back to all data
        history_data = today_data if today_data else data

        last = history_data[-1] if history_data else (data[-1] if data else {})
        prev = history_data[-2] if len(history_data) > 1 else last

        # count crossover signals in today's history only
        ce_signals = sum(1 for d in history_data if d["signal"] == "CE")
        pe_signals = sum(1 for d in history_data if d["signal"] == "PE")

        response = {
            "ok":         True,
            "timeframe":  timeframe,
            "short_len":  short_len,
            "long_len":   long_len,
            "total_bars": len(history_data),
            "ce_signals": ce_signals,
            "pe_signals": pe_signals,
            "current": {
                "timestamp":      last.get("timestamp"),
                "close":          last.get("close"),
                "short_vma":      last.get("short_vma"),
                "long_vma":       last.get("long_vma"),
                "signal":         last.get("signal"),
                "confirm_signal": last.get("confirm_signal"),
                "svma_trend":     last.get("svma_trend"),
                "position":       last.get("position"),
                "prev_short":     prev.get("short_vma"),
                "prev_long":      prev.get("long_vma"),
                "atr":            last.get("atr"),
                "rsi":            last.get("rsi"),
                "upper_band":     last.get("upper_band"),
                "lower_band":     last.get("lower_band"),
                "is_sideways":    last.get("is_sideways"),
                "quality":        last.get("quality"),
            },
            "history": history_data,     # today's bars only — VMA already warmed up
        }
        save_vma_result_snapshot({
            "kind": "dual_vma",
            "timeframe": timeframe,
            "short_len": short_len,
            "long_len": long_len,
            "total_bars": len(history_data),
            "ce_signals": ce_signals,
            "pe_signals": pe_signals,
            "current": response["current"],
        })
        return jsonify(response)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/vma-trades", methods=["GET", "POST"])
def api_vma_trades():
    if request.method == "GET":
        try:
            limit = max(1, min(int(request.args.get("limit", "100")), 500))
        except ValueError:
            return jsonify({"ok": False, "error": "limit must be a number"}), 400

        try:
            trades = fetch_saved_vma_trades(limit=limit)
            return jsonify({"ok": True, "trades": trades, "count": len(trades)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    payload = request.get_json(silent=True) or {}
    trades = payload.get("trades") or []
    if not isinstance(trades, list):
        return jsonify({"ok": False, "error": "trades must be a list"}), 400

    try:
        summary = save_vma_trades(payload)
        return jsonify({"ok": True, **summary})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/vma-active-trade", methods=["GET", "POST"])
def api_vma_active_trade():
    if request.method == "GET":
        try:
            active_trade = fetch_active_vma_trade()
            return jsonify({"ok": True, "active_trade": active_trade})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "ACTIVE").upper()
    if not isinstance(payload.get("position"), dict) and status == "ACTIVE":
        return jsonify({"ok": False, "error": "position is required for ACTIVE status"}), 400

    try:
        summary = save_active_vma_trade(payload)
        return jsonify({"ok": True, **summary})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/angel/option-ltp")
def api_angel_option_ltp():
    side = request.args.get("side", "CE").upper()
    try:
        spot = float(request.args.get("spot", "0"))
    except ValueError:
        return jsonify({"ok": False, "error": "spot must be a number"}), 400

    if side not in {"CE", "PE"}:
        return jsonify({"ok": False, "error": "side must be CE or PE"}), 400
    if spot <= 0:
        return jsonify({"ok": False, "error": "spot must be greater than 0"}), 400

    try:
        data = angel_client.get_nifty_option_ltp(side, spot)
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/angel/ltp")
def api_angel_ltp():
    exchange = request.args.get("exchange", "").strip().upper()
    tradingsymbol = request.args.get("tradingsymbol", "").strip().upper()
    symboltoken = request.args.get("symboltoken", "").strip()

    if not exchange or not tradingsymbol or not symboltoken:
        return jsonify({"ok": False, "error": "exchange, tradingsymbol and symboltoken are required"}), 400

    try:
        ltp = angel_client.get_ltp(exchange, tradingsymbol, symboltoken)
        return jsonify({
            "ok": True,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "symboltoken": symboltoken,
            "ltp": round(ltp, 2),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "env": os.getenv("ENV", "dev"),
            "mongo_configured": bool(MONGO_URI),
            "angel_configured": angel_client.is_configured(),
        }
    )


@app.route("/api/status")
def api_status():
    return jsonify(
        {
            "status": "ok",
            "env": os.getenv("ENV", "dev"),
            "service": "vma-dashboard",
        }
    )


@app.route("/assets/<path:filename>")
def assets(filename: str):
    return send_from_directory(BASE_DIR, filename)


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(BASE_DIR, "service-worker.js")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.after_request
def apply_cache_headers(response):
    if request.path.startswith("/api/") or request.path == "/":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    elif request.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": f"API route not found: {request.path}"}), 404
    return error


@app.errorhandler(405)
def method_not_allowed(error):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": f"Method not allowed for API route: {request.path}"}), 405
    return error


HOST = os.getenv("API_HOST", "127.0.0.1")
PORT = int(os.getenv("API_PORT", "5011"))
URL  = f"http://{HOST}:{PORT}"


def _wait_and_open(url: str, timeout: int = 10):
    """Poll until the server is up, then open the browser."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.2)
    webbrowser.open(url)


def open_frontend():
    """
    Open the VMA dashboard in the default browser.

    - If the Flask server is already running on port 5011, just opens the URL.
    - If the server is NOT running, starts it in a background thread first,
      waits until it is ready, then opens the browser.

    Usage:
        from app import open_frontend
        open_frontend()
    """
    already_up = False
    try:
        urllib.request.urlopen(URL, timeout=1)
        already_up = True
    except Exception:
        pass

    if already_up:
        print(f"Server already running -> opening {URL}")
        webbrowser.open(URL)
    else:
        print(f"Starting VMA server on {URL} ...")
        server_thread = threading.Thread(
            target=lambda: app.run(host=HOST, port=PORT, debug=False, use_reloader=False),
            daemon=True,
        )
        server_thread.start()
        _wait_and_open(URL)
        print(f"Dashboard opened in browser -> {URL}")
        server_thread.join()


def run_server():
    """Start the Flask server in the foreground (blocking). Used by __main__."""
    os.makedirs(BASE_DIR / "static", exist_ok=True)
    print(f"VMA Flask server -> {URL}")
    print("Press Ctrl+C to stop.\n")
    app.run(host=HOST, port=PORT, debug=True)


if __name__ == "__main__":
    os.makedirs(BASE_DIR / "static", exist_ok=True)
    open_frontend()
