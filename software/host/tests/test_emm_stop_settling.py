from test_emm_motion import signed
from test_powered_manual_driver import powered_bus


def test_stop_waits_for_two_zero_speed_samples_after_initial_deceleration():
    bus, serial, events = powered_bus(enabled=True)
    speeds = iter((1, 0, 0))
    observed = []

    def speed_reply(frame):
        speed = next(speeds, 0)
        observed.append(speed)
        return bytes([1, 0x35]) + signed(speed, 2) + b"\x6b"

    serial.overrides[1, 0x35] = speed_reply
    result = bus.stop_all()
    assert result["stop_confirmed"] is True
    assert result["axes"]["X"]["stop_basis"] == "acknowledged_stop"
    assert observed[:3] == [1, 0, 0]
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6) for frame in serial.writes)


def test_stop_never_accepts_a_permanently_nonzero_speed():
    bus, serial, events = powered_bus(enabled=True)
    serial.overrides[1, 0x35] = bytes.fromhex("01350000016b")
    result = bus.stop_all()
    assert result["stop_confirmed"] is False
    assert result["axes"]["Z"]["stop_confirmed"] is True
    assert bus.snapshot()["faulted"] is True
