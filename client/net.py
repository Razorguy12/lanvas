"""Bridges the socket thread to Tk: network events reach the UI thread through the UI queue."""

from __future__ import annotations

from typing import Any

from client.transport import NetCore
from client.widgets import call_in_ui
from common.identity import Identity


class NetWorker:
    """Owns a NetCore. The thread itself lives in NetCore so that headless tests can use it too."""

    def __init__(
        self, host: str, port: int, identity: Identity, username: str, hello: Any, receiver: Any
    ) -> None:
        self.receiver = receiver
        self.stopped = False
        self.core = NetCore(host, port, identity, username, self._emit, hello)

    def _emit(self, kind: str, data: Any) -> None:
        # Called on the socket thread. Slots must run on the UI thread, whichever thread emits.
        call_in_ui(lambda: None if self.stopped else self.receiver(kind, data))

    def start(self) -> None:
        self.core.start()

    def stop(self) -> None:
        self.stopped = True
        self.core.stop()

    def send(self, msg_type: int, payload: dict[str, Any]) -> None:
        self.core.send(msg_type, payload)
