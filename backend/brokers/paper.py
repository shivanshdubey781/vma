from __future__ import annotations

from backend.brokers.base import BrokerAdapter


class PaperBrokerAdapter(BrokerAdapter):
    def place_order(self, payload: dict[str, object]) -> dict[str, object]:
        return {"status": "accepted", "mode": "paper", "payload": payload}

    def modify_order(self, order_id: str, payload: dict[str, object]) -> dict[str, object]:
        return {"status": "modified", "order_id": order_id, "payload": payload}

    def cancel_order(self, order_id: str) -> dict[str, object]:
        return {"status": "cancelled", "order_id": order_id}

    def fetch_positions(self) -> list[dict[str, object]]:
        return []

    def fetch_orders(self) -> list[dict[str, object]]:
        return []
