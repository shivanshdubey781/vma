from __future__ import annotations


class MarketStreamGateway:
    def connect_message(self) -> dict[str, str]:
        return {
            "status": "ready",
            "mode": "simulation-only",
            "message": "Websocket layer is prepared for future live feed adapters, but no live trading is enabled.",
        }
