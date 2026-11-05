"""Offscreen UI tests: real widgets and a real server, driven through the same code as a user."""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PyQt6.QtCore import QEventLoop, QPoint, QPointF, Qt
from PyQt6.QtGui import QFontDatabase, QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from client import dialogs, paths, theme
from client.app import AppWindow
from client.boardlist import BoardEntry
from client.canvas.scene import BoardScene
from client.discovery import DiscoveryWorker
from client.export.pdf import export_pdf
from client.export.png import export_raster, raster_size
from client.export.renderer import SCOPE_FRAME, SCOPE_SELECTION, Scope, drive
from client.export.svg import export_svg
from client.request_screen import STATE_DENIED
from common.fileformat import Trust, board_hash, build_document, decode_lanvas, encode_lanvas
from common.identity import Identity
from common.model import Board, apply_op
from common.protocol import MsgType
from common.simplify import simplify
from tests.harness import BotClient, add_member, make_room, start_server, wait_until

pytestmark = [pytest.mark.ui]


def spin(app, predicate, timeout=8.0):
    def check():
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        return predicate()

    return wait_until(check, timeout, step=0.005)


def _obj(board, obj_id, kind, **props):
    apply_op(board, {"kind": "create", "id": obj_id, "type": kind, "z": "V" + obj_id, "props": props})


def sample_board() -> Board:
    board = Board()
    _obj(board, "frame1", "frame", x=0, y=0, w=300, h=200, fill="#FFFFFF", stroke="#000000", stroke_w=1, name="F1")
    _obj(board, "frame2", "frame", x=400, y=0, w=300, h=200, fill="#FFFFFF", name="F2")
    _obj(board, "r", "rect", x=10, y=10, w=50, h=50, fill="#FF0000", stroke="#000000", stroke_w=2)
    _obj(board, "t", "text", x=420, y=20, w=200, h=40, text="Selectable words", font_size=18, fill="#000000")
    _obj(board, "e", "ellipse", x=100, y=100, w=40, h=40, fill="#00FF00")
    _obj(board, "p", "path", x=0, y=300, w=100, h=50, points=[0, 0, 50, 50, 100, 0], stroke="#0000FF", stroke_w=3)
    _obj(board, "a", "arrow", x=500, y=300, x2=100, y2=20, stroke="#000000", stroke_w=2)
    board.objects["r"]["parent"] = "frame1"
    board.objects["t"]["parent"] = "frame2"
    return board


def scene_for(board: Board) -> BoardScene:
    scene = BoardScene()
    scene.sync(board)
    return scene


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LANVAS_HOME", str(tmp_path / "home"))
    paths.set_profile("")
    return tmp_path / "home"


@pytest.fixture
def srv(tmp_path):
    port, handle = start_server(tmp_path / "server")
    yield port, handle
    handle.stop()


def make_window(qapp, home) -> AppWindow:
    theme.load_fonts()
    theme.apply(qapp)
    window = AppWindow(udp_port=0, start_discovery=False)
    window.launcher.name.setText("Tester")
    window.show()
    return window


@pytest.fixture
def window(qapp, home):
    win = make_window(qapp, home)
    yield win
    win.close()
    win.deleteLater()


def enter_as_owner(qapp, window, port, name="Sprint-1"):
    window.connect_to("127.0.0.1", port, {"create": {"name": name, "code": "CODE1"}})
    assert spin(qapp, lambda: window.session is not None and window.session.welcomed and window.stack.currentWidget() is window.board)


def canvas_point(canvas, x, y):
    return canvas.mapFromScene(QPointF(x, y))


def drag(canvas, a, b, steps=4):
    start, end = canvas_point(canvas, *a), canvas_point(canvas, *b)
    viewport = canvas.viewport()
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    for i in range(1, steps + 1):
        pos = QPoint(start.x() + (end.x() - start.x()) * i // steps, start.y() + (end.y() - start.y()) * i // steps)
        QTest.mouseMove(viewport, pos)
        event_buttons_move(canvas, pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)


def event_buttons_move(canvas, pos):
    """QTest.mouseMove sends no button state, so a drag move is delivered explicitly."""
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QMouseEvent

    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(pos),
        QPointF(canvas.viewport().mapToGlobal(pos)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(canvas.viewport(), event)


# ------------------------------------------------------------------ export
@pytest.mark.export
@pytest.mark.parametrize("scale", [1, 2, 4])
@pytest.mark.parametrize("transparent", [False, True])
def test_EX_01_png_size_and_alpha(qapp, scale, transparent):
    scene = scene_for(sample_board())
    width, height = raster_size(scene, Scope(), scale)
    data = drive(export_raster(scene, Scope(), "PNG", scale, transparent, "#ABCDEF"))
    image = QImage.fromData(data, "PNG")
    assert (image.width(), image.height()) == (width, height)
    corner = image.pixelColor(0, 0)
    if transparent:
        assert corner.alpha() == 0
    else:
        assert corner.alpha() == 255 and corner.name().upper() == "#ABCDEF"
    # The center of the red rectangle, located from the export bounds rather than a guessed offset.
    from client.export.renderer import bounds_of, collect_items

    rect = bounds_of(collect_items(scene, Scope()))
    red = image.pixelColor(round((35 - rect.x()) * scale), round((35 - rect.y()) * scale))
    assert red.alpha() == 255 and red.red() > 200 and red.green() < 60


@pytest.mark.export
def test_EX_01_export_contains_no_overlay_and_paints_objects(qapp):
    scene = scene_for(sample_board())
    data = drive(export_raster(scene, Scope(SCOPE_SELECTION, selection=frozenset({"e"})), "PNG", 1, False, "#FFFFFF"))
    image = QImage.fromData(data, "PNG")
    center = image.pixelColor(image.width() // 2, image.height() // 2)
    assert center.green() > 200 and center.red() < 60  # the green ellipse really was painted


@pytest.mark.export
def test_EX_02_jpeg_opens_background_filled(qapp):
    scene = scene_for(sample_board())
    data = drive(export_raster(scene, Scope(), "JPEG", 1, True, "#FFFFFF"))
    assert data[:2] == b"\xff\xd8"
    image = QImage.fromData(data, "JPEG")
    assert not image.isNull() and image.pixelColor(0, 0).alpha() == 255
    assert image.pixelColor(0, 0).lightness() > 240


@pytest.mark.export
def test_EX_03_svg_valid_xml_text_stays_text(qapp):
    scene = scene_for(sample_board())
    data = drive(export_svg(scene, Scope(), "Board"))
    root = ET.fromstring(data)
    assert root.tag.endswith("svg")
    assert b"Selectable words" in data
    assert b"http://" not in data.replace(b"http://www.w3.org", b"")  # nothing external, offline


@pytest.mark.export
def test_EX_03_svg_embeds_images_as_base64(qapp):
    board = Board()
    image = QImage(4, 4, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    from client.boardpage import encode_image

    _obj(board, "i", "image", x=0, y=0, w=40, h=40, image_data=encode_image(image))
    data = drive(export_svg(scene_for(board), Scope()))
    assert b"data:image" in data and b"base64" in data


def _pdf_pages(data: bytes) -> int:
    import re

    return len(re.findall(rb"/Type\s*/Page[^s]", data))


@pytest.mark.export
def test_EX_04_pdf_fit_and_each_frame_as_page(qapp):
    scene = scene_for(sample_board())
    fit = drive(export_pdf(scene, Scope(), "fit"))
    assert fit.startswith(b"%PDF") and _pdf_pages(fit) == 1
    each = drive(export_pdf(scene, Scope(), "fit", each_frame=True))
    assert _pdf_pages(each) == 2
    assert b"/Subtype /Image" not in fit  # vector, not one big raster


@pytest.mark.export
@pytest.mark.parametrize("page", ["A4", "A3"])
def test_EX_04_pdf_paper_sizes(qapp, page):
    data = drive(export_pdf(scene_for(sample_board()), Scope(), page))
    assert data.startswith(b"%PDF") and _pdf_pages(data) == 1


@pytest.mark.export
def test_EX_05_lanvas_round_trip_through_board_hash(qapp):
    board = sample_board()
    key = Identity.generate()
    raw = encode_lanvas(build_document(board, "R", 7, "t", key, "me"))
    loaded = decode_lanvas(raw)
    assert loaded.board_hash == board_hash(board) and loaded.trust is Trust.SIGNED


@pytest.mark.export
def test_EX_06_scope_selection_and_frame_only_those_objects(qapp):
    scene = scene_for(sample_board())
    from client.export.renderer import collect_items

    assert {i.obj_id for i in collect_items(scene, Scope(SCOPE_SELECTION, selection=frozenset({"e", "p"})))} == {"e", "p"}
    assert {i.obj_id for i in collect_items(scene, Scope(SCOPE_FRAME, frame_id="frame1"))} == {"frame1", "r"}
    frame_png = QImage.fromData(drive(export_raster(scene, Scope(SCOPE_FRAME, frame_id="frame1"))), "PNG")
    whole_png = QImage.fromData(drive(export_raster(scene, Scope())), "PNG")
    assert frame_png.width() < whole_png.width()


@pytest.mark.export
def test_EX_06_hidden_objects_and_comments_not_exported(qapp):
    board = sample_board()
    board.objects["e"]["props"]["visible"] = False
    _obj(board, "c", "comment", x=5, y=5, text="review note")
    from client.export.renderer import collect_items

    ids = {i.obj_id for i in collect_items(scene_for(board), Scope())}
    assert "e" not in ids and "c" not in ids


@pytest.mark.export
def test_EX_07_bundled_font_loaded_and_embedded(qapp):
    assert theme.load_fonts()
    assert theme.FONT_FAMILY in QFontDatabase.families()
    data = drive(export_pdf(scene_for(sample_board()), Scope(), "fit"))
    assert b"Inter" in data  # the PDF carries the bundled font itself, so it renders anywhere


@pytest.mark.export
def test_EX_07_no_runtime_network_code():
    """Static proof for "no internet": nothing in the shipped code can open a URL."""
    root = Path(__file__).resolve().parent.parent
    forbidden = ("urllib", "requests", "http.client", "httpx", "aiohttp", "QNetworkAccess", "socketserver", "asyncio", "websockets", "flask", "fastapi", "zmq")
    for folder in ("common", "server", "client"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for word in forbidden:
                assert f"import {word}" not in text and f"from {word}" not in text, f"{path}: {word}"


@pytest.mark.export
def test_EX_08_large_board_exports_in_chunks(qapp):
    board = Board()
    for i in range(1000):
        _obj(board, f"o{i:04d}", "rect", x=i % 40 * 30, y=i // 40 * 30, w=25, h=25, fill="#0D99FF")
    scene = scene_for(board)
    steps = export_raster(scene, Scope())
    ticks = []
    import time

    while True:
        begin = time.perf_counter()
        try:
            next(steps)
        except StopIteration as stop:
            data = stop.value
            break
        ticks.append(time.perf_counter() - begin)
    assert len(ticks) >= 20  # work is split, so the UI can run between chunks
    assert max(ticks) < 0.1, f"longest uninterrupted chunk {max(ticks):.3f}s"
    assert not QImage.fromData(data, "PNG").isNull()


@pytest.mark.export
def test_export_too_large_is_refused_with_explanation(qapp):
    from client.export.renderer import ExportError

    board = Board()
    _obj(board, "big", "rect", x=0, y=0, w=20000, h=100)
    with pytest.raises(ExportError, match="smaller scale"):
        drive(export_raster(scene_for(board), Scope(), "PNG", 1))


# ------------------------------------------------------------------ discovery worker with real UDP
@pytest.mark.discovery
def test_DS_01_worker_finds_board_with_discover(qapp, tmp_path):
    port, handle = start_server(tmp_path / "s", discovery=True, discovery_port=0, discovery_broadcast=False)
    owner = make_room(port, "Found-it")
    worker = DiscoveryWorker(handle.server.discovery.port)
    seen = []
    worker.boards_changed.connect(lambda boards: seen.append(boards))
    worker.start()
    try:
        assert spin(qapp, lambda: seen and seen[-1], timeout=2.0)
        entry = seen[-1][0]
        assert entry.name == "Found-it" and entry.room_id == owner.room_id and entry.compatible
    finally:
        worker.stop()
        handle.stop()


@pytest.mark.discovery
def test_DS_02_launcher_row_appears_and_disappears(qapp, home):
    window = make_window(qapp, home)
    try:
        entry = BoardEntry("s", "r1", "Sprint-1", "Lab-PC", "Alice", "10.0.0.1", 47801, 3, False, True, 2)
        window._boards_changed([entry])
        assert ("s", "r1") in window.launcher.rows and not window.launcher.empty.isVisible()
        window._boards_changed([])
        assert not window.launcher.rows and window.launcher.empty.isVisible()
    finally:
        window.close()


@pytest.mark.discovery
def test_DS_07_row_asks_for_code_inline(qapp):
    from client.launcher import BoardRow

    entry = BoardEntry("s", "r1", "Coded", "PC", "A", "10.0.0.1", 47801, None, True, True, 2)
    row = BoardRow(entry)
    row.show()
    got = []
    row.join.connect(lambda e, code: got.append(code))
    row.join_button.click()
    assert not got and row.code.isVisible()
    row.code.setText("ABC")
    row.join_button.click()
    assert got == ["ABC"]


@pytest.mark.discovery
def test_DS_08_incompatible_row_is_not_joinable(qapp):
    from client.launcher import BoardRow

    row = BoardRow(BoardEntry("s", "r", "Retro", "PC", "", "10.0.0.1", 47801, None, False, False, 1))
    assert not row.isEnabled() and not hasattr(row, "join_button")


# ------------------------------------------------------------------ whole flows
def test_full_flow_create_draw_chat_save(qapp, window, srv, home):
    port, handle = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    canvas = window.board.canvas
    canvas.set_tool("rect")
    drag(canvas, (10, 10), (110, 60))
    assert spin(qapp, lambda: session.doc.seq == 1 and not session.doc.pending)
    obj = session.doc.view.live()[0]
    assert obj["type"] == "rect" and round(obj["props"]["w"]) >= 95
    assert canvas.selection == {obj["id"]}

    canvas.set_tool("pen")
    drag(canvas, (0, 100), (200, 150), steps=20)
    assert spin(qapp, lambda: session.doc.seq == 2 and not session.doc.pending)
    room = handle.server.rooms[session.room_id]
    assert board_hash(room.board) == session.doc.hash()

    window.board.chat.input.setText("hello <b>world</b>")
    window.board.chat._send()
    assert spin(qapp, lambda: len(session.chat.room.messages) == 1)
    html_shown = window.board.chat.view.toHtml()
    assert "&lt;b&gt;world" in html_shown  # user text is never interpreted as markup

    window.board.save()
    assert spin(qapp, lambda: session.save_state == "saved")
    assert session.save_info["board_hash"] == session.doc.hash()
    assert "Saved" in window.board.save_chip.text()
    session.io.flush()
    assert list(paths.backups_dir(session.room_id).glob("*.lanvas"))


def test_R1_eraser_removes_whole_stroke_for_everyone(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    canvas = window.board.canvas
    watcher = add_member(_owner_bot(session, port), port, "w")
    canvas.set_tool("pen")
    drag(canvas, (0, 0), (100, 0), steps=10)
    assert spin(qapp, lambda: session.doc.seq == 1)
    canvas.set_tool("eraser")
    drag(canvas, (50, -5), (50, 5))
    assert spin(qapp, lambda: session.doc.seq == 2 and not session.doc.view.live())
    watcher.recv_until(2)
    assert watcher.board_hash() == session.doc.hash()


def _owner_bot(session, port):
    bot = BotClient(port, "owner-2nd-device", session.identity)
    bot.join(room_id=session.room_id)
    return bot


def test_select_move_resize_undo(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    canvas = window.board.canvas
    session.create("rect", {"x": 0, "y": 0, "w": 100, "h": 100, "fill": "#FF0000"}, obj_id="box")
    canvas.set_tool("select")
    drag(canvas, (50, 50), (150, 150))
    assert spin(qapp, lambda: not session.doc.pending)
    props = session.doc.view.objects["box"]["props"]
    assert abs(props["x"] - 100) < 10 and abs(props["y"] - 100) < 10
    assert len(session.doc.undo_stack) == 2  # one create, plus one step for the entire drag
    x_before = props["x"]
    # Bottom-right handle of the moved box.
    drag(canvas, (x_before + 100, props["y"] + 100), (x_before + 200, props["y"] + 200))
    assert spin(qapp, lambda: not session.doc.pending)
    assert session.doc.view.objects["box"]["props"]["w"] > 150
    session.undo()
    session.undo()
    assert spin(qapp, lambda: not session.doc.pending)
    assert session.doc.view.objects["box"]["props"]["x"] == 0
    assert session.doc.view.objects["box"]["props"]["w"] == 100


def test_marquee_shift_click_and_mixed_values(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    canvas = window.board.canvas
    session.create("rect", {"x": 0, "y": 0, "w": 50, "h": 50, "fill": "#FF0000"}, obj_id="a")
    session.create("rect", {"x": 100, "y": 0, "w": 50, "h": 50, "fill": "#00FF00"}, obj_id="b")
    drag(canvas, (-20, -20), (200, 100))
    assert canvas.selection == {"a", "b"}
    design = window.board.design
    design.refresh()
    assert design.fields["y"].value() == 0 and design.fields["x"].is_mixed()
    assert design.fields["x"].text() == "Mixed" and design.fill.mixed


def test_design_panel_edit_syncs_and_layers_reorder(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    canvas = window.board.canvas
    bot = _owner_bot(session, port)
    session.create("rect", {"x": 0, "y": 0, "w": 50, "h": 50}, obj_id="a")
    session.create("rect", {"x": 10, "y": 0, "w": 50, "h": 50}, obj_id="b")
    canvas.set_selection({"a"})
    design = window.board.design
    design.fields["x"].setValue(77)
    design._number_edited("x")
    assert spin(qapp, lambda: not session.doc.pending)
    bot.recv_until(session.doc.seq)
    assert bot.board.objects["a"]["props"]["x"] == 77
    layers = window.board.layers
    layers.refresh()
    assert layers.tree.topLevelItemCount() == 2
    top = layers.tree.topLevelItem(0)
    assert layers._object_id(top) == "b"  # top row is the highest z
    # Move "a" to the top of the list: its z has to end up above "b".
    moved = layers.tree.takeTopLevelItem(1)
    layers.tree.insertTopLevelItem(0, moved)
    layers._reordered(moved)
    assert spin(qapp, lambda: not session.doc.pending)
    assert session.doc.view.objects["a"]["z"] > session.doc.view.objects["b"]["z"]
    layers.refresh()
    assert layers._object_id(layers.tree.topLevelItem(0)) == "a"


def test_duplicate_group_delete_align_commands(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    canvas = window.board.canvas
    session.create("rect", {"x": 0, "y": 0, "w": 50, "h": 50}, obj_id="a")
    session.create("rect", {"x": 100, "y": 40, "w": 50, "h": 50}, obj_id="b")
    canvas.set_selection({"a"})
    canvas.duplicate_selected()
    assert len(session.doc.view.live()) == 3
    canvas.set_selection({"a", "b"})
    canvas.align("top")
    assert spin(qapp, lambda: not session.doc.pending)
    assert session.doc.view.objects["a"]["props"]["y"] == session.doc.view.objects["b"]["props"]["y"]
    canvas.group_selected()
    group_id = next(iter(canvas.selection))
    assert session.doc.view.objects[group_id]["type"] == "group"
    assert canvas.selection_root("a") == group_id  # clicking a member now picks the whole group
    canvas.delete_selected()
    assert spin(qapp, lambda: not session.doc.pending)
    assert {o["id"] for o in session.doc.view.live()} - {"a", "b", group_id}
    assert session.doc.view.get_live("a") is None and session.doc.view.get_live(group_id) is None


def test_viewer_tools_disabled_with_explanation(qapp, window, srv):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    room.acl.add_member(window.identity.fingerprint, "Tester", "viewer")
    window.connect_to("127.0.0.1", port, {"room_id": owner.room_id})
    assert spin(qapp, lambda: window.session and window.session.welcomed)
    assert spin(qapp, lambda: window.stack.currentWidget() is window.board)
    board = window.board
    assert not board.tool_buttons["pen"].isEnabled()
    assert "Viewers can't edit" in board.tool_buttons["pen"].toolTip()
    assert board.role_chip.text() == "VIEWER"
    board.canvas.set_tool("pen")
    assert board.canvas.tool.name == "select"


def test_request_access_screen_shows_nothing_then_approved(qapp, window, srv):
    port, _ = srv
    owner = make_room(port, "Secret room")
    owner.create_rect("hidden-object")
    owner.recv_until(1)
    window.connect_to("127.0.0.1", port, {"room_id": owner.room_id})
    assert spin(qapp, lambda: window.stack.currentWidget() is window.request_page)
    assert "Secret room" in window.request_page.room.text()
    assert "waiting" in window.request_page.state.text().lower()
    assert window.session.doc.view.objects == {}  # no board data held at all
    owner.wait_for(MsgType.ACCESS_REQUEST)
    owner.send(MsgType.ACCESS_DECISION, {"fingerprint": window.identity.fingerprint, "decision": "approve", "role": "editor"})
    assert spin(qapp, lambda: window.stack.currentWidget() is window.board)
    assert "hidden-object" in window.session.doc.view.objects


def test_request_denied_shows_cooldown(qapp, window, srv):
    port, _ = srv
    owner = make_room(port)
    window.connect_to("127.0.0.1", port, {"room_id": owner.room_id})
    assert spin(qapp, lambda: window.stack.currentWidget() is window.request_page)
    owner.wait_for(MsgType.ACCESS_REQUEST)
    owner.send(MsgType.ACCESS_DECISION, {"fingerprint": window.identity.fingerprint, "decision": "deny"})
    assert spin(qapp, lambda: "Denied" in window.request_page.state.text())
    assert "ask again in" in window.request_page.state.text()
    window.request_page.show_state(STATE_DENIED, None)
    assert "blocked" in window.request_page.state.text()


def test_owner_gets_toast_and_dialog_for_request(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    visitor = BotClient(port, "Alice")
    visitor.request(room_id=window.session.room_id)
    assert spin(qapp, lambda: window.session.pending_requests)
    assert "•1" in window.board.share_button.text()
    request = next(iter(window.session.pending_requests.values()))
    dialog = dialogs.AccessRequestDialog(request, "Sprint-1")
    dialog._choose("approve", "viewer")
    window.session.decide_access(request["fingerprint"], *dialog.decision)
    assert visitor.wait_for(MsgType.WELCOME, consume=False)["role"] == "viewer"


def test_private_message_ui_strip_and_badge(qapp, window, srv):
    port, handle = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    peer = add_member(_owner_bot(session, port), port, "Bob")
    assert spin(qapp, lambda: peer.fp in session.members)
    chat = window.board.chat
    window.board.tabs.setCurrentWidget(chat)
    peer.send_dm(session.fp, "secret hello")
    assert spin(qapp, lambda: session.chat.total_unread() == 1)
    assert "•1" in window.board.tabs.tabText(window.board.tabs.indexOf(chat))
    window.board.open_private(peer.fp)
    assert spin(qapp, lambda: session.chat.total_unread() == 0)
    assert "only you and Bob can read" in chat.strip.text() and chat.strip.isVisible()
    assert "secret hello" in chat.view.toPlainText()
    chat.input.setText("hi Bob")
    chat._send()
    peer.wait_for(MsgType.CHAT, lambda m: m["text"] == "hi Bob")


def test_muted_user_sees_disabled_input(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    window.session.muted = True
    window.board.chat.refresh()
    assert not window.board.chat.input.isEnabled()
    assert "muted" in window.board.chat.notice.text()


def test_reconnect_banner_and_editing_continue(qapp, window, srv):
    port, handle = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    room_id = session.room_id
    handle.kill()
    assert spin(qapp, lambda: not session.live)
    assert window.board.reconnect_banner.isVisible()
    session.create("rect", {"x": 1, "y": 1, "w": 5, "h": 5}, obj_id="offline")
    session.save()
    assert session.save_state == "local_only"
    assert "pending" in window.board.save_chip.text()
    handle.restart()
    assert spin(qapp, lambda: session.live and not session.doc.pending, timeout=15)
    assert spin(qapp, lambda: session.save_state == "saved", timeout=5)
    assert not window.board.reconnect_banner.isVisible()
    assert board_hash(handle.server.rooms[room_id].board) == session.doc.hash()


def test_inspector_lists_frames_and_chaos_controls(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    board = window.board
    board.toggle_inspector()
    session.create("rect", {"x": 1, "y": 1, "w": 5, "h": 5}, obj_id="r")
    assert spin(qapp, lambda: not session.doc.pending)
    board.inspector.refresh()
    kinds = {board.inspector.table.item(r, 2).text() for r in range(board.inspector.table.rowCount())}
    assert {"OP", "OP_ACK", "WELCOME"} <= kinds
    core = window.net.core
    board.inspector.latency.setValue(300)
    board.inspector.dup.setValue(20)
    assert core.chaos.latency == 0.3 and core.chaos.duplicate == 0.2
    board.inspector._reset()
    assert core.chaos.latency == 0


def test_inspector_hides_other_peoples_dm_text():
    from client.transport import scrub_private

    frame = {"channel_id": "dm:abcd", "from_fp": "A", "to_fp": "B", "text": "secret"}
    assert scrub_private(frame, "A")["text"] == "secret"
    assert scrub_private(frame, "C")["text"] == "«private, 6 chars»"
    assert scrub_private({"channel_id": "room", "text": "open"}, "C")["text"] == "open"


def test_open_file_dialog_trust_headers(qapp, window, tmp_path):
    key = Identity.generate()
    doc = build_document(sample_board(), "Signed", 1, "t", key, "Alice")
    good = tmp_path / "good.lanvas"
    good.write_bytes(encode_lanvas(doc))
    loaded = window.read_file(good)
    text, _ = dialogs.trust_text(loaded)
    assert text.startswith("✓ Signed by Alice")
    doc["board"]["objects"]["r"]["props"]["x"] = 999
    bad = tmp_path / "bad.lanvas"
    bad.write_bytes(encode_lanvas(doc))
    text, _ = dialogs.trust_text(window.read_file(bad))
    assert text.startswith("⚠ This file was modified")
    dialog = dialogs.OpenFileDialog(window.read_file(good))
    assert len(dialog.scene.items_by_id) == 7


def test_SV_12_create_room_from_file_through_window(qapp, window, srv, tmp_path):
    port, handle = srv
    board = sample_board()
    board.objects["e"]["deleted"] = True
    window.seed_objects = list(board.objects.values())
    window.connect_to("127.0.0.1", port, {"create": {"name": "From file"}})
    assert spin(qapp, lambda: window.session and window.session.welcomed and not window.session.doc.pending and window.session.doc.seq == 1)
    room = handle.server.rooms[window.session.room_id]
    assert board_hash(room.board) == board_hash(board) == window.session.doc.hash()


def test_SV_07_recover_banner_and_dialog(qapp, home, srv):
    from client.recovery import Journal

    port, handle = srv
    owner = make_room(port, "Sprint-1")
    room = handle.server.rooms[owner.room_id]
    journal = Journal(paths.journal_dir(), owner.room_id, "Sprint-1", "127.0.0.1", port)
    journal.op_added({"op_id": "old:1", "base_seq": 0, "kind": "create", "id": "saved", "type": "rect", "z": "V", "props": {"x": 1}})
    journal.chat_added({"msg_id": "old:m1", "channel": "room", "text": "unsent"})
    journal.worker.flush()
    journal.worker.stop()
    journal.lock_path.write_text('{"pid": 999999999}')

    window = make_window(qapp, home)
    try:
        assert window.crashed and window.launcher.banner.isHidden() is False
        room.acl.add_member(window.identity.fingerprint, "Tester", "editor")
        crashed = window.crashed[0]
        dialog = dialogs.RecoverDialog(crashed)
        text = dialog.findChild(type(window.launcher.banner_text)).text()
        assert "Sprint-1" in text and "1 unsaved change" in text and "1 unsent message" in text
        window.adopting = crashed
        window.connect_to(crashed.host, crashed.port, {"room_id": crashed.room_id})
        assert spin(qapp, lambda: window.session and window.session.welcomed and not window.session.doc.pending and not window.session.chat.outbox, timeout=12)
        assert "saved" in room.board.objects and [m["text"] for m in room.chat.room.messages] == ["unsent"]
        assert not window.crashed and window.launcher.banner.isHidden()
    finally:
        window.close()
    assert not (paths.journal_dir() / owner.room_id).exists()  # a clean exit left no journal


def test_empty_name_and_layers_hint(qapp, window, srv):
    port, _ = srv
    window.launcher.name.setText("  ")
    assert window.launcher.username() == "Anonymous"
    window.launcher.name.setText("Tester")
    assert paths.settings_file().exists()


def test_connection_failure_returns_message_not_crash(qapp, window, monkeypatch):
    """Regression: an unreachable address used to retry forever with no feedback at all."""
    import client.app as app_module

    monkeypatch.setattr(app_module, "CONNECT_GIVE_UP_MS", 300)
    window.connect_to("127.0.0.1", 9, {"room_id": "r_none"})
    assert window.stack.currentWidget() is window.request_page
    assert "Connecting" in window.request_page.room.text()
    assert spin(qapp, lambda: window.stack.currentWidget() is window.launcher, timeout=3)
    assert "Could not reach" in window.launcher.message.text()
    assert window.session is None


def test_leaving_offline_keeps_unsent_work_for_recovery(qapp, window, srv):
    """Regression: leaving a room while offline used to delete the journal holding unsent ops."""
    from client.recovery import find_crashed, pid_alive

    port, handle = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    room_id = session.room_id
    handle.kill()
    assert spin(qapp, lambda: not session.live)
    session.create("rect", {"x": 1, "y": 1, "w": 5, "h": 5}, obj_id="unsent")
    window.leave()
    root = paths.journal_dir() / room_id
    assert root.exists()
    # Pretend this process has since exited, as it will have by the next launch.
    for lock in root.glob("session-*.lock"):
        lock.write_text('{"pid": 999999999}')
    assert not pid_alive(999999999)
    found = find_crashed(paths.journal_dir())
    assert [op["id"] for op in found[0].ops] == ["unsent"]
    handle.restart()


def test_clean_leave_removes_journal(qapp, window, srv):
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    room_id = session.room_id
    session.create("rect", {"x": 1, "y": 1, "w": 5, "h": 5}, obj_id="sent")
    assert spin(qapp, lambda: not session.doc.pending)
    window.leave()
    assert not (paths.journal_dir() / room_id).exists()


def test_share_badge_and_toast_clear_when_decided_elsewhere(qapp, window, srv):
    """Regression: approving on the owner's other device left the badge and toast stale."""
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    other_device = _owner_bot(session, port)
    visitor = BotClient(port, "Alice")
    visitor.request(room_id=session.room_id)
    assert spin(qapp, lambda: "•1" in window.board.share_button.text())
    assert window.board._request_toasts
    other_device.wait_for(MsgType.ACCESS_REQUEST)
    other_device.send(MsgType.ACCESS_DECISION, {"fingerprint": visitor.fp, "decision": "approve", "role": "viewer"})
    assert spin(qapp, lambda: "•" not in window.board.share_button.text())
    assert not window.board._request_toasts


def test_toasts_are_capped(qapp, window):
    from client.widgets import MAX_TOASTS, Toast

    for i in range(10):
        window.show_toast(f"notice {i}")
    assert len(window.findChildren(Toast)) <= MAX_TOASTS


def test_private_indicators_use_bundled_text_and_icons_not_emoji(qapp, window, srv):
    """Regression: a lock emoji rendered as nothing on machines with no emoji font."""
    port, _ = srv
    enter_as_owner(qapp, window, port)
    session = window.session
    peer = add_member(_owner_bot(session, port), port, "Bob")
    assert spin(qapp, lambda: peer.fp in session.members)
    window.board.open_private(peer.fp)
    chat = window.board.chat
    assert chat.strip.text().startswith("Private")
    row = next(chat.channels.item(i) for i in range(chat.channels.count()) if "Bob" in chat.channels.item(i).text())
    assert not row.icon().isNull()
    root = Path(__file__).resolve().parent.parent
    for path in (root / "client").rglob("*.py"):
        assert "🔒" not in path.read_text(encoding="utf-8"), path


def test_kicked_member_returned_to_launcher(qapp, window, srv):
    port, handle = srv
    owner = make_room(port)
    room = handle.server.rooms[owner.room_id]
    room.acl.add_member(window.identity.fingerprint, "Tester", "editor")
    window.connect_to("127.0.0.1", port, {"room_id": owner.room_id})
    assert spin(qapp, lambda: window.session and window.session.welcomed)
    owner.admin("kick", fp=window.identity.fingerprint)
    assert spin(qapp, lambda: window.stack.currentWidget() is window.launcher)
    assert "removed you" in window.launcher.message.text()


def test_export_dialog_only_enables_relevant_options(qapp):
    dialog = dialogs.ExportDialog([("f", "F1")], True, lambda d: "x")
    assert dialog.scale.isEnabled() and not dialog.page.isEnabled() and not dialog.protect.isEnabled()
    pdf = next(b for b in dialog.format_group.buttons() if b.text() == "PDF")
    pdf.setChecked(True)
    assert dialog.page.isEnabled() and not dialog.scale.isEnabled()
    native = next(b for b in dialog.format_group.buttons() if b.text() == ".lanvas")
    native.setChecked(True)
    assert dialog.protect.isEnabled() and not dialog.scope_frame.isEnabled()


def test_image_object_round_trip(qapp):
    from client.boardpage import encode_image
    from client.canvas.items import BoardItem

    image = QImage(3000, 2000, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.blue)
    data = encode_image(image)
    decoded = QImage.fromData(base64.b64decode(data))
    assert max(decoded.width(), decoded.height()) == 1600  # downscaled to fit the size limit
    obj = {"id": "i", "type": "image", "parent": None, "z": "V", "deleted": False, "props": {"x": 0, "y": 0, "w": 10, "h": 10, "image_data": data}}
    assert not BoardItem(obj)._image.isNull()


@pytest.mark.perf
def test_PF_02_stroke_simplification_shrinks_commit():
    import json
    import math

    points = []
    for i in range(300):
        points += [i * 2.0, 40 * math.sin(i / 40) + (0.2 if i % 2 else -0.2)]
    simplified = simplify(points, tolerance=1.0)
    assert len(simplified) <= len(points) * 0.4
    op = {"kind": "create", "id": "x" * 12, "type": "path", "z": "V", "props": {"points": [round(v, 2) for v in simplified]}}
    assert len(json.dumps(op)) < 4096


@pytest.mark.unit
def test_simplify_edge_cases():
    assert simplify([]) == [] and simplify([1, 2]) == [1, 2] and simplify([1, 2, 3, 4]) == [1, 2, 3, 4]
    assert simplify([0, 0, 1, 0, 2, 0, 3, 0], 0.5) == [0, 0, 3, 0]


def test_every_ui_glyph_exists_in_the_bundled_font(qapp):
    """Regression: anything missing from Inter would fall back to whatever the machine has."""
    from PyQt6.QtGui import QRawFont

    root = Path(__file__).resolve().parent.parent
    font = QRawFont(str(root / "client/assets/fonts/Inter-Regular.ttf"), 12)
    missing = {}
    for path in (root / "client").rglob("*.py"):
        for ch in set(path.read_text(encoding="utf-8")):
            if ord(ch) > 126 and not font.supportsCharacter(ch):
                missing.setdefault(path.name, []).append(ch)
    assert not missing


def test_private_strip_contrast_in_both_themes(qapp):
    """Regression: white on the dark theme's light purple was below the 4.5:1 requirement."""
    for t in (theme.LIGHT, theme.DARK):
        assert theme.contrast_ratio(t.on_private, t.private) >= 4.5
