from __future__ import annotations


class StreamManager:
    def __init__(self) -> None:
        self.connected_clients = 0

    def register(self) -> None:
        self.connected_clients += 1

    def unregister(self) -> None:
        self.connected_clients = max(0, self.connected_clients - 1)
