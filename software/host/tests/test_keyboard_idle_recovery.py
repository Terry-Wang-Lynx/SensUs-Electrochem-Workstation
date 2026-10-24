from types import SimpleNamespace

import pytest

from pa_host.emm_motion import MotionError, SharedEmmBus
from test_emm_motion import AXES
from test_powered_manual_driver import PoweredManualSerial, powered_bus


def test_default_read_budget_handles_observed_desktop_scheduling_delay(monkeypatch):
    serial = PoweredManualSerial(timeout=0.2, write_timeout=0.2)
    bus = SharedEmmBus("fake-desktop-idle", 115200, AXES, serial_factory=lambda **options: serial)
    bus.connect()
    clock = SimpleNamespace(now=100.0, delayed=False)
    original_read = serial.read

    def delayed_read(count):
        if not clock.delayed:
            clock.now += 0.627
            clock.delayed = True
        return original_read(count)

    serial.read = delayed_read
    monkeypatch.setattr("pa_host.emm_motion.time", SimpleNamespace(
        monotonic=lambda: clock.now, time=lambda: 1700000000 + clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds)))
    bus._last_exchange_monotonic = None
    result = bus._read_axis("X")
    assert result["telemetry_valid"] is True
    assert result["updated_monotonic"] == 100
    assert 0.627 <= clock.now - result["updated_monotonic"] < 2


@pytest.mark.parametrize("missing", [False, True])
def test_idle_feedback_fault_attempts_stop_even_after_driver_is_faulted(missing):
    bus, serial, events = powered_bus(enabled=True)
    bus.begin_keyboard_alignment(confirmed=True)
    bus._timeout = 0.01
    serial.writes.clear()

    def malformed_once(frame):
        serial.overrides.pop((1, 0x36))
        return bytes.fromhex("013600000100006a")

    serial.overrides[1, 0x36] = b"" if missing else malformed_once
    with pytest.raises(MotionError) as caught:
        bus.refresh()
    assert caught.value.stop_result is not None
    assert caught.value.stop_result["stop_confirmed"] is (not missing)
    assert {frame[0] for frame in serial.writes if frame[1] == 0xFE} == {1, 2}
    assert bus.snapshot()["faulted"] is True
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6) for frame in serial.writes)


@pytest.mark.parametrize("previous_stop", [False, True])
def test_keyboard_stop_after_fault_obtains_new_evidence_instead_of_requiring_reference(previous_stop):
    bus, serial, events = powered_bus(enabled=True)
    bus.begin_keyboard_alignment(confirmed=True)
    bus._fault(MotionError("checksum", "Invalid idle reply"))
    if previous_stop:
        assert bus.stop_all()["stop_confirmed"]
    serial.writes.clear()
    assert bus.stop_keyboard_alignment()["stop_confirmed"]
    assert [frame[0] for frame in serial.writes if frame[1] == 0xFE] == [1, 2]
    assert bus.snapshot()["faulted"] is True
    assert bus.snapshot()["manual"]["active"] is False
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6) for frame in serial.writes)
