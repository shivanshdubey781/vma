from __future__ import annotations

from backend.models import AlertEvent


class Notifier:
    def publish(self, event: AlertEvent) -> dict[str, object]:
        return {
            "delivered": False,
            "message": "Notifier scaffold ready for Telegram, Discord, and webhook delivery",
            "event": event.title,
        }
