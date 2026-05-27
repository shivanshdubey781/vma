from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient

from backend.config import settings


TIME_FIELDS = ("timestamp_ist", "timestamp", "time", "datetime", "date")


class MongoRepository:
    def __init__(self, uri: str | None = None, database: str | None = None) -> None:
        self.client = MongoClient(
            uri or settings.mongo.uri,
            serverSelectionTimeoutMS=8000,
        )
        self.db = self.client[database or settings.mongo.database]

    def ensure_indexes(self) -> None:
        for collection in settings.mongo.timeframe_collections.values():
            self.db[collection].create_index([("timestamp", DESCENDING)])
            self.db[collection].create_index([("symbol", ASCENDING), ("timestamp", DESCENDING)])

        self.db[settings.mongo.signals_collection].create_index(
            [("symbol", ASCENDING), ("timestamp", DESCENDING)]
        )
        self.db[settings.mongo.trades_collection].create_index(
            [("symbol", ASCENDING), ("entry_time", DESCENDING)]
        )
        self.db[settings.mongo.positions_collection].create_index(
            [("symbol", ASCENDING), ("status", ASCENDING)]
        )
        self.db[settings.mongo.performance_collection].create_index(
            [("symbol", ASCENDING), ("generated_at", DESCENDING)]
        )
        self.db[settings.mongo.alerts_collection].create_index([("created_at", DESCENDING)])
        self.db[settings.mongo.logs_collection].create_index([("created_at", DESCENDING)])

    def fetch_ohlc(self, timeframe: str = "5min", limit: int = 2000) -> list[dict[str, Any]]:
        collection_name = settings.mongo.timeframe_collections.get(timeframe)
        if not collection_name:
            raise ValueError(f"Unknown timeframe: {timeframe}")

        collection = self.db[collection_name]
        sample = collection.find_one()
        ts_field = self._pick_time_field(sample) if sample else None
        sort_desc = [(ts_field, -1)] if ts_field else [("_id", -1)]
        docs = list(collection.find({}, {"_id": 0}).sort(sort_desc).limit(limit))
        docs.reverse()
        return [self._normalize_ohlc(doc) for doc in docs]

    def fetch_ohlc5(self, limit: int = 2000) -> list[dict[str, Any]]:
        return self.fetch_ohlc("5min", limit=limit)

    def insert_one(self, collection_name: str, payload: Any) -> None:
        document = asdict(payload) if is_dataclass(payload) else payload
        self.db[collection_name].insert_one(document)

    def insert_many(self, collection_name: str, payloads: list[Any]) -> None:
        documents = [asdict(item) if is_dataclass(item) else item for item in payloads]
        if documents:
            self.db[collection_name].insert_many(documents)

    def replace_position(self, symbol: str, payload: Any) -> None:
        document = asdict(payload) if is_dataclass(payload) else payload
        document["symbol"] = symbol
        self.db[settings.mongo.positions_collection].replace_one(
            {"symbol": symbol},
            document,
            upsert=True,
        )

    @staticmethod
    def _pick_time_field(document: dict[str, Any] | None) -> str | None:
        if not document:
            return None
        key_map = {key.lower(): key for key in document}
        for field in TIME_FIELDS:
            if field in key_map:
                return key_map[field]
        return None

    @staticmethod
    def _normalize_ohlc(document: dict[str, Any]) -> dict[str, Any]:
        norm = {k.lower(): v for k, v in document.items()}
        timestamp = None
        for field in TIME_FIELDS:
            if norm.get(field):
                timestamp = str(norm[field])
                break

        return {
            "timestamp": timestamp,
            "open": float(norm.get("open", 0) or 0),
            "high": float(norm.get("high", 0) or 0),
            "low": float(norm.get("low", 0) or 0),
            "close": float(norm.get("close", 0) or 0),
            "volume": float(norm.get("volume", 0) or 0),
        }
