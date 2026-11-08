"""The application window: launcher, request-access screen and board, plus the join lifecycle."""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMainWindow, QStackedWidget

from client import dialogs, paths
from client.boardlist import BoardEntry
from client.boardpage import BoardPage
from client.discovery import DiscoveryWorker
from client.launcher import LauncherPage
from client.net import NetWorker
from client.recovery import CrashedSession, discard_crashed, find_crashed
from client.request_screen import STATE_DENIED, RequestAccessPage
from client.session import Session
from client.widgets import ToastHost
from common import durable
from common.identity import Identity
from common.protocol import UDP_PORT, ErrorCode

log = logging.getLogger(__name__)

# How long a new join may go unanswered. An established session is different: that one keeps
# retrying indefinitely, because the person already has work open and the server may come back.
CONNECT_GIVE_UP_MS = 8000

ERROR_TEXT = {
    ErrorCode.ROOM_NOT_FOUND: "That room no longer exists on this server.",
    ErrorCode.CODE_REQUIRED: "This room needs its room code.",
    ErrorCode.KICKED: "The owner removed you from this room.",
    ErrorCode.REVOKED: "The owner revoked your access to this room.",
    ErrorCode.RATE_LIMITED: "Too many requests from this address. Try again in a minute.",
    ErrorCode.BAD_VERSION: "That server runs a different version of LANvas.",
}


class AppWindow(QMainWindow, ToastHost):
    def __init__(self, udp_port: int = UDP_PORT, start_discovery: bool = True) -> None:
        super().__init__()
        self.setWindowTitle("LANvas")
        self.resize(1360, 860)
        self.identity = Identity.load_or_create(paths.identity_file())
        self.settings = self._load_settings()
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.launcher = LauncherPage(self.settings.get("username", ""))
        self.request_page = RequestAccessPage()
        self.stack.addWidget(self.launcher)
        self.stack.addWidget(self.request_page)
        self.session: Session | None = None
        self.net: NetWorker | None = None
        self.board: BoardPage | None = None
        self.hello: dict[str, Any] = {}
        self.target: tuple[str, int] = ("", 0)
        self.answered = False
        self.crashed: list[CrashedSession] = find_crashed(paths.journal_dir())
        self.adopting: CrashedSession | None = None
        self.seed_objects: list[dict[str, Any]] | None = None
        self.boards: list[BoardEntry] = []
        self.servers: list[Any] = []
        self.discovery = DiscoveryWorker(udp_port)
        self.discovery.boards_changed.connect(self._boards_changed)
        self.discovery.servers_changed.connect(self._servers_changed)
        if start_discovery:
            self.discovery.start()
        self.launcher.join_board.connect(self.join_board)
        self.launcher.join_by_address.connect(self.join_by_address)
        self.launcher.create_room.connect(self.create_room)
        self.launcher.refresh.connect(self.discovery.refresh)
        self.launcher.recover.connect(self.recover)
        self.launcher.name_changed.connect(self._name_changed)
        self.request_page.cancel.connect(self.leave)
        self.request_page.retry.connect(self._reconnect_same)
        self._show_recovery_banner()

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> dict[str, Any]:
        value, _ = durable.read_json_with_bak(paths.settings_file())
        return value if isinstance(value, dict) else {}

    def _name_changed(self, name: str) -> None:
        self.settings["username"] = name
        try:
            durable.atomic_write_json(paths.settings_file(), self.settings)
        except OSError:
            log.warning("could not save settings")

    # ------------------------------------------------------------------ discovery
    def _boards_changed(self, boards: list[BoardEntry]) -> None:
        self.boards = boards
        self.launcher.set_boards(boards)
        if self.board is not None:
            self.board.boards = boards

    def _servers_changed(self, servers: list[Any]) -> None:
        self.servers = servers

    # ------------------------------------------------------------------ joining
    def join_board(self, entry: BoardEntry, code: str) -> None:
        hello: dict[str, Any] = {"room_id": entry.room_id}
        if code:
            hello["room_code"] = code
        self._leave_current()
        self.request_page.set_info(entry.name, entry.host_name, self.identity.fingerprint)
        self.connect_to(entry.address, entry.tcp_port, hello)

    def join_by_address(self) -> None:
        dialog = dialogs.JoinByAddressDialog(self)
        if not dialog.exec():
            return
        hello: dict[str, Any] = {}
        code = dialog.code.text().strip()
        if code:
            hello["room_code"] = code
        invite = dialog.invite.text().strip()
        if invite:
            hello["invite"] = invite
        self.connect_to(dialog.host.text().strip(), dialog.port.value(), hello)

    def create_room(self, name: str = "", objects: list[dict[str, Any]] | None = None) -> None:
        dialog = dialogs.CreateRoomDialog(self.servers, self, name)
        if not dialog.exec():
            return
        host, port = dialog.target()
        hello = {
            "create": {
                "name": dialog.name.text().strip(),
                "code": dialog.code.text().strip(),
                "discoverable": dialog.discoverable.isChecked(),
                "require_code": dialog.require_code.isChecked(),
            }
        }
        self.seed_objects = objects
        self.connect_to(host, port, hello)

    def connect_to(self, host: str, port: int, hello: dict[str, Any]) -> None:
        """Start a session. The board page only appears once the server has let us in."""
        self._leave_current()
        self.launcher.show_message("")
        self.request_page.show_connecting(f"{host}:{port}")
        self.stack.setCurrentWidget(self.request_page)
        username = self.launcher.username()
        self.hello = hello
        self.target = (host, port)
        self.answered = False
        session = Session(
            self.identity,
            username,
            send=self._send,
            emit=self._on_session_event,
            journal_root=paths.journal_dir(),
            host=host,
            port=port,
        )
        self.session = session
        if self.adopting is not None:
            session.adopt_crashed(self.adopting)
        self.net = NetWorker(
            host,
            port,
            self.identity,
            username,
            lambda: session.hello_fields(self.hello),
            self._on_net_event,
        )
        self.board = BoardPage(session, lambda: self.net.core if self.net else None, self.discovery)
        self.board.boards = self.boards
        self.board.leave_requested.connect(self.leave)
        self.board.switch_requested.connect(self.join_board)
        self.stack.addWidget(self.board)
        self.net.start()
        QTimer.singleShot(CONNECT_GIVE_UP_MS, lambda s=session: self._connect_timed_out(s))

    def _connect_timed_out(self, session: Session) -> None:
        """Without this, an unreachable address would look like an app that ignored the click."""
        if session is not self.session or session.welcomed or self.answered:
            return
        host, port = self.target
        self.adopting = None  # the crashed session's journal is untouched and can be retried
        self._back_to_launcher(
            f"Could not reach a LANvas server at {host}:{port}. "
            "Check the address, the Wi-Fi network, and the firewall (TCP 47801)."
        )

    def _send(self, msg_type: int, payload: dict[str, Any]) -> None:
        if self.net is not None:
            self.net.send(msg_type, payload)

    def _on_net_event(self, kind: str, data: Any) -> None:
        """Always on the UI thread: NetWorker's signal is a queued connection."""
        if self.session is None:
            return
        if kind == "state":
            self.session.on_state(data)
            if (
                data == "closed"
                and not self.session.welcomed
                and self.stack.currentWidget() is self.launcher
            ):
                self._back_to_launcher("Could not reach that server.")
        elif kind == "frame":
            self.session.handle_frame(*data)
        elif kind == "rtt" and self.board is not None:
            self.board.on_rtt(data)

    def _on_session_event(self, name: str, **kw: Any) -> None:
        if name in ("access_pending", "welcome", "error"):
            self.answered = True
        if name == "access_pending":
            msg = kw["msg"]
            self.request_page.set_info(
                msg.get("room_name", ""), msg.get("host_name", ""), self.identity.fingerprint
            )
            self.stack.setCurrentWidget(self.request_page)
            return
        if name == "welcome":
            self._entered_room(kw)
        elif name == "error" and self._handle_fatal(kw.get("code", ""), kw.get("msg", {})):
            return
        if self.board is not None:
            self.board.on_event(name, **kw)

    def _entered_room(self, kw: dict[str, Any]) -> None:
        if self.board is None or self.session is None:
            return
        self.stack.setCurrentWidget(self.board)
        self.setWindowTitle(f"{self.session.room.get('name', '')} — LANvas")
        if self.adopting is not None:
            self._forget_adopted()
        if self.seed_objects is not None and kw.get("first"):
            self.session.replace_board(self.seed_objects)
            self.seed_objects = None

    def _forget_adopted(self) -> None:
        """Remove only the dead session's lock. The journal directory now belongs to this
        session."""
        adopted = self.adopting
        self.adopting = None
        if adopted is None:
            return
        for lock in adopted.directory.glob("session-*.lock"):
            if self.session and self.session.journal and lock == self.session.journal.lock_path:
                continue
            lock.unlink(missing_ok=True)
        self.crashed = [c for c in self.crashed if c.directory != adopted.directory]
        self._show_recovery_banner()

    def _handle_fatal(self, code: str, msg: dict[str, Any]) -> bool:
        if code == ErrorCode.ACCESS_DENIED:
            self.request_page.show_state(STATE_DENIED, msg.get("retry_after"))
            self.stack.setCurrentWidget(self.request_page)
            return True
        if msg.get("fatal") is False:
            return False
        text = ERROR_TEXT.get(code)
        if text is None:
            return False
        was_inside = self.session is not None and self.session.welcomed
        self._back_to_launcher(text)
        if was_inside:
            self.show_toast(text)
        return True

    def _reconnect_same(self) -> None:
        host, port = self.target
        self.connect_to(host, port, self.hello)

    def _leave_current(self) -> None:
        if self.net is not None:
            self.net.stop()
            self.net = None
        if self.session is not None:
            # Unsent work keeps its journal, so leaving while offline never silently discards it.
            # The next launch finds the lock of a finished process and offers recovery.
            has_unsent = bool(self.session.doc.pending or self.session.chat.outbox)
            self.session.close(clean=not has_unsent)
            self.session = None
        if self.board is not None:
            self.board.shutdown()
            self.stack.removeWidget(self.board)
            self.board.deleteLater()
            self.board = None

    def _back_to_launcher(self, message: str = "") -> None:
        self._leave_current()
        self.setWindowTitle("LANvas")
        self.stack.setCurrentWidget(self.launcher)
        self.launcher.show_message(message)

    def leave(self) -> None:
        self._back_to_launcher()

    # ------------------------------------------------------------------ recovery
    def _show_recovery_banner(self) -> None:
        if self.crashed:
            first = self.crashed[0]
            self.launcher.set_recovery(
                f"LANvas didn't close properly last time. "
                f"{first.room_name or first.room_id} may have unsaved work."
            )
        else:
            self.launcher.set_recovery(None)

    def recover(self) -> None:
        if not self.crashed:
            return
        crashed = self.crashed[0]
        dialog = dialogs.RecoverDialog(crashed, self)
        if not dialog.exec():
            return
        if dialog.choice == dialogs.RecoverDialog.RECOVER:
            self.adopting = crashed
            self.connect_to(crashed.host, crashed.port, {"room_id": crashed.room_id})
        elif dialog.choice == dialogs.RecoverDialog.DISCARD:
            self._discard(crashed)

    def _discard(self, crashed: CrashedSession) -> None:
        discard_crashed(crashed)
        self.crashed = [c for c in self.crashed if c is not crashed]
        self._show_recovery_banner()

    # ------------------------------------------------------------------ window
    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self.layout_toasts()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt override)
        self._leave_current()
        self.discovery.stop()
        super().closeEvent(event)
