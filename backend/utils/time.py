from __future__ import annotations

from datetime import datetime


def session_key(timestamp: str) -> str:
    normalized = timestamp.replace("T", " ")
    if " " in normalized:
        return normalized.split(" ", 1)[0]
    return normalized[:10]


def parse_timestamp(timestamp: str) -> datetime:
    normalized = timestamp.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        if " " in timestamp:
            return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        return datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S")
