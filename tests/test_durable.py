"""SV-03 (writer level) and XP-06: a crash at any write step leaves old or new, never partial."""

from __future__ import annotations

import json

import pytest

from common import durable
from tests.harness import FaultyFS, SimulatedCrash

pytestmark = [pytest.mark.unit, pytest.mark.persist]

WHOLE_FILE_STEPS = [durable.STEP_TMP_WRITTEN, durable.STEP_TMP_SYNCED, durable.STEP_BAK_DONE, durable.STEP_REPLACED]


@pytest.mark.parametrize("step", WHOLE_FILE_STEPS)
@pytest.mark.parametrize("keep_bak", [False, True])
def test_SV_03_XP_06_atomic_write_crash_at_each_step(tmp_path, step, keep_bak):
    path = tmp_path / "acl.json"
    durable.atomic_write_json(path, {"v": "old"}, keep_bak=keep_bak)
    with pytest.raises(SimulatedCrash), FaultyFS(step):
        durable.atomic_write_json(path, {"v": "new"}, keep_bak=keep_bak)
    value = json.loads(path.read_text())["v"]
    assert value == ("new" if step == durable.STEP_REPLACED else "old")
    # A later write must succeed despite whatever .tmp the crash left behind.
    durable.atomic_write_json(path, {"v": "after"}, keep_bak=keep_bak)
    assert json.loads(path.read_text())["v"] == "after"


def test_SV_03_first_write_crash_leaves_nothing_at_target(tmp_path):
    path = tmp_path / "new.json"
    with pytest.raises(SimulatedCrash), FaultyFS(durable.STEP_TMP_WRITTEN):
        durable.atomic_write_json(path, {"v": 1})
    assert not path.exists()


def test_atomic_write_keeps_previous_as_bak(tmp_path):
    path = tmp_path / "room.json"
    durable.atomic_write_json(path, {"v": 1}, keep_bak=True)
    durable.atomic_write_json(path, {"v": 2}, keep_bak=True)
    path.write_text("{corrupt")
    assert durable.read_json_with_bak(path) == ({"v": 1}, True)


def test_line_endings_are_lf(tmp_path):
    path = tmp_path / "x.json"
    durable.atomic_write_json(path, {"a": 1})
    assert path.read_bytes().endswith(b"}\n") and b"\r" not in path.read_bytes()


def test_SV_03_append_crash_mid_line_is_detected_and_truncated(tmp_path):
    path = tmp_path / "ops.jsonl"
    log = durable.AppendLog(path)
    log.append(1, {"a": 1})
    with pytest.raises(SimulatedCrash), FaultyFS(durable.STEP_APPEND_PARTIAL):
        log.append(2, {"a": 2})
    log.close()
    entries, good, bad = durable.scan_log(path)
    assert [s for s, _ in entries] == [1] and bad
    durable.truncate_log(path, good)
    log = durable.AppendLog(path)
    log.append(2, {"a": 2})
    log.close()
    entries, _, bad = durable.scan_log(path)
    assert [s for s, _ in entries] == [1, 2] and not bad


def test_SV_03_append_complete_before_crash_survives(tmp_path):
    path = tmp_path / "ops.jsonl"
    log = durable.AppendLog(path)
    with pytest.raises(SimulatedCrash), FaultyFS(durable.STEP_APPEND_WRITTEN):
        log.append(1, {"a": 1})
    log.close()
    assert durable.scan_log(path)[0] == [(1, {"a": 1})]


def test_crc_catches_edit_and_scan_stops_at_middle_corruption(tmp_path):
    path = tmp_path / "ops.jsonl"
    log = durable.AppendLog(path)
    for i in range(1, 6):
        log.append(i, {"n": i})
    log.close()
    lines = path.read_bytes().split(b"\n")
    lines[2] = lines[2].replace(b'"n":3', b'"n":9')
    path.write_bytes(b"\n".join(lines))
    entries, good, bad = durable.scan_log(path)
    assert [s for s, _ in entries] == [1, 2] and bad
    assert good == len(lines[0]) + len(lines[1]) + 2


def test_scan_rejects_non_increasing_seq(tmp_path):
    path = tmp_path / "ops.jsonl"
    path.write_bytes(durable.encode_log_line(2, {}) + durable.encode_log_line(1, {}))
    entries, _, bad = durable.scan_log(path)
    assert [s for s, _ in entries] == [2] and bad


def test_group_commit_flag(tmp_path):
    log = durable.AppendLog(tmp_path / "l.jsonl")
    assert not log.dirty
    log.append(1, {})
    assert log.dirty
    log.sync()
    assert not log.dirty
    log.close()


def test_rewrite_log_is_atomic(tmp_path):
    path = tmp_path / "chat.jsonl"
    durable.rewrite_log(path, [(5, {"t": "keep"})])
    assert durable.scan_log(path)[0] == [(5, {"t": "keep"})]
