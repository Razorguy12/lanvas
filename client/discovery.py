"""The live board list: a background thread listening for announces (Architecture.md section
4.4)."""

from __future__ import annotations

import json
import logging
import selectors
import socket
import time

from PyQt6.QtCore import QThread, pyqtSignal

from client.boardlist import BoardTracker
from common import netutil
from common.protocol import DISCOVER, PROTOCOL_VERSION, UDP_PORT

log = logging.getLogger(__name__)

INTERFACE_POLL = 5.0
TICK = 0.25


class DiscoveryWorker(QThread):
    boards_changed = pyqtSignal(list)
    servers_changed = pyqtSignal(list)

    def __init__(self, port: int = UDP_PORT, extra_targets: list[tuple[str, int]] | None = None):
        super().__init__()
        self.port = port
        self.extra_targets = extra_targets or []
        self.tracker = BoardTracker()
        self._refresh = False
        self._stopping = False

    def refresh(self) -> None:
        """Safe from the UI thread: only sets a flag that the worker checks on its next tick."""
        self._refresh = True

    def stop(self) -> None:
        self._stopping = True
        self.wait(2000)

    def _send_discover(self, sock: socket.socket) -> None:
        packet = json.dumps({"t": DISCOVER, "protocol_version": PROTOCOL_VERSION}).encode()
        targets = [(b, self.port) for b in netutil.broadcast_targets()]
        # A unicast to this host as well: some systems do not loop broadcasts back to the sender.
        targets.append(("127.0.0.1", self.port))
        targets.extend(self.extra_targets)
        for target in targets:
            try:
                sock.sendto(packet, target)
            except OSError:
                pass  # unreachable interface or no route; other targets may still work

    def run(self) -> None:
        selector = selectors.DefaultSelector()
        # DISCOVER goes out from its own ephemeral port. A reply sent to the shared port could be
        # load-balanced by SO_REUSEPORT to a same-host server, which would never answer it.
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sender.bind(("", 0))
        sender.setblocking(False)
        selector.register(sender, selectors.EVENT_READ)
        listener: socket.socket | None = None
        try:
            listener = netutil.open_discovery_socket(self.port)
            selector.register(listener, selectors.EVENT_READ)
        except OSError as exc:
            log.warning("cannot bind discovery port %s: %s", self.port, exc)

        self._send_discover(sender)
        interfaces = netutil.list_interfaces()
        next_poll = time.monotonic() + INTERFACE_POLL
        last_boards: list = []
        last_servers: list = []
        try:
            while not self._stopping:
                for key, _ in selector.select(TICK):
                    try:
                        data, source = key.fileobj.recvfrom(2048)  # type: ignore[union-attr]
                    except OSError:
                        continue
                    self.tracker.feed(data, source[0], time.time())
                now = time.monotonic()
                if self._refresh:
                    self._refresh = False
                    self._send_discover(sender)
                if now >= next_poll:
                    next_poll = now + INTERFACE_POLL
                    current = netutil.list_interfaces()
                    if current != interfaces:
                        # A device that just joined the network should not wait for an announce.
                        interfaces = current
                        self._send_discover(sender)
                self.tracker.expire(time.time())
                boards, servers = self.tracker.boards(), self.tracker.servers()
                if boards != last_boards:
                    last_boards = boards
                    self.boards_changed.emit(boards)
                if servers != last_servers:
                    last_servers = servers
                    self.servers_changed.emit(servers)
        finally:
            selector.close()
            sender.close()
            if listener is not None:
                listener.close()
