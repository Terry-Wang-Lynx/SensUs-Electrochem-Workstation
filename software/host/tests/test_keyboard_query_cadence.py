import threading
import time
from types import SimpleNamespace

import pytest

from pa_host import emm_motion
from pa_host.emm_motion import MotionError
from test_keyboard_responsiveness import responsive_bus


def test_captured_extra_x_rejection_blocks_next_move_and_preserves_stop_proof():
    bus, serial = responsive_bus()
    serial.overrides[2, 0x1F] = bytes.fromhex("021f00ce21146b0100ee6b")
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 1, 20, 20, None, None, cancel_event=threading.Event())
    assert caught.value.code == "response_length"
    assert caught.value.wire_diagnostic["response_hex"] == "021f00ce21146b"
    assert caught.value.wire_diagnostic["pending_hex"] == "0100ee6b"
    assert caught.value.stop_result["stop_confirmed"] is True
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_all_keyboard_queries_keep_normal_bus_gap_without_duplicate_profile_bursts(monkeypatch):
    bus, serial = responsive_bus()
    clock = [time.monotonic()]

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(emm_motion, "time", SimpleNamespace(monotonic=lambda: clock[0],
                         time=lambda: clock[0], sleep=sleep))
    bus._command_gap_s = 0.02
    events = []
    bus._callback = events.append
    cancellation = threading.Event()
    serial.on_fast_move = lambda address: cancellation.set()
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    previous_reply = None
    profile_commands = []
    for event in events:
        if event["kind"] != "wire":
            continue
        if event["direction"] == "rx":
            previous_reply = event["monotonic_s"]
        elif event["opcode"] != 0xFE:
            if event["opcode"] in (0x1F, 0x42, 0x1A):
                profile_commands.append((event["address"], event["opcode"]))
            if previous_reply is not None:
                assert event["monotonic_s"] - previous_reply >= 0.019999
    assert set(profile_commands) == {(address, opcode) for address in (1, 2) for opcode in (0x1F, 0x42, 0x1A)}
    assert len(profile_commands) == 6
    first_stop = next(index for index, frame in enumerate(serial.writes) if frame[1] == 0xFE)
    assert not any(frame[1] in (0x1F, 0x42, 0x1A) for frame in serial.writes[first_stop:])


def test_profile_change_after_a_hold_is_caught_before_another_fd():
    bus, serial = responsive_bus()
    cancellation = threading.Event()
    profile = bytearray.fromhex(bus.snapshot()["axes"]["Z"]["config"]["raw_hex"])
    profile[9] = 32

    def changed_after_move(address):
        serial.overrides[2, 0x42] = bytes(profile)
        cancellation.set()

    serial.on_fast_move = changed_after_move
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 0, 20, 20, None, None, cancel_event=cancellation)
    assert caught.value.code == "cancelled"
    assert caught.value.stop_result["stop_confirmed"]
    serial.writes.clear()
    with pytest.raises(MotionError):
        bus.alignment_fast_hold("X", 1, 20, 20, None, None, cancel_event=threading.Event())
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_target_read_failure_preserves_the_extra_drive_rejection():
    bus, serial = responsive_bus()
    from test_emm_motion import signed

    serial.overrides[2, 0x34] = bytes([2, 0x34]) + signed(serial.targets[0x34][2], 4) + bytes.fromhex("6b0100ee6b")
    with pytest.raises(MotionError) as caught:
        bus.alignment_fast_hold("X", 1, 20, 20, None, None, cancel_event=threading.Event())
    assert caught.value.code == "manual_target_unverified"
    assert getattr(caught.value, "wire_diagnostic", {}).get("pending_hex") == "0100ee6b"
    assert caught.value.stop_result["stop_confirmed"]
    assert not any(frame[1] == 0xFD for frame in serial.writes)
