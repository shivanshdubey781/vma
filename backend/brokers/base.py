from __future__ import annotations

from abc import ABC, abstractmethod


class BrokerAdapter(ABC):
    @abstractmethod
    def place_order(self, payload: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def modify_order(self, order_id: str, payload: dict[str, object]) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def fetch_positions(self) -> list[dict[str, object]]:
        raise NotImplementedError

    @abstractmethod
    def fetch_orders(self) -> list[dict[str, object]]:
        raise NotImplementedError
