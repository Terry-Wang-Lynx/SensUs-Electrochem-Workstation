"""Bounded shared-RS485 transport for two explicitly configured Emm axes.

Protocol references: the supplied single-axis motor.py/emm_protocol.py and
https://github.com/xhfb/emm_stepper (commands.py, configs.py, parameters.py,
ZDT_X42S manual V1.0.3/V1.0.5). The supported read-config response is exactly
33 bytes / 21 parameters / 28 data bytes. Only fixed 0x6B framing, receive-only
ACK mode, normal integer RPM input and known 1.8/0.9-degree motor types are
accepted. Unknown profiles fail closed; nothing configures the drive for them.
0x02 acknowledges a command, while 0x9F is an unsolicited completion event;
neither replaces fresh position, speed and reached-status checks.

The fixed checksum is a protocol terminator, not payload corruption detection.
The protocol has no request sequence number. No transaction is retried; a fault
invalidates reference/enable authorization and requires reconnection. Queued
bytes, wrong-address frames and unsolicited replies are rejected. This cannot
prove the freshness of a delayed, otherwise identical response on the wire.

The option-status scale-input bit and power flag differ across firmware. The
former must report normal scaling; the latter is tracked for changes rather
than assuming its polarity. Actual direction, scaling, encoder correspondence,
lead, travel, stopping distance, gravity retention and limits need physical
qualification. Software stop confirmation requires two stationary telemetry
samples after an acknowledged stop, or healthy disabled feedback after an E2
rejection on an already disabled drive; it is not a safety-rated emergency stop.
There is no automatic enable, hardware zero, homing, release or protection-clear.
"""

from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from typing import Any, Callable


POSITION_COUNT_MAX = 0xFFFFFFFF


class MotionError(ValueError):
    def __init__(self, code: str, message: str, *, stop_result=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.stop_result = stop_result


class _MotionCancellation:
    def __init__(self, *events):
        self._events = events

    def is_set(self):
        return any(event is not None and event.is_set() for event in self._events)


def _number(value, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MotionError("invalid_config", f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise MotionError("invalid_config", f"{field} must be finite" + (" and positive" if positive else ""))
    return number


def validate_axis_config(config: dict, axis: str = "axis") -> dict:
    required = {"address", "lead_mm", "travel_mm", "positive_direction", "max_speed_mm_s"}
    if not isinstance(config, dict) or not required.issubset(config):
        raise MotionError("invalid_config", f"{axis} requires explicit address, lead, travel, direction and speed")
    result = {}
    for field in required:
        result[field] = _number(config[field], f"{axis}.{field}", positive=field.endswith("mm") or field == "max_speed_mm_s")
    if result["address"] != int(result["address"]) or not 1 <= result["address"] <= 255:
        raise MotionError("invalid_config", f"{axis}.address must be an integer in 1..255")
    if result["positive_direction"] not in (0, 1):
        raise MotionError("invalid_config", f"{axis}.positive_direction must be 0 or 1")
    if result["max_speed_mm_s"] > result["lead_mm"] * 100:
        raise MotionError("invalid_config", f"{axis}.max_speed_mm_s exceeds 6000 RPM")
    result["address"] = int(result["address"])
    result["positive_direction"] = int(result["positive_direction"])
    return result


class SharedEmmBus:
    manual_calibration_supported = False
    manual_capture_supported = True

    def __init__(self, port: str, baud: int, axes: dict, serial_factory=None, *,
                 event_callback: Callable[[dict], None] | None = None,
                 transaction_timeout_s: float = 1.0, move_timeout_s: float | None = None,
                 require_absolute_limits: bool = False):
        if not isinstance(port, str) or not port.strip():
            raise MotionError("invalid_config", "A serial port is required")
        if isinstance(baud, bool) or not isinstance(baud, int) or not 1200 <= baud <= 921600:
            raise MotionError("invalid_config", "baud must be an integer in 1200..921600")
        if not isinstance(axes, dict) or set(axes) != {"X", "Z"}:
            raise MotionError("invalid_config", "Exactly X and Z axes must be configured")
        self.axes = {name: validate_axis_config(axes[name], name) for name in ("X", "Z")}
        if self.axes["X"]["address"] == self.axes["Z"]["address"]:
            raise MotionError("invalid_config", "X and Z addresses must differ")
        self.port = port
        self.baud = baud
        self._timeout = _number(transaction_timeout_s, "transaction_timeout_s", positive=True)
        if self._timeout > 2:
            raise MotionError("invalid_config", "transaction_timeout_s cannot exceed 2 seconds")
        self._move_timeout = None if move_timeout_s is None else _number(move_timeout_s, "move_timeout_s", positive=True)
        if self._move_timeout is not None and self._move_timeout > 28800:
            raise MotionError("invalid_config", "move_timeout_s cannot exceed 8 hours")
        self._serial_factory = serial_factory
        self._callback = event_callback
        self._serial = None
        self._connected = False
        self._faulted = False
        self._transport_detached = False
        self._serial_lock = threading.RLock()
        self._command_gap_s = 0.02
        self._last_exchange_monotonic = None
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._keyboard_stop_lock = threading.Lock()
        self._keyboard_segment_cancel = None
        self._keyboard_segment_started = None
        self._responsive_keyboard = False
        self._automatic_motion_axis = None
        self._startup_approach = None
        self._completed_motion_targets = {}
        self._serial_opened_once = False
        self._stop_event = threading.Event()
        self._stop_generation = 0
        self._last_stop = None
        self._last_stop_generation = None
        self._last_stop_samples = None
        self._last_stop_completed = None
        self.require_absolute_limits = require_absolute_limits is True
        self._connection_token = None
        self._absolute_limits = None
        self._experiment_calibration = None
        self._manual = {"active": False, "status": "idle", "generation": None}
        self._state = self._empty_state()

    def _empty_state(self):
        return {name: {"mechanical": copy.deepcopy(config), "address": config["address"],
                       "referenced": False, "enabled": False, "enable_confirmed": False,
                       "position_mm": None, "speed_mm_s": None, "status": None,
                       "raw_count": None, "reference_count": None, "telemetry_valid": False,
                       "updated_monotonic": None, "config": None, "firmware_version": None,
                       "hardware_version": None, "power_flag_baseline": None}
                for name, config in self.axes.items()}

    @contextmanager
    def _operation(self):
        if self._stop_lock.locked():
            raise MotionError("motion_busy", "Motion stop is in progress")
        if not self._operation_lock.acquire(blocking=False):
            raise MotionError("motion_busy", "Another motion operation is in progress")
        try:
            yield
        finally:
            self._operation_lock.release()

    def _event(self, kind: str, **fields):
        context = getattr(self, "_keyboard_trace_context", None)
        if context or kind.startswith("keyboard_"):
            if context and kind == "wire" and fields.get("direction") == "tx" and fields.get("opcode") == 0xFD:
                context["command_sent"] = True
            from .keyboard_trace import emit
            emit({"source": "motor", "kind": kind, "port": self.port, **(context or {}), **fields})
        if self._callback is not None:
            try:
                self._callback({"kind": kind, "host_unix_s": time.time(),
                                "monotonic_s": time.monotonic(), "port": self.port, **fields})
            except Exception:
                pass

    def _fault(self, error):
        with self._state_lock:
            self._faulted = True
            self._startup_approach = None
            self._completed_motion_targets = {}
            self._absolute_limits = None
            for state in self._state.values():
                state.update(referenced=False, reference_count=None, position_mm=None,
                             enable_confirmed=False, telemetry_valid=False)
        self._event("fault", code=getattr(error, "code", "motion_error"), message=str(error))

    def _require_connected(self, *, allow_fault=False):
        if not self._connected or self._serial is None:
            raise MotionError("not_connected", "Motion bus is not connected")
        if self._faulted and not allow_fault:
            raise MotionError("reconnect_required", "Motion fault requires reconnect and reference confirmation")

    def _require_manual_clear(self):
        if self._manual["active"]:
            raise MotionError("manual_calibration_active", "Explicit manual calibration must be safely completed before motion")

    def _axis(self, axis):
        if axis not in self.axes:
            raise MotionError("invalid_axis", "axis must be X or Z")
        return self.axes[axis]

    def _check_cancel(self, cancel_event=None):
        if self._stop_event.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise MotionError("cancelled", "Motion cancelled")

    def _current_stop_result(self):
        with self._state_lock:
            if self._last_stop is not None and self._last_stop_generation == self._stop_generation:
                return copy.deepcopy(self._last_stop)
            return {"stop_confirmed": False, "axes": {}, "errors": [
                {"code": "stop_pending", "message": "The current STOP has not completed"}]}

    def _transact(self, body: bytes, response_length: int, *, deadline=None, ack=False,
                  cancel_event=None, interruptible=False, stop_command=False, keyboard_read=False):
        with self._serial_lock:
            if self._serial is None:
                raise MotionError("not_connected", "Motion port is closed")
            if interruptible:
                self._check_cancel(cancel_event)
            operation_deadline = deadline or float("inf")
            response = bytearray()
            transmit = None
            received = None
            wire_error = None
            address, opcode = body[:2]
            command_gap = self._command_gap_s
            if keyboard_read:
                with self._state_lock:
                    stopped = (self._last_stop and self._last_stop["stop_confirmed"]
                               and self._last_stop_generation == self._stop_generation
                               and self._last_stop_completed is not None and self._keyboard_segment_started is not None
                               and self._last_stop_completed >= self._keyboard_segment_started)
                    if (not self._responsive_keyboard or not self._manual.get("active")
                            or self._manual.get("control_mode") != "keyboard"
                            or opcode not in (0x1F, 0x42, 0x1A, 0x33, 0x34, 0x35, 0x36, 0x3A, 0x43)
                            or self._manual.get("polling_axis") is not None and not stopped):
                        raise MotionError("keyboard_read_phase", "Keyboard read hints require a stationary keyboard read phase")
            try:
                while not stop_command and self._last_exchange_monotonic is not None:
                    if interruptible:
                        self._check_cancel(cancel_event)
                    now = time.monotonic()
                    if now >= operation_deadline:
                        raise MotionError("transaction_timeout", "Command deadline expired before bus became quiet")
                    gap_remaining = self._last_exchange_monotonic + command_gap - now
                    if gap_remaining <= 0:
                        break
                    time.sleep(min(gap_remaining, operation_deadline - now, 0.005))
                if interruptible:
                    self._check_cancel(cancel_event)
                deadline = min(operation_deadline, time.monotonic() + self._timeout)
                if time.monotonic() >= deadline:
                    raise MotionError("transaction_timeout", "Command deadline expired")
                if self._serial.in_waiting:
                    if stop_command:
                        self._serial.reset_input_buffer()
                    else:
                        raise MotionError("unexpected_input", "Unsolicited or stale serial bytes before command")
                frame = body + b"\x6b"
                self._serial.write_timeout = max(0.001, deadline - time.monotonic())
                transmit = {"frame_hex": frame.hex(), "host_unix_s": time.time(), "monotonic_s": time.monotonic()}
                if self._serial.write(frame) != len(frame):
                    raise MotionError("short_write", "Incomplete serial command write")
                while len(response) < response_length:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MotionError("transaction_timeout", f"Axis {address} opcode 0x{opcode:02x} response timeout")
                    self._serial.timeout = min(0.02, remaining)
                    response.extend(self._serial.read(response_length - len(response)))
                received = {"host_unix_s": time.time(), "monotonic_s": time.monotonic()}
                if time.monotonic() > deadline:
                    raise MotionError("transaction_timeout", "Serial response exceeded deadline")
                if len(response) != response_length or self._serial.in_waiting:
                    raise MotionError("response_length", "Unexpected serial response length")
                if response[0] != address or response[1] != opcode:
                    raise MotionError("response_binding", "Serial response address/opcode mismatch")
                if response[-1] != 0x6B:
                    raise MotionError("checksum", "Serial fixed-checksum mismatch")
                if ack and response[2] != 0x02:
                    raise MotionError("command_rejected", f"Axis {address} rejected opcode 0x{opcode:02x}: 0x{response[2]:02x}")
                if interruptible:
                    self._check_cancel(cancel_event)
                return bytes(response)
            except MotionError as error:
                pending = b""
                diagnostic_error = None
                try:
                    queued = self._serial.in_waiting
                    if queued:
                        previous_timeout = self._serial.timeout
                        try:
                            self._serial.timeout = 0
                            capture_length = min(queued, 256)
                            while len(pending) < capture_length:
                                chunk = self._serial.read(capture_length - len(pending))
                                if not chunk:
                                    break
                                pending += chunk
                        finally:
                            self._serial.timeout = previous_timeout
                except Exception as capture_error:
                    diagnostic_error = str(capture_error)
                error.wire_diagnostic = {"address": address, "opcode": opcode,
                                         "expected_length": response_length, "response_hex": response.hex(),
                                         "pending_hex": pending.hex(), "diagnostic_error": diagnostic_error}
                wire_error = {"code": error.code, "message": str(error), "host_unix_s": time.time(),
                              "monotonic_s": time.monotonic(), **error.wire_diagnostic}
                raise
            except Exception as error:
                if isinstance(error, OSError):
                    self._transport_detached = True
                    try:
                        self._close_port()
                    except OSError:
                        pass
                raise MotionError("serial_error", str(error)) from error
            finally:
                if transmit:
                    self._last_exchange_monotonic = time.monotonic()
                if transmit:
                    self._event("wire", direction="tx", address=address, opcode=opcode, **transmit)
                if response:
                    self._event("wire", direction="rx", address=address, opcode=opcode,
                                frame_hex=response.hex(), **(received or {}))
                if wire_error:
                    self._event("wire_error", **wire_error)

    def _identity(self, axis, **options):
        address = self.axes[axis]["address"]
        version = self._transact(bytes([address, 0x1F]), 7, **options)
        firmware = int.from_bytes(version[2:4], "big")
        hardware = int.from_bytes(version[4:6], "big")
        if not firmware or hardware == 0xFFFF or firmware == 0xFFFF:
            raise MotionError("unsupported_identity", f"{axis} returned an invalid identity")
        response = self._transact(bytes([address, 0x42, 0x6C]), 33, **options)
        if response[2:4] != b"\x21\x15":
            raise MotionError("unsupported_config", f"{axis} has an unsupported configuration layout")
        data = response[4:-1]
        if (data[0] not in (0x19, 0x32) or data[1] != 2 or data[2] != 2
                or data[4] != 0 or data[16] != address or data[17] != 0
                or data[18] != 1 or data[19] not in (0, 1)):
            raise MotionError("unsupported_config", f"{axis} requires known motor, FOC/UART, CW convention, matching ID, fixed checksum, receive-only ACK and no auto-zero protection")
        option_status = self._transact(bytes([address, 0x1A]), 5, **options)
        if option_status[2] & 0x80:
            raise MotionError("unsupported_scaling", f"{axis} has unsupported 10x speed input scaling")
        microstep = data[5] or 256
        step_angle = 1.8 if data[0] == 0x19 else 0.9
        position_window = int.from_bytes(data[26:28], "big") / 10
        if position_window > step_angle:
            raise MotionError("unsupported_position_window", f"{axis} position window exceeds one full motor step")
        config = {"microstep": microstep, "step_angle": step_angle,
                  "pulses_per_rev": microstep * (200 if data[0] == 0x19 else 400),
                  "response_mode": data[18], "stall_protect": data[19],
                  "position_window_deg": position_window,
                  "raw_hex": response.hex(), "options_hex": option_status.hex()}
        with self._state_lock:
            self._state[axis].update(firmware_version=firmware, hardware_version=hardware, config=config)

    def _read_axis(self, axis, *, deadline=None, cancel_event=None, interruptible=False, keyboard_read=False):
        if self._responsive_keyboard:
            return self._read_axis_summary(axis, deadline=deadline, cancel_event=cancel_event,
                                           interruptible=interruptible, keyboard_read=keyboard_read)
        sample_start = time.monotonic()
        config = self.axes[axis]
        address = config["address"]
        options = {"deadline": deadline, "cancel_event": cancel_event, "interruptible": interruptible, "keyboard_read": keyboard_read}
        with self._serial_lock:
            position = self._transact(bytes([address, 0x36]), 8, **options)
            speed = self._transact(bytes([address, 0x35]), 6, **options)
            status = self._transact(bytes([address, 0x3A]), 4, **options)[2]
        if position[2] not in (0, 1) or speed[2] not in (0, 1):
            raise MotionError("invalid_telemetry", f"{axis} returned an invalid sign byte")
        raw_count = int.from_bytes(position[3:7], "big") * (-1 if position[2] else 1)
        rpm = int.from_bytes(speed[3:5], "big") * (-1 if speed[2] else 1)
        return self._update_axis_telemetry(axis, raw_count, rpm, status, sample_start)

    def _read_axis_summary(self, axis, **options):
        sample_start = time.monotonic()
        response = self._transact(bytes([self.axes[axis]["address"], 0x43, 0x7A]), 31, **options)
        if response[2:4] != bytes([31, 9]) or any(response[index] not in (0, 1) for index in (10, 15, 18, 23)):
            raise MotionError("invalid_telemetry", f"{axis} returned an invalid combined Emm status layout")
        raw_count = int.from_bytes(response[19:23], "big") * (-1 if response[18] else 1)
        rpm = int.from_bytes(response[16:18], "big") * (-1 if response[15] else 1)
        target = int.from_bytes(response[11:15], "big") * (-1 if response[10] else 1)
        error = int.from_bytes(response[24:28], "big") * (-1 if response[23] else 1)
        return self._update_axis_telemetry(axis, raw_count, rpm, response[29], sample_start,
                                           input_target_count=target, position_error_count=error)

    def _update_axis_telemetry(self, axis, raw_count, rpm, status, sample_start, **extra):
        config = self.axes[axis]
        direction = -1 if config["positive_direction"] else 1
        with self._state_lock:
            state = self._state[axis]
            if time.monotonic() - sample_start > 2:
                state["telemetry_valid"] = False
                raise MotionError("invalid_telemetry", f"{axis} feedback expired while being read")
            baseline = state["power_flag_baseline"]
            if baseline is not None and baseline != status & 0x80:
                raise MotionError("power_flag_changed", f"{axis} power flag changed; reference is invalid")
            state.update(raw_count=raw_count, speed_mm_s=direction * rpm * config["lead_mm"] / 60,
                         speed_rpm=rpm, status=status, enabled=bool(status & 1),
                         telemetry_valid=True, updated_monotonic=sample_start,
                         power_flag_baseline=status & 0x80, **extra)
            trace = getattr(self, "_keyboard_trace_context", None)
            if (trace and trace.get("axis") == axis and trace.get("command_sent")
                    and not trace.get("motion_observed")
                    and (rpm != 0 or isinstance(trace.get("baseline_count"), int)
                         and abs(raw_count - trace["baseline_count"]) > self._encoder_stability_counts(state))):
                trace["motion_observed"] = True
                self._event("keyboard_first_motion_feedback", raw_count=raw_count, speed_rpm=rpm,
                            sample_start_monotonic_s=sample_start,
                            basis="first sampled encoder/speed change, not exact mechanical onset")
            if state["reference_count"] is not None:
                state["position_mm"] = direction * (raw_count - state["reference_count"]) * config["lead_mm"] / 65536
            return copy.deepcopy(state)

    def enable_responsive_keyboard(self):
        with self._operation():
            self._require_connected()
            if self._responsive_keyboard:
                return
            for axis in self.axes:
                combined = self._read_axis_summary(axis)
                separate = self._read_axis(axis)
                target = self._transact(bytes([self.axes[axis]["address"], 0x33]), 8)
                self._healthy(axis, combined)
                self._healthy(axis, separate)
                if (combined["speed_rpm"] != 0 or separate["speed_rpm"] != 0
                        or combined["status"] != separate["status"]
                        or target[2] not in (0, 1)
                        or combined["input_target_count"] != int.from_bytes(target[3:7], "big") * (-1 if target[2] else 1)
                        or abs(combined["raw_count"] - separate["raw_count"]) > self._encoder_stability_counts(separate)):
                    raise MotionError("invalid_telemetry", "Combined and separate stationary feedback disagree")
            self._responsive_keyboard = True
            self._event("keyboard_combined_telemetry_verified")

    def _healthy(self, axis, state, *, require_enabled=False):
        if state["status"] & 0x3C:
            raise MotionError("axis_fault", f"{axis} reports stall/protection/limit status 0x{state['status']:02x}")
        if require_enabled and (not state["enabled"] or not state["enable_confirmed"]):
            raise MotionError("not_enabled", f"{axis} needs explicit enable and enabled readback")

    def connect(self):
        with self._operation():
            self._require_manual_clear()
            if self._connected:
                raise MotionError("already_connected", "Close the current motion connection with confirmed stopping before reconnecting")
            self._close_port()
            with self._state_lock:
                self._state = self._empty_state()
                self._faulted = False
                self._last_stop = None
                self._last_stop_generation = None
                self._last_stop_samples = None
                self._last_stop_completed = None
                self._responsive_keyboard = False
                self._absolute_limits = None
                self._experiment_calibration = None
                self._connection_token = str(uuid.uuid4())
            self._stop_event.clear()
            try:
                factory = self._serial_factory
                if factory is None:
                    from serial import Serial
                    factory = Serial
                self._serial = factory(port=self.port, baudrate=self.baud,
                                       timeout=min(self._timeout, 0.02), write_timeout=self._timeout)
                self._serial_opened_once = True
                self._serial.reset_input_buffer()
                for axis in self.axes:
                    self._identity(axis)
                    state = self._read_axis(axis)
                    self._healthy(axis, state)
                self._connected = True
                self._transport_detached = False
                self._event("connected", axes=copy.deepcopy(self.axes))
                return self.snapshot()
            except Exception as error:
                self._fault(error)
                self._close_port()
                if isinstance(error, MotionError):
                    raise
                raise MotionError("connect_failed", str(error)) from error

    def snapshot(self):
        with self._state_lock:
            axes = copy.deepcopy(self._state)
            for state in axes.values():
                if state["updated_monotonic"] is None or time.monotonic() - state["updated_monotonic"] > 2:
                    state["telemetry_valid"] = False
            return {"connected": self._connected, "port": self.port, "baud": self.baud,
                    "connection_token": self._connection_token, "stop_generation": self._stop_generation,
                    "absolute_limits": copy.deepcopy(self._absolute_limits),
                    "experiment_calibration": copy.deepcopy(self._experiment_calibration),
                    "absolute_limits_required": self.require_absolute_limits,
                    "automatic_motion_axis": self._automatic_motion_axis,
                    "serial_opened_once": self._serial_opened_once,
                    "startup_approach_pending": self._startup_approach is not None,
                    "faulted": self._faulted, "axes": axes,
                    "referenced": all(state["referenced"] for state in axes.values()),
                    "position": {axis: state["position_mm"] for axis, state in axes.items()},
                    "manual": {**copy.deepcopy(self._manual), "supported": self.manual_calibration_supported,
                               "capture_supported": self.manual_capture_supported,
                               "boundaries": self._keyboard_boundaries()},
                    "stop_confirmed": None if self._last_stop is None else self._last_stop["stop_confirmed"]}

    def refresh(self):
        with self._operation():
            self._require_connected()
            try:
                for axis in self.axes:
                    state = self._read_axis(axis)
                    self._healthy(axis, state)
                return self.snapshot()
            except MotionError as error:
                self._fault(error)
                if self._manual.get("active") and self._manual.get("control_mode") == "keyboard":
                    error.stop_result = self.stop_all()
                raise

    def refresh_keyboard_alignment(self, cancel_event=None):
        if not self._responsive_keyboard:
            return self.refresh()
        with self._operation():
            self._keyboard_session()
            generation = self._stop_generation
            try:
                for axis in self.axes:
                    self._check_cancel(cancel_event)
                    state = self._read_axis(axis, keyboard_read=True)
                    self._healthy(axis, state, require_enabled=True)
                    self._keyboard_session(generation)
                    self._check_cancel(cancel_event)
                return self.snapshot()
            except MotionError as error:
                if error.code != "cancelled":
                    self._fault(error)
                    error.stop_result = self.stop_all()
                raise

    def confirm_reference(self, confirmed=True):
        if confirmed is not True:
            raise MotionError("reference_confirmation", "Reference requires operator confirmation")
        with self._operation():
            self._require_manual_clear()
            self._require_connected()
            with self._state_lock:
                self._startup_approach = None
                self._completed_motion_targets = {}
                generation = self._stop_generation
            try:
                states = {axis: self._read_axis(axis) for axis in self.axes}
                for axis, state in states.items():
                    self._healthy(axis, state)
                    if state["speed_rpm"] != 0:
                        raise MotionError("axis_moving", f"{axis} must be stationary to confirm reference")
                with self._state_lock:
                    if self._stop_generation != generation:
                        raise MotionError("cancelled", "Stop interrupted reference confirmation")
                    for axis, state in states.items():
                        self._state[axis].update(reference_count=state["raw_count"], position_mm=0.0, referenced=True)
                    self._stop_event.clear()
                self._event("reference_confirmed", raw_counts={axis: state["raw_count"] for axis, state in states.items()})
                return self.snapshot()
            except MotionError as error:
                self._fault(error)
                raise

    def enable(self, axis):
        config = self._axis(axis)
        with self._operation():
            self._require_manual_clear()
            self._require_connected()
            try:
                self._check_cancel()
                state = self._read_axis(axis, interruptible=True)
                self._healthy(axis, state)
                self._transact(bytes([config["address"], 0xF3, 0xAB, 1, 0]), 4, ack=True, interruptible=True)
                state = self._read_axis(axis, interruptible=True)
                self._healthy(axis, state)
                if not state["enabled"]:
                    raise MotionError("enable_unconfirmed", f"{axis} enable was not confirmed by status")
                with self._state_lock:
                    self._state[axis]["enable_confirmed"] = True
                self._event("enabled", axis=axis)
                return self.snapshot()
            except MotionError as error:
                self._fault(error)
                raise

    def move_to(self, axis, target_mm, speed_mm_s, cancel_event=None):
        from .motion_approach import FINAL_SPEED_MM_S, endpoint_phases

        config = self._axis(axis)
        target = _number(target_mm, "target_mm")
        speed = _number(speed_mm_s, "speed_mm_s", positive=True)
        with self._state_lock:
            calibration = copy.deepcopy(self._experiment_calibration)
            approach = copy.deepcopy(self._startup_approach)
        endpoints = (() if calibration is None else
                     (calibration["x_first_mm"], calibration["x_last_mm"]) if axis == "X" else
                     (calibration["z_safe_mm"], calibration["z_working_mm"]))
        if (calibration is None or speed <= FINAL_SPEED_MM_S
                or approach is None and not any(abs(target - endpoint) <= 1e-12 for endpoint in endpoints)):
            return self._move_to_target(axis, target, speed, cancel_event)
        if speed > config["max_speed_mm_s"] or not 0 <= target <= config["travel_mm"]:
            return self._move_to_target(axis, target, speed, cancel_event)
        with self._operation():
            started = time.monotonic()
            deadline = started + (self._move_timeout or 28800)
            try:
                self._require_manual_clear()
                self._require_connected()
                self._check_cancel(cancel_event)
                with self._state_lock:
                    identity = (self._stop_generation, self._connection_token,
                                copy.deepcopy(self._experiment_calibration))
                if identity[2] != calibration:
                    raise MotionError("calibration_motion", "Endpoint calibration changed before approach")
                if deadline - time.monotonic() <= 0.02:
                    raise MotionError("move_timeout", f"{axis} endpoint deadline cannot fit stationary verification")
                states, bounds = self._read_experiment_axes(calibration, verify_profiles=True, stationary=True,
                    deadline=deadline, cancel_event=cancel_event, interruptible=True)
                if approach is not None and (axis != "Z" or target != approach["target_z_mm"]):
                    raise MotionError("calibration_approach_required", "The initial approach must reach the recorded safe height first")
                state = states[axis]
                pulse_mm = config["lead_mm"] / state["config"]["pulses_per_rev"]
                tolerance = max(config["lead_mm"] / 65536,
                                config["lead_mm"] * state["config"]["position_window_deg"] / 360)
                phases = endpoint_phases(state["position_mm"], target, speed, bounds[axis],
                                         pulse_mm, tolerance, startup=approach is not None)
                for _phase_target, phase_speed in phases:
                    phase_rpm = math.floor(phase_speed * 60 / config["lead_mm"] + 1e-12)
                    if not 1 <= phase_rpm <= 6000:
                        raise MotionError("speed_range", "Endpoint approach speed cannot be represented without exceeding its cap")
                if not self._move_timeout:
                    previous = state["position_mm"]
                    budget = 0
                    for phase_target, phase_speed in phases:
                        phase_rpm = math.floor(phase_speed * 60 / config["lead_mm"] + 1e-12)
                        budget += abs(phase_target - previous) / (phase_rpm * config["lead_mm"] / 60) * 1.5 + 5
                        previous = phase_target
                    if budget > 28800:
                        raise MotionError("move_duration", "Endpoint approach deadline exceeds eight hours")
                    deadline = started + budget
                held_counts = {name: value["raw_count"] for name, value in states.items() if name != axis}
                for phase_index, (phase_target, phase_speed) in enumerate(phases):
                    self._check_cancel(cancel_event)
                    with self._state_lock:
                        current_identity = (self._stop_generation, self._connection_token, self._experiment_calibration)
                        if current_identity != identity:
                            raise MotionError("calibration_motion", "Endpoint approach identity changed")
                    if deadline - time.monotonic() <= 0.02:
                        raise MotionError("move_timeout", f"{axis} endpoint deadline cannot fit stationary verification")
                    states, _bounds = self._read_experiment_axes(calibration, verify_profiles=True, stationary=True,
                        deadline=deadline, cancel_event=cancel_event, interruptible=True)
                    for held_axis, baseline in held_counts.items():
                        held = states[held_axis]
                        if abs(held["raw_count"] - baseline) > max(1, held["config"]["position_window_deg"] * 65536 / 360):
                            raise MotionError("calibration_motion", f"{held_axis} left its original held position")
                    self._event("endpoint_approach_phase", axis=axis, phase=phase_index + 1,
                                phases=len(phases), requested_target_mm=target, phase_target_mm=phase_target,
                                requested_speed_mm_s=speed, phase_speed_mm_s=phase_speed,
                                provisional_final_speed_mm_s=FINAL_SPEED_MM_S)
                    result = self._move_to_target(axis, phase_target, phase_speed, cancel_event,
                        _operation_held=True, operation_deadline=deadline, _approach_identity=identity,
                        _held_counts=held_counts, _final_phase=phase_index == len(phases) - 1)
                    self._check_cancel(cancel_event)
                    with self._state_lock:
                        if (self._stop_generation, self._connection_token, self._experiment_calibration) != identity:
                            raise MotionError("calibration_motion", "Endpoint approach identity changed after phase")
                    if phase_index != len(phases) - 1:
                        with self._state_lock:
                            self._completed_motion_targets.pop(axis, None)
                    for held_axis, baseline in held_counts.items():
                        held = result["axes"][held_axis]
                        if abs(held["raw_count"] - baseline) > max(1, held["config"]["position_window_deg"] * 65536 / 360):
                            raise MotionError("calibration_motion", f"{held_axis} left its original held position")
                return result
            except MotionError as error:
                if error.code == "transaction_timeout" and time.monotonic() >= deadline:
                    error = MotionError("move_timeout", f"{axis} endpoint approach deadline expired")
                if error.stop_result is None:
                    self._fault(error)
                    error.stop_result = self.stop_all()
                raise error

    def _move_to_target(self, axis, target_mm, speed_mm_s, cancel_event=None, *,
                        _operation_held=False, operation_deadline=None, _approach_identity=None,
                        _held_counts=None, _final_phase=True):
        config = self._axis(axis)
        target = _number(target_mm, "target_mm")
        speed = _number(speed_mm_s, "speed_mm_s", positive=True)
        if not 0 <= target <= config["travel_mm"]:
            raise MotionError("travel_limit", f"{axis} target is outside configured travel")
        rpm = math.floor(speed * 60 / config["lead_mm"] + 1e-12)
        if speed > config["max_speed_mm_s"] or not 1 <= rpm <= 6000:
            raise MotionError("speed_range", "Speed exceeds the configured maximum or is below one RPM")
        actual_speed = rpm * config["lead_mm"] / 60
        with nullcontext() if _operation_held else self._operation():
            self._require_manual_clear()
            self._require_connected()
            with self._state_lock:
                state = self._state[axis]
                if not state["referenced"]:
                    raise MotionError("not_referenced", f"{axis} requires operator reference confirmation")
                if not state["enable_confirmed"]:
                    raise MotionError("not_enabled", f"{axis} requires explicit enable")
            command_outstanding = False
            deadline = operation_deadline
            try:
                self._check_cancel(cancel_event)
                with self._state_lock:
                    calibration = copy.deepcopy(self._experiment_calibration)
                    approach = copy.deepcopy(self._startup_approach)
                if approach is not None and (axis != "Z" or target != approach["target_z_mm"]):
                    raise MotionError("calibration_approach_required", "当前位置不在记录的采集高度区间内，必须先沿 Z 到达安全高度，不能先横移或直接到工作高度。")
                calibrated_bounds = None
                held_counts = None
                if calibration is not None:
                    if operation_deadline is not None and operation_deadline - time.monotonic() <= 0.02:
                        raise MotionError("move_timeout", f"{axis} endpoint deadline cannot fit stationary verification")
                    calibrated_states, calibrated_bounds = self._read_experiment_axes(
                        calibration, verify_profiles=True, stationary=True,
                        deadline=deadline, cancel_event=cancel_event, interruptible=True)
                    held_counts = {name: value["raw_count"] for name, value in calibrated_states.items()}
                    if _held_counts is not None:
                        held_counts.update(_held_counts)
                        for held_axis, baseline in _held_counts.items():
                            held = calibrated_states[held_axis]
                            if abs(held["raw_count"] - baseline) > max(1, held["config"]["position_window_deg"] * 65536 / 360):
                                raise MotionError("calibration_motion", f"{held_axis} left its original held position")
                self._require_absolute_limits()
                if self._absolute_limits is not None:
                    self._read_limits(verify_profiles=True, deadline=deadline, cancel_event=cancel_event, interruptible=True)
                state = self._read_axis(axis, deadline=deadline, cancel_event=cancel_event, interruptible=True)
                self._healthy(axis, state, require_enabled=True)
                self._check_cancel(cancel_event)
                if state["speed_rpm"] != 0:
                    raise MotionError("axis_moving", f"{axis} is already moving")
                pulse_mm = config["lead_mm"] / state["config"]["pulses_per_rev"]
                resolution_tolerance = max(pulse_mm / 2, config["lead_mm"] / 65536)
                if not -resolution_tolerance <= state["position_mm"] <= config["travel_mm"] + resolution_tolerance:
                    raise MotionError("travel_limit", f"{axis} current position is outside configured travel")
                delta = target - state["position_mm"]
                pulses = math.floor(abs(delta) / pulse_mm + 0.5)
                if pulses > 0xFFFFFFFF:
                    raise MotionError("pulse_range", "Move exceeds protocol pulse range")
                quantized_target = state["position_mm"] + math.copysign(pulses * pulse_mm, delta)
                if calibrated_bounds is not None:
                    lower, upper = calibrated_bounds[axis]
                    if not lower <= target <= upper:
                        raise MotionError("calibration_motion", f"{axis} target exceeds the calibrated experiment window")
                    if not lower - 1e-12 <= quantized_target <= upper + 1e-12:
                        correction = (1 if delta > 0 else -1) if quantized_target < lower else (-1 if delta > 0 else 1)
                        pulses += correction
                        quantized_target = state["position_mm"] + math.copysign(pulses * pulse_mm, delta)
                        if (pulses < 0 or not lower - 1e-12 <= quantized_target <= upper + 1e-12
                                or abs(quantized_target - target) > pulse_mm + 1e-12):
                            raise MotionError("calibration_motion", f"{axis} quantized target cannot stay inside the calibrated experiment window")
                if pulses and not 0 <= quantized_target <= config["travel_mm"]:
                    pulses -= 1
                    quantized_target = state["position_mm"] + math.copysign(pulses * pulse_mm, delta)
                if pulses and not 0 <= quantized_target <= config["travel_mm"]:
                    raise MotionError("travel_limit", "Quantized target exceeds configured travel")
                tolerance = (max(config["lead_mm"] / 65536, config["lead_mm"] * state["config"]["position_window_deg"] / 360)
                             if calibration is not None else
                             max(resolution_tolerance, config["lead_mm"] * state["config"]["position_window_deg"] / 360))
                self._limit_target(axis, target, quantized_target, tolerance)
                timeout = self._move_timeout or (abs(delta) / actual_speed * 1.5 + 5)
                if timeout > 28800:
                    raise MotionError("move_duration", "Move deadline exceeds eight hours")
                deadline = min(time.monotonic() + timeout, operation_deadline or float("inf"))
                with self._state_lock:
                    self._last_stop = None
                    self._state[axis].update(command_speed_mm_s=actual_speed, target_mm=target,
                                             quantized_target_mm=quantized_target, arrival_tolerance_mm=tolerance)
                if pulses:
                    self._require_absolute_limits()
                    self._check_cancel(cancel_event)
                    if _approach_identity is not None:
                        with self._state_lock:
                            if (self._stop_generation, self._connection_token, self._experiment_calibration) != _approach_identity:
                                raise MotionError("calibration_motion", "Endpoint approach identity changed before command")
                    direction = config["positive_direction"] if delta > 0 else 1 - config["positive_direction"]
                    body = bytes([config["address"], 0xFD, direction]) + rpm.to_bytes(2, "big") + bytes([10]) + pulses.to_bytes(4, "big") + bytes([2, 0])
                    command_outstanding = True
                    with self._state_lock:
                        self._automatic_motion_axis = axis
                    self._transact(body, 4, deadline=deadline, ack=True,
                                   cancel_event=cancel_event, interruptible=True)
                stable_samples = 0
                settled_target = None
                if not pulses:
                    settled_target = self._automatic_target_observation(axis, state, quantized_target, calibration,
                        require_command_target=False, deadline=deadline, cancel_event=cancel_event)
                while True:
                    self._check_cancel(cancel_event)
                    if time.monotonic() >= deadline:
                        raise MotionError("move_timeout", f"{axis} did not reach target before deadline")
                    try:
                        state = self._read_axis(axis, deadline=deadline, cancel_event=cancel_event, interruptible=True)
                    except MotionError as error:
                        if error.code == "transaction_timeout" and time.monotonic() >= deadline:
                            raise MotionError("move_timeout", f"{axis} move deadline expired") from error
                        raise
                    self._healthy(axis, state, require_enabled=True)
                    self._guard_automatic_axis(axis, state, calibration)
                    self._check_cancel(cancel_event)
                    reached = (abs(state["position_mm"] - quantized_target) <= tolerance + 1e-12
                               and state["speed_rpm"] == 0 and bool(state["status"] & 2))
                    stable_samples = stable_samples + 1 if reached else 0
                    if stable_samples >= 2:
                        if command_outstanding:
                            state, settled_target = self._settle_automatic_axis(axis, quantized_target, tolerance, calibration,
                                deadline=deadline, cancel_event=cancel_event)
                            command_outstanding = False
                        with self._state_lock:
                            self._startup_approach = None
                        if calibration is not None:
                            states, _bounds = self._read_experiment_axes(calibration, verify_profiles=True,
                                stationary=True, deadline=deadline, cancel_event=cancel_event, interruptible=True)
                            for held_axis, held_state in states.items():
                                if held_axis == axis:
                                    continue
                                held_tolerance = max(1, held_state["config"]["position_window_deg"] * 65536 / 360)
                                if (held_state["speed_rpm"] != 0 or not held_state["status"] & 2
                                        or abs(held_state["raw_count"] - held_counts[held_axis]) > held_tolerance):
                                    raise MotionError("calibration_motion", f"{held_axis} left its uncommanded held position")
                            state = states[axis]
                        if self._absolute_limits is not None:
                            states = self._read_limits(verify_profiles=True, deadline=deadline,
                                                       cancel_event=cancel_event, interruptible=True)
                            state = states[axis]
                        if calibration is not None or self._absolute_limits is not None:
                            state = self._read_axis(axis, deadline=deadline, cancel_event=cancel_event, interruptible=True)
                            self._healthy(axis, state, require_enabled=True)
                            self._guard_automatic_axis(axis, state, calibration)
                        self._check_cancel(cancel_event)
                        if (state["speed_rpm"] != 0 or not state["status"] & 2
                                or abs(state["position_mm"] - quantized_target) > tolerance + 1e-12):
                            raise MotionError("calibration_motion", f"{axis} changed after its target was confirmed stopped")
                        final_target = self._automatic_target_observation(axis, state, quantized_target, calibration,
                            require_command_target=bool(pulses), deadline=deadline, cancel_event=cancel_event)
                        if final_target is None or final_target != settled_target:
                            raise MotionError("calibration_motion", f"{axis} target changed during final arrival verification")
                        with self._state_lock:
                            self._check_cancel(cancel_event)
                            self._completed_motion_targets[axis] = {"target_mm": quantized_target,
                                "target_signature": final_target, "require_command_target": bool(pulses),
                                "generation": self._stop_generation, "connection_token": self._connection_token,
                                "reference_count": self._state[axis]["reference_count"]}
                            self._automatic_motion_axis = None
                        self._event("arrived" if _final_phase else "endpoint_approach_stage_arrived", axis=axis, target_mm=target, position_mm=state["position_mm"],
                                    quantized_target_mm=quantized_target,
                                    quantized_position_error_mm=state["position_mm"] - quantized_target,
                                    position_error_mm=state["position_mm"] - target, arrival_tolerance_mm=tolerance,
                                    requested_speed_mm_s=speed, command_speed_mm_s=actual_speed)
                        return self.snapshot()
                    self._stop_event.wait(min(0.02, max(0, deadline - time.monotonic())))
            except MotionError as error:
                if error.code == "transaction_timeout" and deadline is not None and time.monotonic() >= deadline:
                    timeout_error = MotionError("move_timeout", f"{axis} move deadline expired")
                    if isinstance(getattr(error, "wire_diagnostic", None), dict):
                        timeout_error.wire_diagnostic = copy.deepcopy(error.wire_diagnostic)
                    error = timeout_error
                if command_outstanding:
                    try:
                        self._transact(bytes([config["address"], 0xFE, 0x98, 0]), 4, ack=True, stop_command=True)
                    except MotionError as stop_error:
                        self._event("active_axis_stop_failed", axis=axis, error=str(stop_error))
                self._fault(error)
                error.stop_result = self.stop_all()
                raise error
            finally:
                with self._state_lock:
                    self._automatic_motion_axis = None

    def _guard_automatic_axis(self, axis, state, calibration):
        if calibration is not None:
            from .calibrated_motion import check_calibrated_approach_feedback, check_calibrated_axis_feedback
            try:
                if self._startup_approach is not None:
                    check_calibrated_approach_feedback(self.snapshot(), calibration, self._startup_approach, {axis})
                else:
                    check_calibrated_axis_feedback(self.snapshot(), calibration, axis)
            except ValueError as error:
                raise MotionError("calibration_motion", str(error)) from error
        self._limit_axis_state(axis, state)

    def _automatic_target_observation(self, axis, state, target, calibration, *, require_command_target=True,
                                      deadline=None, cancel_event=None):
        if not self._responsive_keyboard and calibration is None:
            return ()
        if self._responsive_keyboard:
            if not self._keyboard_jog_ready(state):
                return None
            observed = state["input_target_count"]
        else:
            observed = self._read_manual_target(axis, 0x33, manual_session=False, deadline=deadline,
                                               cancel_event=cancel_event)
            instantaneous = self._read_manual_target(axis, 0x34, manual_session=False, deadline=deadline,
                                                     cancel_event=cancel_event)
            if abs(observed - instantaneous) > 1 or abs(state["raw_count"] - observed) > self._keyboard_jog_counts(state):
                return None
        if require_command_target:
            from .keyboard_fast_jog import _target_representation_budget
            mechanical = self.axes[axis]
            sign = -1 if mechanical["positive_direction"] else 1
            expected = state["reference_count"] + sign * target * 65536 / mechanical["lead_mm"]
            budget = _target_representation_budget(65536 / state["config"]["pulses_per_rev"])
            if abs(observed - expected) > budget:
                raise MotionError("calibration_motion", f"{axis} reported a different finite command target")
        return (observed,)

    def _settle_automatic_axis(self, axis, target, tolerance, calibration, *, deadline, cancel_event,
                             require_command_target=True):
        quiet_since = anchor = signature = previous_time = None
        stable_samples = 0
        quiet_duration = 0.2 if calibration is not None or self._absolute_limits is not None or self._responsive_keyboard else 0
        while time.monotonic() < deadline:
            self._check_cancel(cancel_event)
            state = self._read_axis(axis, deadline=deadline, cancel_event=cancel_event, interruptible=True)
            self._healthy(axis, state, require_enabled=True)
            self._guard_automatic_axis(axis, state, calibration)
            self._check_cancel(cancel_event)
            reached = (state["speed_rpm"] == 0 and bool(state["status"] & 2)
                       and abs(state["position_mm"] - target) <= tolerance + 1e-12)
            observed = (self._automatic_target_observation(axis, state, target, calibration,
                require_command_target=require_command_target, deadline=deadline, cancel_event=cancel_event) if reached else None)
            if not reached or observed is None:
                quiet_since = anchor = signature = previous_time = None
                stable_samples = 0
            elif (anchor is None or observed != signature
                    or abs(state["raw_count"] - anchor) > self._encoder_stability_counts(state)):
                quiet_since, anchor, signature = state["updated_monotonic"], state["raw_count"], observed
                previous_time = state["updated_monotonic"]
                stable_samples = 0
            elif state["updated_monotonic"] > previous_time:
                previous_time = state["updated_monotonic"]
                stable_samples = stable_samples + 1 if previous_time - quiet_since >= quiet_duration else 0
                if stable_samples >= 2:
                    self._check_cancel(cancel_event)
                    self._event("finite_motion_settled", axis=axis, position_mm=state["position_mm"],
                                quiet_s=previous_time - quiet_since, target_signature=list(signature),
                                basis="natural_finite_completion")
                    return state, signature
            self._stop_event.wait(min(.02, max(0, deadline - time.monotonic())))
        raise MotionError("move_timeout", f"{axis} did not naturally settle at its finite command target")

    def _guard_experiment_feedback(self, record):
        from .calibrated_motion import check_calibrated_approach_feedback, check_calibrated_feedback
        if record.get("motion") != {"port": self.port, "baud": self.baud, "axes": self.axes}:
            raise MotionError("calibration_motion", "Current mechanical settings differ from the trusted experiment calibration")
        try:
            if self._startup_approach is not None:
                current = self.snapshot()
                for axis in self.axes:
                    state = current["axes"][axis]
                    tolerance = max(1, state["config"]["position_window_deg"] * 65536 / 360)
                    if abs(state["raw_count"] - self._startup_approach["start_raw_counts"][axis]) > tolerance:
                        raise MotionError("calibration_motion", f"{axis} changed before the initial safe-height approach")
                return check_calibrated_approach_feedback(current, record, self._startup_approach)
            return check_calibrated_feedback(self.snapshot(), record)
        except ValueError as error:
            raise MotionError("calibration_motion", str(error)) from error

    def verify_natural_park(self, record, cancel_event=None):
        with self._operation():
            self._require_connected()
            self._require_manual_clear()
            self._check_cancel(cancel_event)
            with self._state_lock:
                generation = self._stop_generation
                targets = copy.deepcopy(self._completed_motion_targets)
                if (record != self._experiment_calibration or not isinstance(record, dict)
                        or self._startup_approach is not None or self._automatic_motion_axis is not None
                        or set(targets) != set(self.axes)):
                    raise MotionError("calibration_motion", "A fully completed trusted finite plan is required before natural parking")
                for axis, target in targets.items():
                    if (target["generation"] != generation or target["connection_token"] != self._connection_token
                            or target["reference_count"] != self._state[axis]["reference_count"]):
                        raise MotionError("calibration_motion", "The completed finite targets no longer match this reference")
            deadline = time.monotonic() + max(2.0, self._timeout * 8 + 3)
            try:
                self._read_experiment_axes(record, verify_profiles=True, stationary=True, deadline=deadline,
                                           cancel_event=cancel_event, interruptible=True)
                for axis, target in targets.items():
                    profile = self._state[axis]["config"]
                    tolerance = max(self.axes[axis]["lead_mm"] / 65536,
                                    self.axes[axis]["lead_mm"] * profile["position_window_deg"] / 360)
                    state, observed = self._settle_automatic_axis(axis, target["target_mm"], tolerance, record,
                        require_command_target=target["require_command_target"], deadline=deadline, cancel_event=cancel_event)
                    if observed != target["target_signature"]:
                        raise MotionError("calibration_motion", f"{axis} target changed before natural parking")
                self._read_experiment_axes(record, verify_profiles=True, stationary=True, deadline=deadline,
                                           cancel_event=cancel_event, interruptible=True)
                states = {}
                for axis, target in targets.items():
                    state = self._read_axis(axis, deadline=deadline, cancel_event=cancel_event, interruptible=True)
                    self._healthy(axis, state, require_enabled=True)
                    self._guard_automatic_axis(axis, state, record)
                    observed = self._automatic_target_observation(axis, state, target["target_mm"], record,
                        require_command_target=target["require_command_target"], deadline=deadline, cancel_event=cancel_event)
                    tolerance = max(self.axes[axis]["lead_mm"] / 65536,
                                    self.axes[axis]["lead_mm"] * state["config"]["position_window_deg"] / 360)
                    if (observed != target["target_signature"] or state["speed_rpm"] != 0 or not state["status"] & 2
                            or abs(state["position_mm"] - target["target_mm"]) > tolerance + 1e-12):
                        raise MotionError("calibration_motion", f"{axis} natural parking verification changed")
                    states[axis] = state
                with self._state_lock:
                    self._check_cancel(cancel_event)
                    if generation != self._stop_generation:
                        raise MotionError("cancelled", "STOP interrupted natural parking verification")
                    self._guard_experiment_feedback(record)
                    proof = {"basis": "natural_finite_completion", "connection_token": self._connection_token,
                             "stop_generation": generation, "reference_identity": self._reference_identity(states),
                             "targets": targets}
                self._event("natural_park_verified", proof=copy.deepcopy(proof))
                return proof
            except MotionError as error:
                if error.code != "cancelled":
                    self._fault(error)
                raise

    def _read_experiment_axes(self, record, *, verify_profiles=False, stationary=False, **options):
        if verify_profiles:
            for axis in self.axes:
                self._identity(axis, **options)
        states = self._stationary_states(**options) if stationary else {axis: self._read_axis(axis, **options) for axis in self.axes}
        for axis, state in states.items():
            self._healthy(axis, state, require_enabled=True)
        return states, self._guard_experiment_feedback(record)

    def install_experiment_calibration(self, record):
        with self._operation():
            with self._state_lock:
                self._startup_approach = None
                self._completed_motion_targets = {}
            if record is None:
                with self._state_lock:
                    self._experiment_calibration = None
                    self._startup_approach = None
                return self.snapshot()
            self._require_connected()
            self._require_manual_clear()
            try:
                from .guided_automation import _validate_saved_calibration
                try:
                    record = _validate_saved_calibration(record)
                except (ValueError, KeyError, TypeError, AttributeError) as error:
                    raise MotionError("calibration_motion", f"Invalid trusted experiment calibration: {error}") from error
                if set(record["points"]) != {"first", "last", "working", "safe"} or record["reference_valid"] is not True:
                    raise MotionError("calibration_motion", "A trusted current-session four-point calibration is required")
                with self._state_lock:
                    generation = self._stop_generation
                self._check_cancel()
                for axis in self.axes:
                    self._identity(axis)
                states = self._stationary_states(enabled=True)
                current_motion = {"port": self.port, "baud": self.baud, "axes": self.axes}
                if (record.get("connection_token") != self._connection_token or record["motion"] != current_motion
                        or record["reference"]["profiles"] != self._reference_profiles()
                        or any(not state["referenced"] or state["reference_count"] != record["reference"]["reference_counts"][axis]
                               for axis, state in states.items())):
                    raise MotionError("calibration_motion", "The experiment calibration connection, profile or reference changed")
                initial_counts = {axis: state["raw_count"] for axis, state in states.items()}
                target_samples = []
                for _sample in range(2):
                    targets = {axis: {"target_count": self._read_manual_target(axis, 0x33, manual_session=False),
                                      "instantaneous_target_count": self._read_manual_target(axis, 0x34, manual_session=False)}
                               for axis in self.axes}
                    states = self._stationary_states(enabled=True)
                    for axis, state in states.items():
                        tolerance = max(1, state["config"]["position_window_deg"] * 65536 / 360)
                        if (abs(state["raw_count"] - initial_counts[axis]) > 1
                                or any(abs(target - state["raw_count"]) > tolerance for target in targets[axis].values())):
                            raise MotionError("calibration_motion", f"{axis} locked target positions cannot be confirmed at the current encoder")
                    target_samples.append(targets)
                with self._state_lock:
                    if generation != self._stop_generation:
                        raise MotionError("cancelled", "STOP interrupted experiment calibration installation")
                    self._check_cancel()
                    for state in self._state.values():
                        state["enable_confirmed"] = True
                    from .calibrated_motion import calibrated_startup_approach
                    try:
                        self._startup_approach = calibrated_startup_approach(self.snapshot(), record)
                    except ValueError as error:
                        raise MotionError("calibration_motion", str(error)) from error
                    self._guard_experiment_feedback(record)
                    self._experiment_calibration = copy.deepcopy(record)
                self._event("experiment_calibration_installed", calibration_id=record["id"],
                            raw_counts={axis: state["raw_count"] for axis, state in states.items()},
                            target_samples=target_samples)
                return self.snapshot()
            except MotionError as error:
                self._fault(error)
                error.stop_result = self.stop_all()
                raise

    def _require_absolute_limits(self):
        from .motion_limits import binding_matches
        with self._state_lock:
            record = self._absolute_limits
            if record is None:
                if self.require_absolute_limits:
                    raise MotionError("absolute_limits_required", "Four verified operating boundaries are required")
                return
            if not binding_matches(record, self.snapshot()):
                self._absolute_limits = None
                raise MotionError("absolute_limits_stale", "Absolute limit connection or motor profile changed")

    def _limit_states(self, states):
        for axis in self.axes:
            self._limit_axis_state(axis, states[axis])

    def _limit_axis_state(self, axis, state):
        self._require_absolute_limits()
        if self._absolute_limits is None:
            return
        bounds = self._absolute_limits["boundaries"]
        low, high = ("x_right", "x_left") if axis == "X" else ("z_up", "z_down")
        if not bounds[low] <= state["raw_count"] <= bounds[high]:
            raise MotionError("absolute_limit_violation", f"{axis} raw position exceeds captured operating boundaries")

    def _read_limits(self, *, verify_profiles=False, **options):
        if verify_profiles:
            for axis in self.axes:
                self._identity(axis, **options)
        states = {axis: self._read_axis(axis, **options) for axis in self.axes}
        for axis, state in states.items():
            self._healthy(axis, state)
        self._limit_states(states)
        return states

    def _limit_target(self, axis, requested, quantized, tolerance):
        with self._state_lock:
            self._require_absolute_limits()
            if self._absolute_limits is None:
                return
            bounds = copy.deepcopy(self._absolute_limits["boundaries"])
            reference = self._state[axis]["reference_count"]
        if reference is None:
            raise MotionError("not_referenced", f"{axis} reference was invalidated")
        config = self.axes[axis]
        sign = -1 if config["positive_direction"] else 1
        low, high = (bounds["x_right"], bounds["x_left"]) if axis == "X" else (bounds["z_up"], bounds["z_down"])
        margin = tolerance * 65536 / config["lead_mm"]
        for target in (requested, quantized):
            raw_target = reference + sign * target * 65536 / config["lead_mm"]
            if not low + margin <= raw_target <= high - margin:
                raise MotionError("absolute_limit_violation", f"{axis} target and arrival margin exceed captured operating boundaries")

    def capture_limit_position(self):
        with self._operation():
            self._require_connected()
            self._require_manual_clear()
            with self._state_lock:
                generation = self._stop_generation
            try:
                for axis in self.axes:
                    self._identity(axis)
                states = self._stationary_states()
                with self._state_lock:
                    if generation != self._stop_generation:
                        raise MotionError("cancelled", "STOP interrupted operating-boundary capture")
                    return {"version": "emm-limit-position-1", "connection_token": self._connection_token,
                            "stop_generation": generation, "motion": {"port": self.port, "baud": self.baud,
                            "axes": copy.deepcopy(self.axes)}, "profiles": self._reference_profiles(),
                            "raw_counts": {axis: state["raw_count"] for axis, state in states.items()}}
            except MotionError as error:
                self._fault(error)
                raise

    def install_absolute_limits(self, record):
        if record is None:
            with self._state_lock:
                self._absolute_limits = None
            return self.snapshot()
        from .motion_limits import binding_matches, validate_limit_record
        record = validate_limit_record(record)
        with self._operation():
            self._require_connected()
            self._require_manual_clear()
            with self._state_lock:
                generation = self._stop_generation
                if not binding_matches(record, self.snapshot(), generation=True):
                    raise MotionError("absolute_limits_stale", "Captured connection, profile or STOP identity changed")
            for axis in self.axes:
                self._identity(axis)
            states = self._stationary_states()
            with self._state_lock:
                if generation != self._stop_generation or not binding_matches(record, self.snapshot(), generation=True):
                    raise MotionError("cancelled", "STOP or profile change interrupted operating-boundary installation")
                self._absolute_limits = record
                try:
                    self._limit_states(states)
                except MotionError:
                    self._absolute_limits = None
                    raise
            return self.snapshot()

    def check_absolute_limits(self):
        with self._operation():
            self._require_connected()
            try:
                self._require_absolute_limits()
                self._read_limits(verify_profiles=True)
                return self.snapshot()
            except MotionError as error:
                self._fault(error)
                error.stop_result = self.stop_all()
                raise

    def preflight_absolute_limits(self, steps):
        with self._operation():
            self._require_connected()
            self._require_manual_clear()
            with self._state_lock:
                generation = self._stop_generation
            self._require_absolute_limits()
            try:
                states = self._read_limits(verify_profiles=True)
            except MotionError as error:
                self._fault(error)
                error.stop_result = self.stop_all()
                raise
            for step in steps:
                if not step.get("op", "").startswith("move_"):
                    continue
                params = step.get("resolved_params", step.get("params", {}))
                axis = params.get("axis")
                config = self._axis(axis)
                state = states[axis]
                target = _number(params.get("target_mm"), "target_mm")
                if not state["referenced"]:
                    raise MotionError("not_referenced", f"{axis} requires reference confirmation")
                if not 0 <= target <= config["travel_mm"]:
                    raise MotionError("travel_limit", f"{axis} target exceeds configured travel")
                pulse_mm = config["lead_mm"] / state["config"]["pulses_per_rev"]
                tolerance = max(pulse_mm / 2, config["lead_mm"] / 65536,
                                config["lead_mm"] * state["config"]["position_window_deg"] / 360)
                self._limit_target(axis, target, target, tolerance + pulse_mm / 2)
            with self._state_lock:
                if generation != self._stop_generation:
                    raise MotionError("cancelled", "STOP interrupted absolute-limit preflight")
            return self.snapshot()

    @staticmethod
    def _encoder_stability_counts(state):
        profile = state["config"]
        return max(1.0, min(32768 / profile["pulses_per_rev"], profile["position_window_deg"] * 65536 / 360))

    @staticmethod
    def _keyboard_jog_counts(state):
        return max(1.0, state["config"]["position_window_deg"] * 65536 / 360)

    def _keyboard_jog_ready(self, state):
        target = state.get("input_target_count")
        return (state.get("telemetry_valid") is True and state["enabled"] and state["speed_rpm"] == 0
                and bool(state["status"] & 2) and isinstance(target, int)
                and abs(state["raw_count"] - target) <= self._keyboard_jog_counts(state)
                and abs(state["raw_count"] + state.get("position_error_count", float("inf")) - target) <= 1)

    def _keyboard_jog_pair(self, previous, state):
        return (self._keyboard_jog_ready(previous) and self._keyboard_jog_ready(state)
                and state["input_target_count"] == previous["input_target_count"]
                and 0 < state["updated_monotonic"] - previous["updated_monotonic"] <= 2
                and abs(state["raw_count"] - previous["raw_count"]) <= self._keyboard_jog_counts(state))

    def _keyboard_held_matches(self, initial, state):
        if not self._responsive_keyboard:
            return abs(state["raw_count"] - initial["raw_count"]) <= self._encoder_stability_counts(state)
        tolerance = self._keyboard_jog_counts(state)
        return (self._keyboard_jog_ready(initial) and self._keyboard_jog_ready(state)
                and abs(state["raw_count"] - initial["raw_count"]) <= tolerance
                and abs(state["raw_count"] - initial["input_target_count"]) <= tolerance
                and abs(state["input_target_count"] - initial["input_target_count"]) <= tolerance)

    def _stationary_states(self, *, disabled=False, enabled=False, **options):
        sample_start = time.monotonic()
        budget = max(0.25, self._timeout * 3 + 12 * self._command_gap_s)
        deadline = min(options.get("deadline") or float("inf"), sample_start + budget)
        read_options = {**options, "deadline": deadline}
        first = None
        while True:
            second = {axis: self._read_axis(axis, **read_options) for axis in self.axes}
            unstable_axis = None
            for axis, state in second.items():
                self._healthy(axis, state)
                if state["speed_rpm"] != 0:
                    raise MotionError("axis_moving", f"{axis} must have stationary encoder telemetry")
                if (not state["telemetry_valid"] or state["updated_monotonic"] is None
                        or not sample_start <= state["updated_monotonic"] <= time.monotonic()
                        or time.monotonic() - state["updated_monotonic"] > 2):
                    raise MotionError("invalid_telemetry", f"{axis} requires fresh stationary telemetry")
                if disabled and state["enabled"]:
                    raise MotionError("release_unconfirmed", f"{axis} manual release was not confirmed")
                if enabled and not state["enabled"]:
                    raise MotionError("enable_unconfirmed", f"{axis} actual locked feedback was not confirmed")
                if first is not None:
                    if state["updated_monotonic"] <= first[axis]["updated_monotonic"]:
                        raise MotionError("invalid_telemetry", f"{axis} requires two distinct stationary samples")
                    if (state["speed_rpm"] != 0 or first[axis]["speed_rpm"] != 0
                            or abs(state["raw_count"] - first[axis]["raw_count"]) > self._encoder_stability_counts(state)):
                        unstable_axis = axis
            if first is not None and unstable_axis is None:
                return second
            if deadline - time.monotonic() <= 0.02:
                raise MotionError("axis_moving", f"{unstable_axis or 'X/Z'} must have stationary encoder telemetry")
            first = second
            time.sleep(0.02)

    def _manual_session(self):
        self._require_connected()
        if not self._manual["active"]:
            raise MotionError("manual_calibration_session", "Begin explicit manual calibration first")
        if self._stop_event.is_set() or self._manual["generation"] != self._stop_generation:
            raise MotionError("cancelled", "Stop interrupted manual calibration")
        if (self._manual["connection_token"] != self._connection_token
                or self._manual["profiles"] != self._reference_profiles()):
            raise MotionError("manual_profile_changed", "Manual calibration connection or motor profile changed")

    def _manual_position(self, states):
        return {"motion": {"port": self.port, "baud": self.baud, "axes": copy.deepcopy(self.axes)},
                "connection_token": self._connection_token, "profiles": self._reference_profiles(),
                "raw_counts": {axis: state["raw_count"] for axis, state in states.items()}}

    def _keyboard_session(self, generation=None):
        self._manual_session()
        if generation is not None and generation != self._stop_generation:
            raise MotionError("cancelled", "STOP permanently cancelled this keyboard alignment operation")
        if self._manual.get("control_mode") != "keyboard":
            raise MotionError("keyboard_alignment_session", "Begin explicit keyboard alignment first")
        if self._manual["motion"] != {"port": self.port, "baud": self.baud, "axes": self.axes}:
            raise MotionError("manual_profile_changed", "Keyboard alignment mechanical settings changed")

    def _keyboard_position(self, states):
        return {**self._manual_position(states), "session_token": self._manual["session_token"],
                "stop_generation": self._stop_generation}

    def _keyboard_bounds(self, states):
        origins = self._manual.get("start_counts")
        if origins is None:
            return
        for axis, state in states.items():
            lower, upper = self._keyboard_raw_bounds(axis)
            if not lower <= state["raw_count"] <= upper:
                raise MotionError("travel_limit", f"{axis} exceeded an operator-set boundary or the controller position encoding range")

    def _keyboard_raw_bounds(self, axis):
        restriction = self._manual.get("direction_bounds", {}).get(axis, {})
        return (max(-POSITION_COUNT_MAX, restriction.get("lower", -POSITION_COUNT_MAX)),
                min(POSITION_COUNT_MAX, restriction.get("upper", POSITION_COUNT_MAX)))

    def _keyboard_direction_budget(self, axis, direction, state):
        updated = state.get("updated_monotonic")
        if (type(state.get("raw_count")) is not int or not state.get("telemetry_valid")
                or updated is None or not 0 <= time.monotonic() - updated <= 2):
            raise MotionError("invalid_telemetry", f"{axis} requires fresh encoder feedback for keyboard boundaries")
        lower, upper = self._keyboard_raw_bounds(axis)
        sign = -1 if direction else 1
        boundary = lower if direction else upper
        remaining = sign * (boundary - state["raw_count"]) * self.axes[axis]["lead_mm"] / 65536
        return boundary, remaining - 0.1

    def _keyboard_boundaries(self):
        if self._manual.get("control_mode") != "keyboard" or not self._manual.get("start_counts"):
            return []
        result = []
        for axis, direction, label in (("X", 0, "left"), ("X", 1, "right"), ("Z", 0, "down"), ("Z", 1, "up")):
            state = self._state[axis]
            config = state.get("config")
            if not isinstance(config, dict) or not config.get("pulses_per_rev"):
                continue
            try:
                boundary, remaining = self._keyboard_direction_budget(axis, direction, state)
            except MotionError:
                continue
            pulse_mm = self.axes[axis]["lead_mm"] / config["pulses_per_rev"]
            at_limit = remaining < 2 * pulse_mm
            explicit = ("lower" if direction else "upper") in self._manual.get("direction_bounds", {}).get(axis, {})
            if not explicit and not at_limit:
                continue
            result.append({"axis": axis, "direction": label, "boundary_count": boundary,
                           "remaining_mm": max(0, remaining), "at_limit": at_limit,
                           "kind": "operator" if explicit else "protocol"})
        return result

    def _keyboard_states(self, *, stationary=True, targets=True, require_enabled=True, generation=None, **options):
        self._keyboard_session(generation)
        for axis in self.axes:
            self._identity(axis, **options)
        self._keyboard_session(generation)
        states = (self._stationary_states(enabled=require_enabled, **options) if stationary else
                  {axis: self._read_axis(axis, **options) for axis in self.axes})
        self._keyboard_bounds(states)
        for axis, state in states.items():
            self._healthy(axis, state)
            if require_enabled and not state["enabled"]:
                raise MotionError("enable_unconfirmed", f"{axis} keyboard alignment requires actual locked feedback")
        if targets:
            initial = states
            target_samples = []
            for _sample in range(2):
                read_targets = {axis: {"target_count": self._read_manual_target(axis, 0x33),
                                       "instantaneous_target_count": self._read_manual_target(axis, 0x34)}
                                for axis in self.axes}
                states = self._stationary_states(enabled=require_enabled, **options)
                self._keyboard_session(generation)
                self._keyboard_bounds(states)
                for axis, state in states.items():
                    tolerance = max(1.0, state["config"]["position_window_deg"] * 65536 / 360)
                    if abs(state["raw_count"] - initial[axis]["raw_count"]) > self._encoder_stability_counts(state):
                        raise MotionError("axis_moving", f"{axis} moved during keyboard target verification")
                    if any(abs(target - state["raw_count"]) > tolerance for target in read_targets[axis].values()):
                        raise MotionError("manual_target_mismatch", f"{axis} target registers differ from the current encoder; fresh power-up and no hand turning are required before enabling")
                target_samples.append(read_targets)
            self._event("keyboard_target_guard", raw_counts={axis: state["raw_count"] for axis, state in states.items()},
                        target_samples=target_samples)
        self._keyboard_session(generation)
        if require_enabled:
            with self._state_lock:
                self._keyboard_session(generation)
                for axis in self.axes:
                    self._state[axis]["enable_confirmed"] = True
        return states

    def _keyboard_failure(self, error):
        self._fault(error)
        with self._state_lock:
            if self._manual.get("control_mode") == "keyboard":
                self._manual.update(active=False, status="blocked", generation=None)
        error.stop_result = self.stop_all()

    def _keyboard_start_states(self, axis, *, generation, cancel_event=None, interruptible=True):
        if not self._responsive_keyboard:
            return self._keyboard_states(generation=generation, cancel_event=cancel_event, interruptible=interruptible)
        self._keyboard_session(generation)
        with self._state_lock:
            previous = copy.deepcopy(self._state)
        options = {"cancel_event": cancel_event, "interruptible": interruptible, "keyboard_read": True}
        for name in self.axes:
            self._identity(name, **options)
        self._keyboard_session(generation)
        states = {name: self._read_axis(name, **options) for name in self.axes}
        for name, state in states.items():
            self._healthy(name, state, require_enabled=True)
            if state["speed_rpm"] != 0:
                raise MotionError("axis_moving", f"{name} is not stationary before keyboard movement")
            prior = previous[name]
            stable = prior["updated_monotonic"] is not None and self._keyboard_jog_pair(prior, state)
            if not stable:
                latest = self._read_axis(name, **options)
                self._healthy(name, latest, require_enabled=True)
                if not self._keyboard_jog_pair(state, latest):
                    raise MotionError("axis_moving", f"{name} requires stationary feedback before keyboard movement")
                states[name] = state = latest
            tolerance = max(1.0, state["config"]["position_window_deg"] * 65536 / 360)
            if abs(state["input_target_count"] - state["raw_count"]) > tolerance:
                raise MotionError("manual_target_mismatch", f"{name} target differs from the current encoder")
            instantaneous = self._read_manual_target(name, 0x34, keyboard_read=True)
            if abs(instantaneous - state["raw_count"]) > tolerance:
                raise MotionError("manual_target_mismatch", f"{name} instantaneous target differs from the current encoder")
        self._check_cancel(cancel_event)
        self._keyboard_session(generation)
        self._keyboard_bounds(states)
        return states

    def _keyboard_stopped_states(self, *, generation, cancel_event=None, interruptible=True):
        with self._state_lock:
            reusable = (self._responsive_keyboard and self._keyboard_segment_started is not None
                        and self._last_stop_completed is not None
                        and self._last_stop_completed >= self._keyboard_segment_started
                        and self._last_stop_generation == generation == self._stop_generation
                        and self._last_stop and self._last_stop["stop_confirmed"])
            previous = copy.deepcopy(self._last_stop_samples) if reusable else None
        if previous is None:
            return self._keyboard_states(targets=False, generation=generation,
                                         cancel_event=cancel_event, interruptible=interruptible)
        self._keyboard_session(generation)
        options = {"cancel_event": cancel_event, "interruptible": interruptible, "keyboard_read": True}
        states = {axis: self._read_axis(axis, **options) for axis in self.axes}
        for axis, state in states.items():
            self._healthy(axis, state, require_enabled=True)
            prior = previous[axis]
            if not self._keyboard_jog_pair(prior, state):
                raise MotionError("axis_moving", f"{axis} changed after the confirmed keyboard STOP")
        self._keyboard_session(generation)
        self._keyboard_bounds(states)
        return states

    def begin_keyboard_alignment(self, confirmed=False, boundary_at_current=None):
        if confirmed is not True:
            raise MotionError("keyboard_alignment_confirmation", "Explicit keyboard motion and clearance confirmation are required")
        directions = {("X", "left"): "upper", ("X", "right"): "lower", ("Z", "up"): "lower", ("Z", "down"): "upper"}
        if boundary_at_current is not None and (not isinstance(boundary_at_current, dict)
                or set(boundary_at_current) != {"axis", "direction"}
                or not isinstance(boundary_at_current.get("axis"), str)
                or not isinstance(boundary_at_current.get("direction"), str)
                or (boundary_at_current.get("axis"), boundary_at_current.get("direction")) not in directions):
            raise MotionError("invalid_direction", "Use one current-position X/Z directional boundary")
        with self._operation():
            self._require_connected()
            with self._state_lock:
                if (self._manual["active"] and self._manual.get("control_mode") != "keyboard"
                        and self._manual["status"] != "released"):
                    raise MotionError("manual_calibration_active", "Only a healthy released legacy session can be superseded")
                generation = self._stop_generation + 1
                self._experiment_calibration = None
                self._absolute_limits = None
            try:
                stopped = self.stop_all()
                if stopped["stop_confirmed"] is not True:
                    raise MotionError("stop_unconfirmed", "Both axes must confirm stopping before keyboard alignment", stop_result=stopped)
                for axis in self.axes:
                    self._identity(axis)
                with self._state_lock:
                    if self._stop_generation != generation:
                        raise MotionError("cancelled", "STOP interrupted keyboard alignment begin")
                    self._manual = {"active": True, "status": "starting", "mode": "keyboard", "control_mode": "keyboard",
                                    "generation": generation, "connection_token": self._connection_token,
                                    "session_token": str(uuid.uuid4()), "profiles": self._reference_profiles(),
                                    "motion": {"port": self.port, "baud": self.baud, "axes": copy.deepcopy(self.axes)}}
                    self._stop_event.clear()
                    for state in self._state.values():
                        for field in ("target_mm", "quantized_target_mm", "arrival_tolerance_mm", "command_speed_mm_s"):
                            state.pop(field, None)
                initial = self._keyboard_states(require_enabled=False, generation=generation, interruptible=True)
                enabled_axes = {axis for axis, state in initial.items() if state["enabled"]}
                enable_attempted = False
                for axis, config in self.axes.items():
                    self._keyboard_session(generation)
                    if initial[axis]["enabled"]:
                        continue
                    if enable_attempted:
                        states = self._keyboard_states(require_enabled=False, generation=generation, interruptible=True)
                        for checked_axis, state in states.items():
                            if state["enabled"] != (checked_axis in enabled_axes):
                                raise MotionError("enable_unconfirmed", f"{checked_axis} changed lock state during keyboard enable guards")
                    self._transact(bytes([config["address"], 0xF3, 0xAB, 1, 0]), 4, ack=True, interruptible=True)
                    enable_attempted = True
                    enabled_axes.add(axis)
                    states = self._stationary_states(interruptible=True)
                    self._keyboard_session(generation)
                    for checked_axis, state in states.items():
                        tolerance = max(1.0, state["config"]["position_window_deg"] * 65536 / 360)
                        if abs(state["raw_count"] - initial[checked_axis]["raw_count"]) > tolerance:
                            raise MotionError("manual_relock_displacement", f"{checked_axis} moved while keyboard lock was being confirmed")
                        if state["enabled"] != (checked_axis in enabled_axes):
                            raise MotionError("enable_unconfirmed", f"{checked_axis} keyboard alignment enable state was not confirmed")
                states = self._keyboard_states(targets=False, generation=generation, interruptible=True)
                with self._state_lock:
                    self._keyboard_session(generation)
                    self._manual.update(status="locked", start_counts={axis: state["raw_count"] for axis, state in states.items()})
                    if boundary_at_current is not None:
                        axis = boundary_at_current["axis"]
                        bound = directions[(axis, boundary_at_current["direction"])]
                        margin = math.ceil(0.1 * 65536 / self.axes[axis]["lead_mm"])
                        self._manual["direction_bounds"] = {axis: {bound: states[axis]["raw_count"] + (margin if bound == "upper" else -margin)}}
                    for axis in self.axes:
                        self._state[axis]["enable_confirmed"] = True
                self._event("keyboard_alignment_started", session_token=self._manual["session_token"],
                            raw_counts=self._manual["start_counts"])
                return self._keyboard_position(states)
            except MotionError as error:
                self._keyboard_failure(error)
                raise

    def alignment_step(self, axis, direction, speed_mm_s, duration_s, max_speed_mm_s, cancel_event=None):
        config = self._axis(axis)
        if isinstance(direction, bool) or not isinstance(direction, int) or direction not in (0, 1):
            raise MotionError("invalid_direction", "Keyboard alignment motor direction must be 0 or 1")
        speed = _number(speed_mm_s, "speed_mm_s", positive=True)
        duration = _number(duration_s, "duration_s", positive=True)
        maximum = _number(max_speed_mm_s, "max_speed_mm_s", positive=True)
        rpm = math.floor(speed * 60 / config["lead_mm"])
        if maximum > 2 or speed > min(maximum, config["max_speed_mm_s"], 2) or not 1 <= rpm <= 6000:
            raise MotionError("speed_range", "Keyboard speed must respect operator, axis and 2 mm/s limits without rounding up below one RPM")
        if duration > 0.12:
            raise MotionError("move_duration", "Keyboard segments cannot exceed 0.12 seconds")
        actual_speed = rpm * config["lead_mm"] / 60
        with self._operation():
            self._keyboard_session()
            with self._state_lock:
                generation = self._stop_generation
                segment_cancel = threading.Event()
                self._keyboard_segment_cancel = segment_cancel
                self._keyboard_segment_started = time.monotonic()
            cancellation = _MotionCancellation(segment_cancel, cancel_event)
            try:
                self._check_cancel(cancellation)
                initial = self._keyboard_start_states(axis, generation=generation, cancel_event=cancellation, interruptible=True)
                state = initial[axis]
                pulse_mm = config["lead_mm"] / state["config"]["pulses_per_rev"]
                boundary, remaining = self._keyboard_direction_budget(axis, direction, state)
                if remaining < 2 * pulse_mm:
                    return {"segment_mm": 0, "hold_exhausted": True, "stop_reason": "travel_limit"}
                pulses = math.floor(min(0.1, actual_speed * duration, remaining) / pulse_mm + 1e-12)
                if not 1 <= pulses <= 0xFFFFFFFF:
                    raise MotionError("pulse_range", "Keyboard segment is below one pulse or exceeds protocol range")
                segment_mm = pulses * pulse_mm
                nominal_duration = segment_mm / actual_speed
                sign = -1 if direction else 1
                target_count = state["raw_count"] + sign * pulses * 65536 / state["config"]["pulses_per_rev"]
                proposed = {name: copy.deepcopy(value) for name, value in initial.items()}
                proposed[axis]["raw_count"] = target_count
                self._keyboard_bounds(proposed)
                tolerance = max(1.0, state["config"]["position_window_deg"] * 65536 / 360)
                lower = min(state["raw_count"], target_count) - tolerance
                upper = max(state["raw_count"], target_count) + tolerance
                timeout = self._move_timeout or 5.0
                deadline = time.monotonic() + timeout
                self._check_cancel(cancellation)
                self._keyboard_session(generation)
                body = (bytes([config["address"], 0xFD, direction]) + rpm.to_bytes(2, "big") + bytes([10])
                        + pulses.to_bytes(4, "big") + bytes([2, 0]))
                self._transact(body, 4, deadline=deadline, ack=True, cancel_event=cancellation, interruptible=True)
                earliest_arrival = time.monotonic() + nominal_duration
                quiet_since = None
                quiet_count = None
                stable_samples = 0
                while True:
                    self._check_cancel(cancellation)
                    self._keyboard_session(generation)
                    if time.monotonic() >= deadline:
                        raise MotionError("move_timeout", f"{axis} keyboard segment did not finish before its deadline")
                    current = self._read_axis(axis, deadline=deadline, cancel_event=cancellation, interruptible=True)
                    self._keyboard_session(generation)
                    self._healthy(axis, current, require_enabled=True)
                    self._keyboard_bounds({axis: current})
                    if not lower <= current["raw_count"] <= upper:
                        raise MotionError("calibration_motion", f"{axis} left the committed keyboard segment envelope")
                    reached = (time.monotonic() >= earliest_arrival and current["speed_rpm"] == 0
                               and bool(current["status"] & 2) and abs(current["raw_count"] - target_count) <= tolerance)
                    if not reached:
                        quiet_since = quiet_count = None
                    elif quiet_count is None or abs(current["raw_count"] - quiet_count) > self._encoder_stability_counts(current):
                        quiet_since, quiet_count = time.monotonic(), current["raw_count"]
                    quiet = reached and quiet_since is not None and time.monotonic() - quiet_since >= 0.2
                    stable_samples = stable_samples + 1 if quiet else 0
                    if stable_samples >= 2:
                        self._keyboard_session(generation)
                        self._transact(bytes([config["address"], 0xFE, 0x98, 0]), 4, deadline=deadline,
                                       ack=True, cancel_event=cancellation, interruptible=True)
                        self._keyboard_session(generation)
                        states = self._keyboard_states(targets=False, generation=generation,
                                                       deadline=deadline, cancel_event=cancellation, interruptible=True)
                        for held_axis, held in states.items():
                            if held_axis != axis and (not held["status"] & 2
                                                      or not self._keyboard_held_matches(initial[held_axis], held)):
                                raise MotionError("calibration_motion", f"{held_axis} moved away from its held position")
                        if (not states[axis]["status"] & 2
                                or abs(states[axis]["raw_count"] - target_count) > tolerance):
                            raise MotionError("calibration_motion", f"{axis} left its reached keyboard target during final verification")
                        self._check_cancel(cancellation)
                        self._keyboard_session(generation)
                        result = {**self._keyboard_position(states), "requested_speed_mm_s": speed,
                                  "command_speed_mm_s": actual_speed, "segment_mm": segment_mm,
                                  "duration_s": nominal_duration}
                        self._event("keyboard_alignment_step", axis=axis, direction=direction, **result)
                        return result
                    self._stop_event.wait(min(0.02, max(0, deadline - time.monotonic())))
            except MotionError as error:
                if error.code == "cancelled":
                    with self._state_lock:
                        owns_generation = generation == self._stop_generation
                    if owns_generation:
                        self.stop_keyboard_alignment()
                    error.stop_result = self._current_stop_result()
                else:
                    self._keyboard_failure(error)
                raise
            finally:
                with self._state_lock:
                    if self._keyboard_segment_cancel is segment_cancel:
                        self._keyboard_segment_cancel = None
                        self._keyboard_segment_started = None

    def alignment_fast_hold(self, axis, direction, speed_mm_s, max_speed_mm_s, max_distance_mm, duration_s,
                            cancel_event=None):
        from .keyboard_fast_jog import run_fast_alignment
        return run_fast_alignment(self, axis, direction, speed_mm_s, max_speed_mm_s,
                                  max_distance_mm, duration_s, cancel_event=cancel_event)

    def stop_keyboard_alignment(self):
        with self._keyboard_stop_lock:
            with self._state_lock:
                owner = None
                if not self._faulted:
                    self._keyboard_session()
                    owner = {"session_token": self._manual["session_token"], "generation": self._stop_generation}
            return self._stop_all(keyboard_owner=owner)

    def capture_keyboard_alignment(self):
        with self._operation():
            self._keyboard_session()
            generation = self._stop_generation
            try:
                states = self._keyboard_states(generation=generation, interruptible=True)
                with self._state_lock:
                    self._keyboard_session(generation)
                    return self._keyboard_position(states)
            except MotionError as error:
                self._keyboard_failure(error)
                raise

    def finish_keyboard_alignment(self, reference_counts, coordinate_extents=None):
        with self._operation():
            self._keyboard_session()
            generation = self._stop_generation
            try:
                if (not isinstance(reference_counts, dict) or set(reference_counts) != set(self.axes)
                        or any(isinstance(count, bool) or not isinstance(count, int) for count in reference_counts.values())):
                    raise MotionError("invalid_reference", "Both integer software reference counts are required")
                updated_axes = copy.deepcopy(self.axes)
                if coordinate_extents is not None:
                    if not isinstance(coordinate_extents, dict) or set(coordinate_extents) != set(self.axes):
                        raise MotionError("invalid_reference", "Both software coordinate extents are required")
                    for axis, extent in coordinate_extents.items():
                        extent = _number(extent, f"coordinate_extents.{axis}", positive=True)
                        if not self.axes[axis]["travel_mm"] <= extent <= 10000:
                            raise MotionError("invalid_reference", "Derived coordinate extents must contain the current reference range")
                        updated_axes[axis]["travel_mm"] = extent
                states = self._keyboard_states(generation=generation, interruptible=True)
                positions = {}
                for axis, state in states.items():
                    sign = -1 if self.axes[axis]["positive_direction"] else 1
                    positions[axis] = sign * (state["raw_count"] - reference_counts[axis]) * self.axes[axis]["lead_mm"] / 65536
                    if not 0 <= positions[axis] <= updated_axes[axis]["travel_mm"]:
                        raise MotionError("travel_limit", f"{axis} keyboard reference is outside the verified coordinate extent")
                with self._state_lock:
                    self._keyboard_session(generation)
                    self.axes = updated_axes
                    for axis in self.axes:
                        self._state[axis].update(reference_count=reference_counts[axis], referenced=True,
                                                 position_mm=positions[axis], enable_confirmed=True,
                                                 mechanical=copy.deepcopy(self.axes[axis]))
                    self._manual.update(active=False, status="complete", generation=None)
                return self._reference_identity(states)
            except MotionError as error:
                self._keyboard_failure(error)
                raise

    def cancel_keyboard_alignment(self):
        with self._operation():
            self._keyboard_session()
            generation = self._stop_generation
            try:
                self._keyboard_states(generation=generation, interruptible=True)
                with self._state_lock:
                    self._keyboard_session(generation)
                    self._manual.update(active=False, status="cancelled", generation=None)
                    for state in self._state.values():
                        state.update(referenced=False, reference_count=None, position_mm=None, enable_confirmed=False)
                return self.snapshot()
            except MotionError as error:
                self._keyboard_failure(error)
                raise

    def begin_manual_calibration(self, *, release_confirmed=False, z_supported=False):
        if self.manual_calibration_supported is not True and (release_confirmed is not True or z_supported is not True):
            if release_confirmed is False and z_supported is False:
                raise MotionError("manual_relock_unqualified", "手拧记录需要明确确认带电松轴及Z轴已支撑，原位锁轴仍受目标位置检查")
            raise MotionError("manual_release_confirmation", "Explicit release and supported-Z confirmations are required")
        with self._operation():
            self._require_connected()
            self._require_manual_clear()
            try:
                with self._state_lock:
                    generation = self._stop_generation + 1
                stop_result = self.stop_all()
                if stop_result["stop_confirmed"] is not True:
                    raise MotionError("stop_unconfirmed", "Both axes must confirm stopping before manual release",
                                      stop_result=stop_result)
                for axis in self.axes:
                    self._identity(axis)
                with self._state_lock:
                    if generation != self._stop_generation:
                        raise MotionError("cancelled", "STOP interrupted manual release")
                    self._manual.update(active=True, status="releasing", generation=generation,
                                        connection_token=self._connection_token, profiles=self._reference_profiles(),
                                        relock_attempted=False)
                    self._experiment_calibration = None
                    self._stop_event.clear()
                    for state in self._state.values():
                        state.update(referenced=False, reference_count=None, position_mm=None, enable_confirmed=False)
                        for field in ("target_mm", "quantized_target_mm", "arrival_tolerance_mm", "command_speed_mm_s"):
                            state.pop(field, None)
                for axis, config in self.axes.items():
                    self._manual_session()
                    self._transact(bytes([config["address"], 0xF3, 0xAB, 0, 0]), 4, ack=True, interruptible=True)
                states = self._stationary_states(disabled=True)
                self._manual_session()
                with self._state_lock:
                    self._manual["status"] = "released"
                self._event("manual_released", raw_counts={axis: state["raw_count"] for axis, state in states.items()})
                return self._manual_position(states)
            except MotionError as error:
                with self._state_lock:
                    if self._manual["active"]:
                        self._manual["status"] = "blocked"
                self._fault(error)
                if self._manual["active"]:
                    error.stop_result = self.stop_all()
                raise

    def capture_manual_position(self):
        with self._operation():
            self._manual_session()
            try:
                for axis in self.axes:
                    self._identity(axis)
                states = self._stationary_states(disabled=True)
                self._manual_session()
                self._event("manual_captured", raw_counts={axis: state["raw_count"] for axis, state in states.items()})
                return self._manual_position(states)
            except MotionError as error:
                self._manual["status"] = "blocked"
                self._fault(error)
                raise

    def _relock_manual_at_current(self):
        for axis in self.axes:
            self._identity(axis)
        self._manual_session()
        initial = self._stationary_states(disabled=True)
        tolerances = {}
        for axis, state in initial.items():
            profile = state["config"]
            window = profile["position_window_deg"]
            if not 0 <= window <= profile["step_angle"]:
                raise MotionError("unsupported_position_window", f"{axis} relock window exceeds one full motor step")
            tolerances[axis] = max(1.0, window * 65536 / 360)
        target_samples = []
        for _sample in range(2):
            targets = {axis: {"target_count": self._read_manual_target(axis, 0x33),
                              "instantaneous_target_count": self._read_manual_target(axis, 0x34)}
                       for axis in self.axes}
            states = self._stationary_states(disabled=True)
            self._manual_session()
            for axis, state in states.items():
                if abs(state["raw_count"] - initial[axis]["raw_count"]) > 1:
                    raise MotionError("axis_moving", f"{axis} moved during manual relock target checks")
                if any(abs(target - state["raw_count"]) > tolerances[axis] for target in targets[axis].values()):
                    self._event("manual_relock_blocked", axis=axis, raw_count=state["raw_count"],
                                targets=targets[axis], tolerance_counts=tolerances[axis])
                    raise MotionError("manual_target_mismatch", f"{axis} target positions do not match the current encoder; axes remain released")
            target_samples.append(targets)
        self._event("manual_relock_guard", raw_counts={axis: state["raw_count"] for axis, state in states.items()},
                    target_samples=target_samples, tolerance_counts=tolerances)
        enabled_axes = set()
        try:
            for axis, config in self.axes.items():
                self._manual_session()
                with self._state_lock:
                    self._manual["relock_attempted"] = True
                self._transact(bytes([config["address"], 0xF3, 0xAB, 1, 0]), 4, ack=True, interruptible=True)
                enabled_axes.add(axis)
                states = self._stationary_states()
                self._manual_session()
                for checked_axis, state in states.items():
                    if state["enabled"] != (checked_axis in enabled_axes):
                        raise MotionError("enable_unconfirmed", f"{checked_axis} manual relock enable state was not confirmed")
                    if abs(state["raw_count"] - initial[checked_axis]["raw_count"]) > tolerances[checked_axis]:
                        raise MotionError("manual_relock_displacement", f"{checked_axis} encoder moved beyond the relock arrival window")
            self._event("manual_relocked", raw_counts={axis: state["raw_count"] for axis, state in states.items()},
                        tolerance_counts=tolerances)
            return states
        except MotionError as error:
            with self._state_lock:
                self._manual["status"] = "blocked"
            self._fault(error)
            error.stop_result = self.stop_all()
            raise

    def _read_manual_target(self, axis, opcode, *, manual_session=True, keyboard_read=False,
                            deadline=None, cancel_event=None):
        if opcode not in (0x33, 0x34):
            raise MotionError("manual_target_unverified", "Only documented target-position reads are allowed")
        address = self._axis(axis)["address"]
        if manual_session:
            self._manual_session()
        else:
            self._require_connected()
            self._check_cancel()
        try:
            response = self._transact(bytes([address, opcode]), 8, interruptible=True, keyboard_read=keyboard_read,
                                      deadline=deadline, cancel_event=cancel_event)
            if response[2] not in (0, 1):
                raise MotionError("invalid_telemetry", f"{axis} target position has an invalid sign byte")
            target = int.from_bytes(response[3:7], "big") * (-1 if response[2] else 1)
        except MotionError as error:
            if error.code == "cancelled":
                raise
            failure = MotionError("manual_target_unverified", f"{axis} target register 0x{opcode:02x} could not be verified: {error}")
            if isinstance(getattr(error, "wire_diagnostic", None), dict):
                failure.wire_diagnostic = copy.deepcopy(error.wire_diagnostic)
            raise failure from error
        self._event("manual_target_read", axis=axis, opcode=opcode, raw_count=target)
        return target

    def finish_manual_calibration(self, reference_counts):
        with self._operation():
            self._manual_session()
            try:
                if (not isinstance(reference_counts, dict) or set(reference_counts) != set(self.axes)
                        or any(isinstance(count, bool) or not isinstance(count, int) for count in reference_counts.values())):
                    raise MotionError("invalid_reference", "Both integer software reference counts are required")
                initial = self._stationary_states(disabled=True)
                for axis, state in initial.items():
                    sign = -1 if self.axes[axis]["positive_direction"] else 1
                    position = sign * (state["raw_count"] - reference_counts[axis]) * self.axes[axis]["lead_mm"] / 65536
                    if not 0 <= position <= self.axes[axis]["travel_mm"]:
                        raise MotionError("travel_limit", f"{axis} manual reference is outside travel")
                states = self._relock_manual_at_current()
                self._manual_session()
                for axis, state in states.items():
                    if not state["enabled"]:
                        raise MotionError("enable_unconfirmed", f"{axis} manual relock was not confirmed")
                with self._state_lock:
                    self._manual_session()
                    for axis, state in states.items():
                        sign = -1 if self.axes[axis]["positive_direction"] else 1
                        position = sign * (state["raw_count"] - reference_counts[axis]) * self.axes[axis]["lead_mm"] / 65536
                        if not 0 <= position <= self.axes[axis]["travel_mm"]:
                            raise MotionError("travel_limit", f"{axis} manual reference is outside travel")
                        self._state[axis].update(reference_count=reference_counts[axis], referenced=True,
                                                 position_mm=position, enable_confirmed=True)
                    self._manual.update(active=False, status="complete", generation=None)
                return self._reference_identity(states)
            except MotionError as error:
                self._manual["status"] = "blocked"
                self._fault(error)
                if self._manual.get("relock_attempted") and error.stop_result is None:
                    error.stop_result = self.stop_all()
                raise

    def cancel_manual_calibration(self):
        with self._operation():
            self._manual_session()
            try:
                states = self._relock_manual_at_current()
                self._manual_session()
                if any(not state["enabled"] for state in states.values()):
                    raise MotionError("enable_unconfirmed", "Manual cancellation did not confirm current-position relock")
                with self._state_lock:
                    self._manual_session()
                    self._manual.update(active=False, status="cancelled", generation=None)
                    for state in self._state.values():
                        state.update(referenced=False, reference_count=None, position_mm=None, enable_confirmed=False)
                return self.snapshot()
            except MotionError as error:
                self._manual["status"] = "blocked"
                self._fault(error)
                if self._manual.get("relock_attempted") and error.stop_result is None:
                    error.stop_result = self.stop_all()
                raise

    def _reference_profiles(self):
        return {axis: {field: copy.deepcopy(state[field]) for field in
                       ("firmware_version", "hardware_version", "config", "power_flag_baseline")}
                for axis, state in self._state.items()}

    def _reference_identity(self, states):
        return {"version": "emm-reference-1", "motion": {"port": self.port, "baud": self.baud, "axes": copy.deepcopy(self.axes)},
                "reference_counts": {axis: state["reference_count"] for axis, state in self._state.items()},
                "raw_counts": {axis: state["raw_count"] for axis, state in states.items()},
                "profiles": self._reference_profiles()}

    def reference_identity(self):
        with self._operation():
            self._require_connected()
            self._require_manual_clear()
            if any(not state["referenced"] for state in self._state.values()):
                raise MotionError("not_referenced", "Both axes require a known software reference")
            try:
                states = self._stationary_states()
                return self._reference_identity(states)
            except MotionError as error:
                self._fault(error)
                raise

    def restore_reference(self, identity):
        with self._operation():
            self._require_connected()
            self._require_manual_clear()
            with self._state_lock:
                self._startup_approach = None
                self._completed_motion_targets = {}
                generation = self._stop_generation
            try:
                if not isinstance(identity, dict) or set(identity) != {"version", "motion", "reference_counts", "raw_counts", "profiles"}:
                    raise MotionError("invalid_reference", "A saved exact reference identity is required")
                if identity["version"] != "emm-reference-1" or identity["motion"] != {"port": self.port, "baud": self.baud, "axes": self.axes}:
                    raise MotionError("reference_identity", "Saved motion identity differs from the connected bus")
                for field in ("reference_counts", "raw_counts"):
                    values = identity[field]
                    if (not isinstance(values, dict) or set(values) != set(self.axes)
                            or any(isinstance(count, bool) or not isinstance(count, int) for count in values.values())):
                        raise MotionError("invalid_reference", f"{field} requires exact integer counts for X and Z")
                for axis in self.axes:
                    self._identity(axis)
                states = self._stationary_states()
                if identity["profiles"] != self._reference_profiles():
                    raise MotionError("reference_identity", "Motor configuration, firmware or power flags changed")
                for axis, state in states.items():
                    if abs(state["raw_count"] - identity["raw_counts"][axis]) > 1:
                        raise MotionError("reference_checkpoint", f"{axis} encoder differs from its saved parked reference checkpoint")
                    sign = -1 if self.axes[axis]["positive_direction"] else 1
                    position = sign * (state["raw_count"] - identity["reference_counts"][axis]) * self.axes[axis]["lead_mm"] / 65536
                    tolerance = self.axes[axis]["lead_mm"] / 65536
                    if not -tolerance <= position <= self.axes[axis]["travel_mm"] + tolerance:
                        raise MotionError("travel_limit", f"{axis} restored position is outside configured travel")
                with self._state_lock:
                    if self._stop_generation != generation:
                        raise MotionError("cancelled", "Stop interrupted reference restoration")
                    for axis, state in states.items():
                        sign = -1 if self.axes[axis]["positive_direction"] else 1
                        self._state[axis].update(reference_count=identity["reference_counts"][axis], referenced=True,
                                                position_mm=sign * (state["raw_count"] - identity["reference_counts"][axis]) * self.axes[axis]["lead_mm"] / 65536,
                                                enable_confirmed=False)
                    self._stop_event.clear()
                self._event("reference_restored", raw_counts=identity["raw_counts"])
                return self.snapshot()
            except MotionError as error:
                self._fault(error)
                raise

    def parked_reference_checkpoint(self, identity):
        with self._operation():
            self._require_connected()
            self._require_manual_clear()
            with self._state_lock:
                generation = self._stop_generation
                if not self._stop_event.is_set() or not self._last_stop or self._last_stop["stop_confirmed"] is not True:
                    raise MotionError("stop_unconfirmed", "A parked checkpoint requires confirmed motion STOP")
            if not isinstance(identity, dict) or set(identity) != {"version", "motion", "reference_counts", "raw_counts", "profiles"}:
                raise MotionError("invalid_reference", "The pre-STOP reference identity is required")
            if identity["version"] != "emm-reference-1" or identity["motion"] != {"port": self.port, "baud": self.baud, "axes": self.axes}:
                raise MotionError("reference_identity", "Pre-STOP motion identity differs from the connected bus")
            for field in ("reference_counts", "raw_counts"):
                values = identity[field]
                if (not isinstance(values, dict) or set(values) != set(self.axes)
                        or any(isinstance(count, bool) or not isinstance(count, int) for count in values.values())):
                    raise MotionError("invalid_reference", f"{field} requires exact integer counts for X and Z")
            for axis in self.axes:
                self._identity(axis)
            states = self._stationary_states()
            if identity["profiles"] != self._reference_profiles():
                raise MotionError("reference_identity", "Motor configuration, firmware or power flags changed after STOP")
            for axis, state in states.items():
                if abs(state["raw_count"] - identity["raw_counts"][axis]) > 1:
                    raise MotionError("reference_checkpoint", f"{axis} encoder changed since the pre-STOP reference")
                sign = -1 if self.axes[axis]["positive_direction"] else 1
                position = sign * (state["raw_count"] - identity["reference_counts"][axis]) * self.axes[axis]["lead_mm"] / 65536
                tolerance = self.axes[axis]["lead_mm"] / 65536
                if not -tolerance <= position <= self.axes[axis]["travel_mm"] + tolerance:
                    raise MotionError("travel_limit", f"{axis} parked reference is outside configured travel")
            with self._state_lock:
                if self._stop_generation != generation:
                    raise MotionError("cancelled", "Stop interrupted parked reference verification")
                checkpoint = self._reference_identity(states)
                checkpoint["reference_counts"] = copy.deepcopy(identity["reference_counts"])
            return checkpoint

    def stop_all(self):
        return self._stop_all()

    def _stop_all(self, *, keyboard_owner=None):
        with self._state_lock:
            self._startup_approach = None
            self._completed_motion_targets = {}
            jog_handoff = (keyboard_owner is not None and self._responsive_keyboard
                           and self._keyboard_segment_started is not None)
            self._stop_generation += 1
            generation = self._stop_generation
            self._stop_event.set()
            if self._keyboard_segment_cancel is not None:
                self._keyboard_segment_cancel.set()
            if keyboard_owner is None and self._manual.get("control_mode") == "keyboard" and self._manual["active"]:
                self._manual.update(active=False, status="interrupted", generation=None)
                self._experiment_calibration = None
        with self._stop_lock:
            result = {"stop_confirmed": False, "axes": {}, "errors": []}
            replies = {}
            with self._state_lock:
                active_axis = self._automatic_motion_axis
            stop_order = sorted(self.axes, key=lambda axis: axis != active_axis)
            for axis in stop_order:
                config = self.axes[axis]
                result["axes"][axis] = {"stop_confirmed": False, "error": None,
                                        "stop_acknowledged": False, "stop_reply": None,
                                        "stop_basis": None}
                try:
                    self._require_connected(allow_fault=True)
                    response = self._transact(bytes([config["address"], 0xFE, 0x98, 0]), 4,
                                              stop_command=True)
                    reply = response[2]
                    result["axes"][axis].update(stop_acknowledged=reply == 0x02,
                                                stop_reply=f"{reply:02x}")
                    if reply not in (0x02, 0xE2):
                        raise MotionError("command_rejected", f"Axis {config['address']} rejected opcode 0xfe: 0x{reply:02x}")
                    replies[axis] = reply
                except MotionError as error:
                    result["axes"][axis]["error"] = str(error)
                    result["errors"].append({"axis": axis, "code": error.code, "message": str(error)})
            for axis in self.axes:
                if axis not in replies:
                    continue
                try:
                    sample_start = time.monotonic()
                    confirmation_deadline = sample_start + max(0.25, self._timeout * 3)
                    previous = None
                    while True:
                        state = self._read_axis(axis, deadline=confirmation_deadline)
                        self._healthy(axis, state)
                        if (not state["telemetry_valid"] or state["updated_monotonic"] is None
                                or not sample_start <= state["updated_monotonic"] <= time.monotonic()
                                or time.monotonic() - state["updated_monotonic"] > 2):
                            raise MotionError("stop_unconfirmed", f"{axis} stop telemetry is not fresh")
                        if replies[axis] == 0xE2 and state["enabled"]:
                            raise MotionError("stop_unconfirmed", f"{axis} rejected STOP while still enabled")
                        pair_confirmed = (previous is not None and
                            (self._keyboard_jog_pair(previous, state) if jog_handoff else
                             previous["speed_rpm"] == 0 and state["speed_rpm"] == 0
                             and abs(previous["raw_count"] - state["raw_count"]) <= 1
                             and state["updated_monotonic"] > previous["updated_monotonic"]))
                        if pair_confirmed:
                            break
                        remaining = confirmation_deadline - time.monotonic()
                        if remaining <= 0.02:
                            raise MotionError("stop_unconfirmed", f"{axis} stop telemetry is not stationary")
                        previous = state
                        time.sleep(0.02)
                    result["axes"][axis].update(stop_confirmed=True,
                                                stop_basis="disabled_feedback" if replies[axis] == 0xE2 else
                                                "keyboard_position_window" if jog_handoff else "acknowledged_stop")
                except MotionError as error:
                    result["axes"][axis]["error"] = str(error)
                    result["errors"].append({"axis": axis, "code": error.code, "message": str(error)})
            result["stop_confirmed"] = all(state["stop_confirmed"] for state in result["axes"].values())
            with self._state_lock:
                self._last_stop = copy.deepcopy(result)
                self._last_stop_generation = generation
                self._last_stop_samples = copy.deepcopy(self._state)
                self._last_stop_completed = time.monotonic()
                for state in self._state.values():
                    state.update(referenced=False, reference_count=None, position_mm=None, enable_confirmed=False)
                if keyboard_owner is not None:
                    owned = (generation == self._stop_generation == keyboard_owner["generation"] + 1
                             and self._manual.get("session_token") == keyboard_owner["session_token"]
                             and self._manual["active"] and self._manual["generation"] == keyboard_owner["generation"])
                    locked = all(state["enabled"] for state in self._state.values())
                    if (owned and locked and not self._faulted and result["stop_confirmed"]
                            and self._manual["status"] == "locked"):
                        self._manual.update(generation=generation, status="locked")
                        for state in self._state.values():
                            state["enable_confirmed"] = True
                        self._stop_event.clear()
                    elif owned:
                        self._manual.update(active=False, status="blocked", generation=None)
            if result["errors"]:
                self._fault(MotionError("stop_unconfirmed", "One or more axes did not confirm stopping"))
            self._event("stopped", **result)
            return result

    def _close_port(self):
        with self._serial_lock:
            device, self._serial = self._serial, None
            self._last_exchange_monotonic = None
            self._connected = False
            self._absolute_limits = None
            self._experiment_calibration = None
            self._startup_approach = None
            self._completed_motion_targets = {}
            if device is not None:
                device.close()
            with self._state_lock:
                for state in self._state.values():
                    state.update(referenced=False, reference_count=None, position_mm=None,
                                 enable_confirmed=False, telemetry_valid=False)

    def close(self):
        self._stop_event.set()
        if self._connected:
            result = self.stop_all()
            if result["stop_confirmed"] is not True:
                raise MotionError("stop_unconfirmed", "Cannot close motion port until both axes confirm stopping",
                                  stop_result=result)
        else:
            with self._state_lock:
                result = copy.deepcopy(self._last_stop) if self._last_stop is not None else {
                    "stop_confirmed": False, "axes": {}, "errors": []}
        self._close_port()
        return result
