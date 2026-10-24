"""Owned keyboard holds with finite segments and expiring server leases."""

from __future__ import annotations

import copy
import math
import threading
import time

from .automation import AutomationError, _keys, _number, _object, _text


class AlignmentJog:
    LEASE_SECONDS = 0.35
    MAX_SEGMENT_SECONDS = 0.12
    MAX_SEGMENT_MM = 0.1
    HARD_MAX_SPEED = 20.0
    MAX_PRESS_IDS = 10000
    IDLE_REFRESH_SECONDS = 0.5
    DIRECTIONS = {("X", "left"): 0, ("X", "right"): 1, ("Z", "up"): 1, ("Z", "down"): 0}

    def __init__(self, motion, session_token, *, clock=time.monotonic, on_blocked=None, start_threads=True):
        self._motion = motion
        self._token = _text(session_token, "session_token", 100)
        self._clock = clock
        self._on_blocked = on_blocked
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active = None
        self._presses = set()
        self._released = set()
        self._frozen = False
        self._blocked = False
        self._closed = False
        self._stopping = False
        self._inflight = False
        self._idle_inflight = False
        self._idle_cancel_event = None
        self._last_idle_refresh = self._clock()
        self._idle_done = threading.Event()
        self._idle_done.set()
        self._step_done = threading.Event()
        self._step_done.set()
        self._step_settled = threading.Event()
        self._step_settled.set()
        self._idle_settled = threading.Event()
        self._idle_settled.set()
        self._stop_done = threading.Event()
        self._stop_done.set()
        self._halt_count = 0
        self._halt_settled = threading.Event()
        self._halt_settled.set()
        self._stop_result = None
        self._error = None
        self._last_stop_reason = None
        axis_ceiling = min(self.HARD_MAX_SPEED, *(axis["mechanical"]["max_speed_mm_s"]
                                                 for axis in motion.snapshot()["axes"].values()))
        self._settings = {"speed_mm_s": min(0.5, axis_ceiling), "max_speed_mm_s": min(1.0, axis_ceiling),
                          "fast_motion_confirmed": False}
        self._validate_settings(self._settings)
        if start_threads:
            threading.Thread(target=self._work, name="alignment-jog", daemon=True).start()
            threading.Thread(target=self._watch, name="alignment-lease", daemon=True).start()

    def snapshot(self):
        with self._lock:
            active = self._active or {}
            status = ("closed" if self._closed else "blocked" if self._blocked else "stopping" if self._stopping
                      else "frozen" if self._frozen else "holding" if self._active else "idle")
            return {"session_token": self._token, "status": status, "active": bool(self._active),
                    "press_id": active.get("press_id"), "axis": active.get("axis"), "direction": active.get("direction"),
                    "distance_mm": active.get("distance_mm", 0), "last_stop_reason": self._last_stop_reason,
                    "error": copy.deepcopy(self._error), "stop_result": copy.deepcopy(self._stop_result),
                    "boundaries": self._motion.snapshot().get("manual", {}).get("boundaries", []),
                    "settings": {**self._settings, "hard_max_speed_mm_s": self.HARD_MAX_SPEED,
                                 "lease_ms": 350, "heartbeat_ms": 100, "max_hold_s": None,
                                 "max_hold_mm": None, "hold_mode": "until_release"}}

    def _payload(self, payload, fields, *, press=True):
        _object(payload, "jog")
        _keys(payload, {"session_token", *(fields or ()), *(("press_id",) if press else ())}, "jog")
        token = _text(payload.get("session_token"), "session_token", 100)
        if token != self._token:
            raise AutomationError("alignment_session", "The keyboard session has changed", "session_token", 409)
        if press:
            return _text(payload.get("press_id"), "press_id", 100)

    def _remember(self, press_id):
        if press_id not in self._presses:
            if len(self._presses) >= self.MAX_PRESS_IDS:
                raise AutomationError("alignment_press_limit", "Start a new keyboard session after the press identifier limit", status=409)
            self._presses.add(press_id)

    def _available(self):
        if self._blocked or self._closed or self._frozen or self._stopping:
            raise AutomationError("alignment_unavailable", "Keyboard alignment is frozen, closed or blocked", status=409)

    def _validate_settings(self, settings):
        maximum = _number(settings["max_speed_mm_s"], "max_speed_mm_s", 0.000001, self.HARD_MAX_SPEED)
        speed = _number(settings["speed_mm_s"], "speed_mm_s", 0.000001, maximum)
        if not isinstance(settings["fast_motion_confirmed"], bool):
            raise AutomationError("alignment_confirmation", "Fast-motion confirmation must be boolean", "fast_motion_confirmed")
        if maximum > 2 and settings["fast_motion_confirmed"] is not True:
            raise AutomationError("alignment_fast_confirmation", "Fast motion requires electrodes clear of the wells and at least 10 mm clear travel", "fast_motion_confirmed", 409)
        if speed > 2 and not callable(getattr(self._motion, "alignment_fast_hold", None)):
            raise AutomationError("alignment_speed", "This motion driver does not support bounded fast alignment")
        axes = self._motion.snapshot()["axes"]
        for axis in ("X", "Z"):
            mechanical = axes[axis]["mechanical"]
            if maximum > mechanical["max_speed_mm_s"]:
                raise AutomationError("alignment_speed", "Jog maximum exceeds an axis speed limit", "max_speed_mm_s")
            if math.floor(speed * 60 / mechanical["lead_mm"]) < 1:
                raise AutomationError("alignment_speed", "Jog speed must produce at least one RPM without rounding upward", "speed_mm_s")

    def configure(self, payload):
        with self._lock:
            self._payload(payload, {"speed_mm_s", "max_speed_mm_s", "fast_motion_confirmed"}, press=False)
            self._available()
            if self._active or self._inflight:
                raise AutomationError("alignment_release_required", "Release the jog control before changing speed", status=409)
            settings = {**self._settings, **{field: payload[field] for field in self._settings if field in payload}}
            self._validate_settings(settings)
            self._settings = settings
            return self.snapshot()

    def _expired(self, now):
        if not self._active:
            return None
        if now >= self._active["deadline"]:
            return "lease_expired"
        return None

    def start(self, payload):
        self._watch_once()
        with self._condition:
            press_id = self._payload(payload, {"axis", "direction"})
            self._available()
            axis, direction = payload.get("axis"), payload.get("direction")
            if not isinstance(axis, str) or not isinstance(direction, str) or (axis, direction) not in self.DIRECTIONS:
                raise AutomationError("invalid_value", "Use X left/right or Z up/down", "direction")
            self._remember(press_id)
            if press_id in self._released:
                raise AutomationError("alignment_press_released", "This press was released or expired; release and press again", status=409)
            if self._active:
                if all(self._active[field] == payload[field] for field in ("press_id", "axis", "direction")):
                    return self.snapshot()
                self._released.add(press_id)
                raise AutomationError("alignment_hold_active", "Only one axis and direction may be held", status=409)
            self._validate_settings(self._settings)
            now = self._clock()
            self._motion._keyboard_trace_context = {"session_token": self._token, "press_id": press_id,
                "axis": axis, "direction": direction,
                "baseline_count": self._motion.snapshot()["axes"][axis].get("raw_count"),
                "command_sent": False, "motion_observed": False}
            emit = getattr(self._motion, "_event", None)
            if callable(emit):
                emit("keyboard_start_accepted", idle_inflight=self._idle_inflight)
            if self._idle_cancel_event is not None:
                self._idle_cancel_event.set()
            self._active = {"press_id": press_id, "axis": axis, "direction": direction, "sequence": 0,
                            "started": now, "deadline": now + self.LEASE_SECONDS, "distance_mm": 0.0,
                            "cancel_event": threading.Event()}
            self._error = None
            self._condition.notify_all()
            return self.snapshot()

    def heartbeat(self, payload):
        self._watch_once()
        with self._condition:
            press_id = self._payload(payload, {"sequence"})
            sequence = payload.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= 2 ** 53 - 1:
                raise AutomationError("invalid_value", "A positive heartbeat sequence is required", "sequence")
            if (self._last_stop_reason == "travel_limit" and press_id in self._released and self._active is None
                    and not self._blocked and not self._closed and not self._frozen):
                return self.snapshot()
            self._available()
            if not self._active or self._active["press_id"] != press_id or press_id in self._released:
                raise AutomationError("alignment_press_released", "The press is no longer active", status=409)
            if sequence <= self._active["sequence"]:
                raise AutomationError("alignment_heartbeat_order", "An old heartbeat cannot extend a lease", "sequence", 409)
            self._active.update(sequence=sequence, deadline=self._clock() + self.LEASE_SECONDS)
            self._condition.notify_all()
            return self.snapshot()

    def stop(self, payload):
        emit = getattr(self._motion, "_event", None)
        if callable(emit):
            emit("keyboard_stop_received", press_id=payload.get("press_id"))
        with self._lock:
            press_id = self._payload(payload, ())
            self._remember(press_id)
            self._released.add(press_id)
            active = self._active is not None and self._active["press_id"] == press_id
            pending_stop = self._stop_done if self._stopping else None
        if active:
            self._halt("released")
        elif pending_stop is not None and not pending_stop.wait(15):
            raise AutomationError("alignment_stop_unknown", "Timed out waiting for the current jog STOP", status=409)
        if callable(emit):
            emit("keyboard_stop_ready", press_id=press_id, stop_result=self._stop_result)
        context = getattr(self._motion, "_keyboard_trace_context", None)
        if context and context.get("press_id") == press_id:
            self._motion._keyboard_trace_context = None
        return self.snapshot()

    def freeze(self):
        with self._lock:
            if self._frozen and not self._stopping and self._step_done.is_set() and self._idle_done.is_set():
                if self._blocked:
                    raise AutomationError("alignment_unavailable", "Keyboard alignment requires safe recovery", status=409)
                return self.snapshot()
            self._frozen = True
        self._halt("frozen", force=True)
        return self.snapshot()

    def resume(self, session_token):
        with self._condition:
            if session_token != self._token or self._blocked or self._closed or self._stopping or self._inflight:
                raise AutomationError("alignment_session", "The keyboard session cannot be resumed", status=409)
            self._frozen = False
            self._condition.notify_all()
            return self.snapshot()

    def close(self, *, stop=True):
        with self._condition:
            if not stop and (not self._frozen or self._inflight or self._stopping
                             or not self._stop_result or self._stop_result.get("stop_confirmed") is not True):
                raise AutomationError("alignment_stop_unknown", "Freeze and confirm STOP before ending keyboard alignment", status=409)
            self._closed = True
            self._frozen = True
            self._condition.notify_all()
        if stop:
            self._halt("closed", force=True)

    def retire(self):
        with self._condition:
            if (self._active or self._inflight or self._idle_inflight or self._stopping
                    or self._halt_count or not self._step_settled.is_set() or not self._idle_settled.is_set()):
                raise AutomationError("alignment_stop_unknown", "Drain the old keyboard session before replacement", status=409)
            self._closed = self._frozen = True
            self._condition.notify_all()

    def prepare_stop_recovery(self):
        with self._condition:
            self._frozen = True
            self._stop_result = None
            if self._active:
                self._active["cancel_event"].set()
                self._released.add(self._active["press_id"])
                self._active = None
            self._condition.notify_all()
        if not (self._step_settled.wait(15) and self._idle_settled.wait(15)
                and self._stop_done.wait(15) and self._halt_settled.wait(15)):
            raise AutomationError("alignment_stop_unknown", "Keyboard requests have not finished draining", status=409)

    def reconcile_stop_recovery(self, motion, generation, connection_token):
        with self._condition:
            if (motion is not self._motion or not self._frozen or self._active or self._inflight
                    or self._idle_inflight or self._stopping or self._halt_count or not self._step_settled.is_set()
                    or not self._idle_settled.is_set()):
                raise AutomationError("alignment_stop_unknown", "Keyboard STOP recovery is not drained", status=409)
            before = motion.snapshot()
            result = motion._current_stop_result()
            after = motion.snapshot()
            if (before.get("connection_token") != connection_token
                    or after.get("connection_token") != connection_token
                    or before.get("stop_generation") != after.get("stop_generation")
                    or after.get("stop_generation", -1) <= generation):
                raise AutomationError("alignment_stop_unknown", "Keyboard STOP recovery requires new current-connection evidence", status=409)
            self._stop_result = copy.deepcopy(result)
            self._last_stop_reason = "explicit_stop_recovery"
            return self.snapshot()

    def _halt(self, reason, *, force=False, error=None):
        with self._condition:
            self._halt_count += 1
            self._halt_settled.clear()
        try:
            return self._halt_once(reason, force=force, error=error)
        finally:
            with self._condition:
                self._halt_count -= 1
                if not self._halt_count:
                    self._halt_settled.set()
                self._condition.notify_all()

    def _halt_once(self, reason, *, force=False, error=None):
        with self._condition:
            if self._active:
                self._active["cancel_event"].set()
                self._released.add(self._active["press_id"])
                self._active = None
            elif not force and not self._stopping:
                return
            self._last_stop_reason = reason
            if self._stopping:
                pending = self._stop_done
                owner = False
            else:
                pending = self._stop_done = threading.Event()
                self._stopping = True
                owner = True
            self._condition.notify_all()
        if not owner:
            if not pending.wait(15):
                raise AutomationError("alignment_stop_unknown", "Timed out waiting for jog STOP", status=409)
            if self.snapshot()["status"] == "blocked":
                notification = None
                with self._condition:
                    if error is not None and self._error is None:
                        original = {"code": getattr(error, "code", "alignment_error"), "message": str(error)}
                        if isinstance(getattr(error, "wire_diagnostic", None), dict):
                            original["wire"] = copy.deepcopy(error.wire_diagnostic)
                        confirmed = self._stop_result and self._stop_result.get("stop_confirmed") is True
                        self._error = original if confirmed else {
                            "code": "alignment_stop_unknown", "message": "Jog STOP requires safe recovery", "cause": original}
                        notification = (copy.deepcopy(self._error), copy.deepcopy(self._stop_result))
                    failure = copy.deepcopy(self._error) or {
                        "code": "alignment_stop_unknown", "message": "Jog STOP requires safe recovery"}
                if notification is not None and self._on_blocked:
                    self._on_blocked(*notification)
                raise AutomationError(failure["code"], failure["message"], status=409)
            if error is not None:
                self._halt(reason, force=True, error=error)
            return
        result = {"stop_confirmed": False, "axes": {}, "errors": []}
        supplied_stop = getattr(error, "stop_result", None)
        if isinstance(supplied_stop, dict) and isinstance(supplied_stop.get("stop_confirmed"), bool):
            result = copy.deepcopy(supplied_stop)
        else:
            try:
                result = self._motion.stop_keyboard_alignment()
            except Exception as stop_error:
                result["errors"].append({"code": getattr(stop_error, "code", "alignment_stop_unknown"), "message": str(stop_error)})
        drained = self._step_done.wait(15) and self._idle_done.wait(15)
        original_fault = None
        if error is not None:
            original_fault = {"code": getattr(error, "code", "alignment_error"), "message": str(error)}
            if isinstance(getattr(error, "wire_diagnostic", None), dict):
                original_fault["wire"] = copy.deepcopy(error.wire_diagnostic)
        if result.get("stop_confirmed") is not True or not drained:
            error = AutomationError("alignment_stop_unknown", "Jog STOP or in-flight completion is unconfirmed; retain ownership for safe recovery", status=409)
        with self._condition:
            self._stop_result = copy.deepcopy(result)
            self._stopping = False
            if error:
                self._blocked = self._frozen = True
                preserve_fault = self._error and (original_fault is not None and original_fault["code"] == "reconnect_required"
                    or original_fault is None and getattr(error, "code", None) == "alignment_stop_unknown")
                if preserve_fault:
                    original_fault = copy.deepcopy(self._error)
                self._error = (copy.deepcopy(original_fault) if preserve_fault and getattr(error, "code", None) in {"reconnect_required", original_fault["code"]}
                               else {"code": getattr(error, "code", "alignment_error"), "message": str(error)})
                if original_fault is not None:
                    if original_fault["code"] != self._error["code"]:
                        self._error["cause"] = original_fault
                    elif "wire" in original_fault:
                        self._error["wire"] = original_fault["wire"]
            pending.set()
            self._condition.notify_all()
            error_payload = copy.deepcopy(self._error)
        if error:
            if self._on_blocked:
                self._on_blocked(error_payload, copy.deepcopy(result))
            raise AutomationError(error_payload["code"], error_payload["message"], "calibration", 409)

    def _watch_once(self, *, refresh_idle=False):
        with self._lock:
            reason = self._expired(self._clock())
        if reason:
            self._halt(reason)
        if refresh_idle:
            self._refresh_idle_once()

    def _refresh_idle_once(self):
        refresh_keyboard = getattr(self._motion, "refresh_keyboard_alignment", None)
        refresh = refresh_keyboard if callable(refresh_keyboard) else getattr(self._motion, "refresh", None)
        if not callable(refresh):
            return
        with self._lock:
            now = self._clock()
            if (self._active or self._inflight or self._idle_inflight or self._frozen or self._stopping
                    or self._blocked or self._closed or now - self._last_idle_refresh < self.IDLE_REFRESH_SECONDS):
                return
            self._last_idle_refresh = now
            self._idle_inflight = True
            cancellation = self._idle_cancel_event = threading.Event()
            self._idle_done.clear()
            self._idle_settled.clear()
        failure = None
        try:
            if callable(refresh_keyboard):
                refresh(cancel_event=cancellation)
            else:
                refresh()
        except Exception as error:
            if getattr(error, "code", None) not in ("motion_busy", "cancelled"):
                failure = error
        finally:
            with self._condition:
                if failure is not None:
                    self._blocked = self._frozen = True
                self._idle_inflight = False
                if self._idle_cancel_event is cancellation:
                    self._idle_cancel_event = None
                self._idle_done.set()
                self._condition.notify_all()
        try:
            if failure:
                self._halt("fault", force=True, error=failure)
        finally:
            self._idle_settled.set()

    def _step_once(self):
        fast_hold = False
        with self._lock:
            if not self._active or self._stopping or self._frozen or self._blocked or self._closed or self._inflight:
                return
            reason = self._expired(self._clock())
            if reason:
                segment = None
            elif self._idle_inflight:
                return True
            else:
                active = self._active
                state = self._motion.snapshot()["axes"][active["axis"]]
                mechanical = state["mechanical"]
                speed = math.floor(self._settings["speed_mm_s"] * 60 / mechanical["lead_mm"]) * mechanical["lead_mm"] / 60
                fast_hold = self._settings["speed_mm_s"] > 2
                duration = None if fast_hold else min(self.MAX_SEGMENT_SECONDS, self.MAX_SEGMENT_MM / speed)
                pulses_per_rev = state.get("config", {}).get("pulses_per_rev")
                if not fast_hold and pulses_per_rev and duration * speed < mechanical["lead_mm"] / pulses_per_rev - 1e-12:
                    reason = "pulse_resolution"
                    segment = None
                else:
                    if not fast_hold:
                        active["distance_mm"] += speed * duration
                    self._inflight = True
                    self._step_done.clear()
                    self._step_settled.clear()
                    segment = (active["axis"], self.DIRECTIONS[(active["axis"], active["direction"])],
                               self._settings["speed_mm_s"], duration,
                               self._settings["max_speed_mm_s"] if fast_hold else min(2, self._settings["max_speed_mm_s"]),
                               active["cancel_event"])
        if reason:
            self._halt(reason)
            return
        failure = None
        busy = False
        exhausted = False
        stop_reason = None
        try:
            if fast_hold:
                result = self._motion.alignment_fast_hold(segment[0], segment[1], segment[2], segment[4],
                                                          None, None, cancel_event=segment[5])
                exhausted = result.get("hold_exhausted") is True
                stop_reason = result.get("stop_reason", "travel_limit")
                with self._condition:
                    if self._active is active:
                        active["distance_mm"] += result["segment_mm"]
            else:
                result = self._motion.alignment_step(*segment[:5], cancel_event=segment[5])
                exhausted = result.get("hold_exhausted") is True
                stop_reason = result.get("stop_reason", "travel_limit")
        except Exception as error:
            if getattr(error, "code", None) == "motion_busy":
                busy = True
            elif not (segment[5].is_set() and getattr(error, "code", None) == "cancelled"):
                failure = error
        finally:
            with self._condition:
                if busy and not fast_hold and self._active is active:
                    active["distance_mm"] = max(0.0, active["distance_mm"] - speed * duration)
                self._inflight = False
                self._step_done.set()
                self._condition.notify_all()
        try:
            if failure:
                self._halt("fault", force=True, error=failure)
            elif exhausted:
                self._halt(stop_reason)
            else:
                self._watch_once()
        finally:
            self._step_settled.set()
        return busy

    def _work(self):
        while True:
            with self._condition:
                while not self._closed and (not self._active or self._stopping or self._frozen or self._blocked):
                    self._condition.wait()
                if self._closed:
                    return
            try:
                if self._step_once():
                    with self._condition:
                        self._condition.wait(0.01)
            except AutomationError:
                pass

    def _watch(self):
        while True:
            with self._condition:
                if self._closed:
                    return
                self._condition.wait(0.025)
            try:
                self._watch_once(refresh_idle=True)
            except AutomationError:
                pass
