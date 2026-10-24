from __future__ import annotations

import copy
import threading
import time

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus, validate_axis_config


AXES = {
    "X": {"address": 1, "lead_mm": 2, "travel_mm": 150,
          "positive_direction": 0, "max_speed_mm_s": 10},
    "Z": {"address": 2, "lead_mm": 2, "travel_mm": 50,
          "positive_direction": 1, "max_speed_mm_s": 10},
}


def config_reply(address):
    payload = bytearray.fromhex("19 02 02 02 00 10 01 00 04 b0 0b b8 0f a0 05 02 01 00 01 01 00 08 08 98 07 d0 00 08")
    payload[16] = address
    return bytes([address, 0x42, 0x21, 0x15]) + payload + b"\x6b"


def signed(value, size):
    return bytes([int(value < 0)]) + abs(value).to_bytes(size, "big")


class FakeSerial:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.timeout = kwargs["timeout"]
        self.write_timeout = kwargs["write_timeout"]
        self.buffer = bytearray()
        self.writes = []
        self.closed = False
        self.positions = {1: 65536, 2: -32768}
        self.speeds = {1: 0, 2: 0}
        self.statuses = {1: 2, 2: 2}
        self.overrides = {}
        self.move_completes = True
        self.on_move = None
        self.read_chunk = 2

    @property
    def in_waiting(self):
        return len(self.buffer)

    def reset_input_buffer(self):
        self.buffer.clear()

    def write(self, frame):
        self.writes.append(frame)
        address, opcode = frame[:2]
        if (address, opcode) in self.overrides:
            response = self.overrides[address, opcode]
            if isinstance(response, Exception):
                raise response
            if callable(response):
                response = response(frame)
        elif opcode == 0x1F:
            response = bytes([address, opcode, 0, 107, 3, 1, 0x6B])
        elif opcode == 0x42:
            response = config_reply(address)
        elif opcode == 0x1A:
            response = bytes([address, opcode, 6, 0, 0x6B])
        elif opcode == 0x36:
            response = bytes([address, opcode]) + signed(self.positions[address], 4) + b"\x6b"
        elif opcode == 0x35:
            response = bytes([address, opcode]) + signed(self.speeds[address], 2) + b"\x6b"
        elif opcode == 0x3A:
            response = bytes([address, opcode, self.statuses[address], 0x6B])
        elif opcode == 0xF3:
            assert frame[2:] == b"\xab\x01\x00\x6b"
            self.statuses[address] |= 1
            response = bytes([address, opcode, 2, 0x6B])
        elif opcode == 0xFD:
            assert frame[10:] == b"\x02\x00\x6b"
            if self.move_completes:
                delta = round(int.from_bytes(frame[6:10], "big") * 65536 / 3200)
                self.positions[address] += delta * (-1 if frame[2] else 1)
                self.speeds[address] = 0
                self.statuses[address] = 3
            else:
                self.statuses[address] = 1
            if self.on_move:
                self.on_move()
            response = bytes([address, opcode, 2, 0x6B])
        elif opcode == 0xFE:
            assert frame[2:] == b"\x98\x00\x6b"
            self.speeds[address] = 0
            response = bytes([address, opcode, 2, 0x6B])
        else:
            raise AssertionError(f"Unexpected command {frame.hex()}")
        self.buffer.extend(response)
        return len(frame)

    def read(self, count):
        count = min(count, self.read_chunk)
        response = bytes(self.buffer[:count])
        del self.buffer[:count]
        if not response:
            time.sleep(min(self.timeout, 0.001))
        return response

    def close(self):
        self.closed = True


def make_bus(*, move_timeout_s=0.4, **kwargs):
    opened = []

    def factory(**options):
        serial = FakeSerial(**options)
        opened.append(serial)
        return serial

    bus = SharedEmmBus("fake-rs485", 115200, copy.deepcopy(AXES),
                       serial_factory=factory, transaction_timeout_s=0.02,
                       move_timeout_s=move_timeout_s, **kwargs)
    bus._command_gap_s = 0
    return bus, opened


def ready_bus(**kwargs):
    bus, opened = make_bus(**kwargs)
    bus.connect()
    bus.confirm_reference()
    bus.enable("X")
    bus.enable("Z")
    return bus, opened[0]


@pytest.mark.parametrize("field,value", [
    ("address", 0), ("address", 256), ("address", 1.5), ("address", True),
    ("lead_mm", 0), ("lead_mm", float("nan")), ("travel_mm", -1),
    ("travel_mm", float("inf")), ("positive_direction", 2),
    ("max_speed_mm_s", 0), ("max_speed_mm_s", 201),
])
def test_axis_validation_rejects_invalid_mechanics(field, value):
    config = {**AXES["X"], field: value}
    with pytest.raises(ValueError):
        validate_axis_config(config)


def test_mechanical_config_has_no_implicit_defaults_and_addresses_are_unique():
    with pytest.raises(ValueError):
        validate_axis_config({"address": 1})
    with pytest.raises(ValueError):
        SharedEmmBus("unused", 115200, {"X": AXES["X"], "Z": AXES["X"]})
    with pytest.raises(ValueError):
        SharedEmmBus("unused", 115200, {"X": AXES["X"]})


def test_connect_reads_both_axes_without_enable_zero_home_or_motion():
    events = []
    bus, opened = make_bus(event_callback=events.append)
    state = bus.connect()
    assert state["connected"] and state["port"] == "fake-rs485"
    assert state["referenced"] is False
    assert state["position"] == {"X": None, "Z": None}
    assert state["axes"]["X"]["raw_count"] == 65536
    assert state["axes"]["Z"]["raw_count"] == -32768
    assert state["axes"]["X"]["config"]["microstep"] == 16
    assert all(axis["telemetry_valid"] for axis in state["axes"].values())
    assert not any(axis["enable_confirmed"] for axis in state["axes"].values())
    assert {frame[1] for frame in opened[0].writes} == {0x1F, 0x42, 0x1A, 0x36, 0x35, 0x3A}
    assert opened[0].write_timeout > 0
    assert {event["direction"] for event in events if event["kind"] == "wire"} == {"tx", "rx"}


@pytest.mark.parametrize("response", [
    bytes.fromhex("02 1f 00 6b 03 01 6b"),
    bytes.fromhex("01 36 00 6b 03 01 6b"),
    bytes.fromhex("01 1f 00 6b 03 01 00"),
    bytes.fromhex("01 1f 00 6b 03 01 6b 00"),
    bytes.fromhex("01 1f 00 6b"),
    bytes.fromhex("01 1f 00 00 00 00 6b"),
])
def test_connect_rejects_wrong_address_opcode_checksum_length_or_identity(response):
    serial = FakeSerial(timeout=0.001, write_timeout=0.02)
    serial.overrides[1, 0x1F] = response
    bus = SharedEmmBus("fake", 115200, AXES, serial_factory=lambda **_: serial,
                       transaction_timeout_s=0.01)
    with pytest.raises(MotionError):
        bus.connect()
    assert not bus.snapshot()["connected"]
    assert serial.closed
    assert not any(frame[1] in (0xF3, 0xFD) for frame in serial.writes)


@pytest.mark.parametrize("index,value", [(2, 34), (3, 20), (4, 0), (20, 2),
                                         (21, 1), (22, 0), (22, 3), (23, 2)])
def test_connect_rejects_unsupported_config_layout_or_protocol(index, value):
    serial = FakeSerial(timeout=0.001, write_timeout=0.02)
    response = bytearray(config_reply(1))
    response[index] = value
    serial.overrides[1, 0x42] = bytes(response)
    bus = SharedEmmBus("fake", 115200, AXES, serial_factory=lambda **_: serial,
                       transaction_timeout_s=0.01)
    with pytest.raises(MotionError):
        bus.connect()


def test_reference_is_operator_confirmed_software_offset_and_resets_on_reconnect():
    bus, opened = make_bus()
    bus.connect()
    with pytest.raises(MotionError):
        bus.confirm_reference(confirmed=False)
    state = bus.confirm_reference()
    assert state["position"] == {"X": 0.0, "Z": 0.0}
    opened[0].positions[1] += 16384
    opened[0].positions[2] -= 32768
    assert bus.refresh()["position"] == {"X": 0.5, "Z": 1.0}
    bus.close()
    state = bus.connect()
    assert not state["referenced"]
    assert not any(axis["enable_confirmed"] for axis in state["axes"].values())
    assert not any(frame[1] in (0x0A, 0x9A) for device in opened for frame in device.writes)


def test_reference_cannot_be_confirmed_while_an_axis_is_moving():
    bus, opened = make_bus()
    bus.connect()
    opened[0].speeds[2] = 1
    with pytest.raises(MotionError):
        bus.confirm_reference()
    assert not bus.snapshot()["referenced"]


def test_arrival_accepts_observed_encoder_error_within_drive_position_window():
    bus, serial = ready_bus()
    serial.on_move = lambda: serial.positions.update({1: serial.positions[1] + 26})
    result = bus.move_to("X", 0.5, 0.5)
    assert result["position"]["X"] == pytest.approx(0.50079345703125)
    assert result["axes"]["X"]["arrival_tolerance_mm"] == pytest.approx(2 * 0.8 / 360)
    assert not any(frame[1] == 0xFE for frame in serial.writes)
    assert result["referenced"]


def test_arrival_still_rejects_error_outside_drive_window_and_stops_both_axes():
    bus, serial = ready_bus(move_timeout_s=0.08)
    serial.on_move = lambda: serial.positions.update({1: serial.positions[1] + 200})
    with pytest.raises(MotionError) as failure:
        bus.move_to("X", 0.5, 0.5)
    assert failure.value.code == "move_timeout"
    assert failure.value.stop_result["stop_confirmed"]
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}


def test_arrival_window_is_centered_on_the_actual_quantized_command():
    bus, serial = ready_bus()
    serial.positions[1] += 6
    serial.on_move = lambda: serial.positions.update({1: serial.positions[1] + 140})
    result = bus.move_to("X", 0.5, 0.5)
    state = result["axes"]["X"]
    assert state["quantized_target_mm"] == pytest.approx(0.50018310546875)
    assert abs(result["position"]["X"] - state["quantized_target_mm"]) < state["arrival_tolerance_mm"]
    assert abs(result["position"]["X"] - 0.5) > state["arrival_tolerance_mm"]


@pytest.mark.parametrize("motor_type,window_tenths", [(0x19, 19), (0x32, 10)])
def test_connect_rejects_position_window_wider_than_one_full_motor_step(motor_type, window_tenths):
    serial = FakeSerial(timeout=0.001, write_timeout=0.02)
    response = bytearray(config_reply(1))
    response[4] = motor_type
    response[30:32] = window_tenths.to_bytes(2, "big")
    serial.overrides[1, 0x42] = bytes(response)
    bus = SharedEmmBus("fake", 115200, AXES, serial_factory=lambda **_: serial)
    with pytest.raises(MotionError) as failure:
        bus.connect()
    assert failure.value.code == "unsupported_position_window"
    assert not any(frame[1] in (0xFD, 0xF3) for frame in serial.writes)


@pytest.mark.parametrize("target", [0, 150])
def test_fractional_endpoint_rounding_stays_inside_soft_limits(target):
    bus, serial = ready_bus()
    serial.positions[1] += 16378 if target == 0 else 150 * 32768 - 16378
    result = bus.move_to("X", target, 0.5)
    command = next(frame for frame in serial.writes if frame[1] == 0xFD)
    assert int.from_bytes(command[6:10], "big") == 799
    assert 0 <= result["position"]["X"] <= 150
    assert abs(result["position"]["X"] - target) <= 2 / 3200


def test_subpulse_encoder_readback_at_reference_does_not_block_inward_move():
    bus, serial = ready_bus()
    serial.positions[1] -= 2
    result = bus.move_to("X", 0.5, 0.5)
    assert result["position"]["X"] == pytest.approx(0.5 - 2 / 32768)


def test_true_out_of_range_readback_is_not_hidden_by_arrival_tolerance():
    bus, serial = ready_bus()
    serial.positions[1] -= 100
    with pytest.raises(MotionError) as failure:
        bus.move_to("X", 0.5, 0.5)
    assert failure.value.code == "travel_limit"
    assert not any(frame[1] == 0xFD for frame in serial.writes)


@pytest.mark.parametrize("ack", [0, 0x12, 0x22, 0xE2, 0xEE, 0x9F])
def test_enable_requires_success_ack_and_readback(ack):
    bus, opened = make_bus()
    bus.connect()
    opened[0].overrides[1, 0xF3] = bytes([1, 0xF3, ack, 0x6B])
    with pytest.raises(MotionError):
        bus.enable("X")
    assert not bus.snapshot()["axes"]["X"]["enable_confirmed"]


def test_enable_ack_without_enabled_readback_is_failure():
    bus, opened = make_bus()
    bus.connect()
    opened[0].overrides[1, 0xF3] = bytes.fromhex("01 f3 02 6b")
    with pytest.raises(MotionError):
        bus.enable("X")
    assert not bus.snapshot()["axes"]["X"]["enable_confirmed"]


def test_moves_require_reference_and_explicit_enable():
    bus, opened = make_bus()
    bus.connect()
    with pytest.raises(MotionError):
        bus.move_to("X", 1, 1)
    bus.confirm_reference()
    opened[0].statuses[1] = 3
    bus.refresh()
    with pytest.raises(MotionError):
        bus.move_to("X", 1, 1)
    assert not any(frame[1] == 0xFD for frame in opened[0].writes)


def test_move_uses_axis_direction_and_readback_not_ack_as_arrival():
    bus, serial = ready_bus()
    state = bus.move_to("Z", 1, 0.5)
    assert state["position"]["Z"] == 1
    commands = [frame for frame in serial.writes if frame[1] == 0xFD]
    assert commands == [bytes.fromhex("02 fd 01 00 0f 0a 00 00 06 40 02 00 6b")]
    assert serial.writes[-1][1] == 0x3A


@pytest.mark.parametrize("target,speed", [(-1, 1), (151, 1), (float("nan"), 1),
                                         (1, 0), (1, 0.01), (1, float("inf")), (1, 11)])
def test_move_rejects_bounds_and_unrepresentable_speeds_before_command(target, speed):
    bus, serial = ready_bus()
    with pytest.raises(MotionError):
        bus.move_to("X", target, speed)
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_quantized_speed_never_exceeds_requested_speed():
    bus, serial = ready_bus()
    bus.move_to("X", 1, 0.051)
    frame = next(frame for frame in serial.writes if frame[1] == 0xFD)
    assert frame[3:5] == b"\x00\x01"
    assert bus.snapshot()["axes"]["X"]["command_speed_mm_s"] == pytest.approx(1 / 30)


@pytest.mark.parametrize("wrong_state", ["position", "speed", "reached"])
def test_success_ack_does_not_bypass_position_speed_and_reached_checks(wrong_state):
    bus, serial = ready_bus(move_timeout_s=0.08)
    serial.move_completes = False

    def attempted_move():
        serial.positions[1] = 98304 if wrong_state != "position" else 65536
        serial.speeds[1] = 1 if wrong_state == "speed" else 0
        serial.statuses[1] = 1 if wrong_state == "reached" else 3

    serial.on_move = attempted_move
    with pytest.raises(MotionError) as failure:
        bus.move_to("X", 1, 1)
    assert failure.value.code == "move_timeout"
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not bus.snapshot()["referenced"]


def test_cancel_during_move_stops_both_axes_and_does_not_disable_vertical_axis():
    bus, serial = ready_bus()
    cancel = threading.Event()
    serial.on_move = cancel.set
    with pytest.raises(MotionError) as failure:
        bus.move_to("Z", 1, 1, cancel_event=cancel)
    assert failure.value.code == "cancelled"
    assert failure.value.stop_result["stop_confirmed"]
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert not any(frame[1] == 0xF3 and frame[3] == 0 for frame in serial.writes)


def test_missing_speed_is_not_fabricated_as_zero_and_invalidates_reference():
    bus, serial = ready_bus()
    serial.overrides[2, 0x35] = b""
    with pytest.raises(MotionError):
        bus.refresh()
    state = bus.snapshot()
    assert not state["referenced"]
    assert not state["axes"]["Z"]["telemetry_valid"]
    assert not state["axes"]["Z"]["enable_confirmed"]


def test_stop_attempts_both_axes_even_when_first_ack_fails_and_reports_failure():
    bus, serial = ready_bus()
    serial.overrides[1, 0xFE] = bytes.fromhex("01 fe e2 6b")
    result = bus.stop_all()
    assert not result["stop_confirmed"]
    assert not result["axes"]["X"]["stop_confirmed"]
    assert result["axes"]["Z"]["stop_confirmed"]
    assert result["errors"]
    assert [frame[0] for frame in serial.writes if frame[1] == 0xFE] == [1, 2]


def test_stop_ack_without_zero_speed_is_unconfirmed():
    bus, serial = ready_bus()
    serial.speeds[2] = 10
    serial.overrides[2, 0xFE] = bytes.fromhex("02 fe 02 6b")
    assert not bus.stop_all()["stop_confirmed"]


def test_close_retains_vertical_enable_and_does_not_claim_stop_without_evidence():
    bus, serial = ready_bus()
    bus.close()
    assert serial.closed
    assert not bus.snapshot()["connected"]
    assert not bus.snapshot()["referenced"]
    assert not any(frame[1] == 0xF3 and frame[3] == 0 for frame in serial.writes)


def test_scale_input_mode_is_rejected_without_enabling():
    serial = FakeSerial(timeout=0.001, write_timeout=0.02)
    serial.overrides[1, 0x1A] = bytes.fromhex("01 1a 86 00 6b")
    bus = SharedEmmBus("fake", 115200, AXES, serial_factory=lambda **_: serial)
    with pytest.raises(MotionError, match="scaling"):
        bus.connect()
    assert not any(frame[1] == 0xF3 for frame in serial.writes)


def test_malformed_sign_or_power_change_latches_fault_and_blocks_enable():
    bus, serial = ready_bus()
    serial.statuses[1] = 0x83
    with pytest.raises(MotionError) as failure:
        bus.refresh()
    assert failure.value.code == "power_flag_changed"
    with pytest.raises(MotionError) as failure:
        bus.enable("X")
    assert failure.value.code == "reconnect_required"


def test_stale_bytes_fail_closed_instead_of_becoming_fresh_telemetry():
    bus, serial = ready_bus()
    serial.buffer.extend(bytes.fromhex("01 35 00 00 00 6b"))
    with pytest.raises(MotionError) as failure:
        bus.refresh()
    assert failure.value.code == "unexpected_input"
    assert not bus.snapshot()["referenced"]


def test_failed_move_is_not_retried_and_stops_both_axes():
    bus, serial = ready_bus()
    serial.overrides[1, 0xFD] = b""
    with pytest.raises(MotionError) as failure:
        bus.move_to("X", 1, 1)
    assert failure.value.code == "transaction_timeout"
    assert len([frame for frame in serial.writes if frame[1] == 0xFD]) == 1
    stops = [frame[0] for frame in serial.writes if frame[1] == 0xFE]
    assert stops[0] == 1
    assert set(stops) == {1, 2}
    assert failure.value.stop_result["stop_confirmed"]


def test_stop_request_between_move_gate_and_wire_write_cannot_be_cleared():
    bus, serial = ready_bus()
    bus._stop_event.set()
    with pytest.raises(MotionError):
        bus.move_to("X", 1, 1)
    assert not any(frame[1] == 0xFD for frame in serial.writes)


def test_external_stop_interrupts_wait_for_arrival_with_bounded_latency():
    bus, serial = ready_bus()
    serial.move_completes = False
    started = threading.Event()
    serial.on_move = started.set
    failures = []

    def move():
        try:
            bus.move_to("X", 1, 1)
        except MotionError as error:
            failures.append(error)

    worker = threading.Thread(target=move)
    worker.start()
    assert started.wait(1)
    before = time.monotonic()
    result = bus.stop_all()
    worker.join(1)
    assert not worker.is_alive()
    assert time.monotonic() - before < 0.5
    assert result["stop_confirmed"]
    assert failures and failures[0].code == "cancelled"


def test_stop_sends_other_axis_command_before_waiting_for_first_telemetry():
    bus, serial = ready_bus()
    offset = len(serial.writes)
    bus.stop_all()
    assert [frame[1] for frame in serial.writes[offset:offset + 2]] == [0xFE, 0xFE]


def test_successful_stop_requires_new_reference_and_enable_to_rearm():
    bus, serial = ready_bus()
    assert bus.stop_all()["stop_confirmed"]
    with pytest.raises(MotionError):
        bus.move_to("X", 1, 1)
    bus.confirm_reference()
    with pytest.raises(MotionError):
        bus.move_to("X", 1, 1)
    bus.enable("X")
    bus.enable("Z")
    assert bus.move_to("X", 1, 1)["position"]["X"] == 1


def test_pending_stop_blocks_explicit_enable_before_wire_write():
    bus, serial = ready_bus()
    before = len(serial.writes)
    bus._stop_event.set()
    with pytest.raises(MotionError) as failure:
        bus.enable("X")
    assert failure.value.code == "cancelled"
    assert not any(frame[1] == 0xF3 for frame in serial.writes[before:])


def test_enable_lost_during_move_invalidates_reference_and_attempts_both_stops():
    bus, serial = ready_bus()
    serial.move_completes = False
    serial.on_move = lambda: serial.statuses.update({1: 2})
    with pytest.raises(MotionError) as failure:
        bus.move_to("X", 1, 1)
    assert failure.value.code == "not_enabled"
    assert not bus.snapshot()["referenced"]
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}


def test_short_serial_write_never_reports_enable_success():
    bus, serial = ready_bus()
    original_write = serial.write

    def write(frame):
        if frame[1] == 0xF3:
            return len(frame) - 1
        return original_write(frame)

    serial.write = write
    with pytest.raises(MotionError) as failure:
        bus.enable("X")
    assert failure.value.code == "short_write"
    assert not bus.snapshot()["axes"]["X"]["enable_confirmed"]


def test_stop_rejects_drifting_axis_even_when_speed_rounds_to_zero():
    bus, serial = ready_bus()

    def drifting(frame):
        serial.positions[2] += 16
        return b"\x02\x36" + signed(serial.positions[2], 4) + b"\x6b"

    serial.overrides[2, 0x36] = drifting
    result = bus.stop_all()
    assert not result["stop_confirmed"]
    assert result["axes"]["X"]["stop_confirmed"]
    assert not result["axes"]["Z"]["stop_confirmed"]


def test_snapshot_expires_cached_telemetry_without_fabricating_zero_speed(monkeypatch):
    bus, serial = ready_bus()
    state = bus.snapshot()
    later = max(axis["updated_monotonic"] for axis in state["axes"].values()) + 3
    monkeypatch.setattr("pa_host.emm_motion.time.monotonic", lambda: later)
    assert not any(axis["telemetry_valid"] for axis in bus.snapshot()["axes"].values())


def test_close_rejects_unconfirmed_stop_and_retains_connection_for_retry():
    bus, serial = ready_bus()
    serial.overrides[2, 0xFE] = bytes.fromhex("02 fe e2 6b")
    with pytest.raises(MotionError) as failure:
        bus.close()
    assert failure.value.code == "stop_unconfirmed"
    assert failure.value.stop_result["stop_confirmed"] is False
    assert failure.value.stop_result["axes"]["Z"]["stop_confirmed"] is False
    assert not serial.closed
    assert bus.snapshot()["connected"]
    assert bus.snapshot()["stop_confirmed"] is False
    assert bus.snapshot()["faulted"]
    del serial.overrides[2, 0xFE]
    assert bus.stop_all()["stop_confirmed"]
    result = bus.close()
    assert result["stop_confirmed"]
    assert serial.closed
    assert not bus.snapshot()["connected"]


def test_close_reports_telemetry_failure_even_when_stop_was_acknowledged():
    bus, serial = ready_bus()
    serial.overrides[1, 0x35] = b""
    with pytest.raises(MotionError) as failure:
        bus.close()
    assert failure.value.stop_result["axes"]["X"]["stop_confirmed"] is False
    assert failure.value.stop_result["axes"]["Z"]["stop_confirmed"] is True
    assert bus.snapshot()["connected"]
    assert not serial.closed


def test_disconnected_close_preserves_known_stop_status_without_inventing_confirmation():
    bus, opened = make_bus()
    assert bus.close()["stop_confirmed"] is False
    assert opened == []
    bus.connect()
    assert bus.close()["stop_confirmed"]
    assert bus.close()["stop_confirmed"]


def test_connect_rejects_replacing_an_open_bus_and_preserves_reference():
    bus, serial = ready_bus()
    state = bus.snapshot()
    commands = list(serial.writes)
    with pytest.raises(MotionError) as failure:
        bus.connect()
    assert failure.value.code == "already_connected"
    assert not serial.closed
    assert serial.writes == commands
    assert bus.snapshot() == state


def test_connect_cannot_bypass_unconfirmed_stop_by_reopening_serial():
    bus, opened = make_bus()
    bus.connect()
    serial = opened[0]
    serial.statuses[1] = 3
    serial.overrides[1, 0xFE] = bytes.fromhex("01 fe e2 6b")
    with pytest.raises(MotionError):
        bus.close()
    with pytest.raises(MotionError) as failure:
        bus.connect()
    assert failure.value.code == "already_connected"
    assert len(opened) == 1
    assert not serial.closed
    assert bus.snapshot()["connected"]
    assert bus.snapshot()["stop_confirmed"] is False
