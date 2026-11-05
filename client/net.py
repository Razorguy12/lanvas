"""Bridges the socket thread to Qt: network events arrive on the UI thread as queued signals."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from client.transport import NetCore
from common.identity import Identity


class NetWorker(QObject):
    """Owns a NetCore. The thread itself lives in NetCore so that headless tests can use it too."""

    event = pyqtSignal(str, object)

    def __init__(
        self, host: str, port: int, identity: Identity, username: str, hello: Any, receiver: Any
    ) -> None:
        super().__init__()
        # Queued explicitly: slots must run on the UI thread, whichever thread emits the signal.
        self.event.connect(receiver, Qt.ConnectionType.QueuedConnection)
        self.core = NetCore(host, port, identity, username, self.event.emit, hello)

    def start(self) -> None:
        self.core.start()

    def stop(self) -> None:
        self.core.stop()

    def send(self, msg_type: int, payload: dict[str, Any]) -> None:
        self.core.send(msg_type, payload)
