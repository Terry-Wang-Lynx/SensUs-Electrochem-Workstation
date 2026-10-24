import copy
import time
from types import SimpleNamespace

import pytest
from serial import SerialException

from pa_host.automation import AutomationController, AutomationError, default_experiment
from pa_host.emm_motion import SharedEmmBus
from pa_host.guided_automation import GuidedAutomation
from test_guided_integration import dispatch, guided_app
from test_keyboard_stop_recovery import recovery_app
from test_powered_manual_driver import PoweredManualSerial


def unavailable_controller(tmp_path, failure, *, identity_fails=False):
    serials = []
    def factory(port, baud, axes, **options):
        def open_port(**settings):
            if port == "missing-port" and not identity_fails:
                raise failure
            serial = PoweredManualSerial(**settings)
            serial.statuses = {1: 3, 2: 3}
            if identity_fails:
                serial.overrides[1, 0x1F] = b""
                serial.overrides[2, 0xFE] = b""
            serials.append(serial)
            return serial
        if identity_fails:
            options["transaction_timeout_s"] = 0.01
        bus = SharedEmmBus(port, baud, axes, serial_factory=open_port, **options)
        bus._command_gap_s = 0
        return bus
    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                      motion_factory=factory, motion_feedback_interval_s=60)
    motion = default_experiment()["bindings"]["motion"]
    motion["port"] = "missing-port"
    return controller, motion, serials


@pytest.mark.parametrize("failure", [FileNotFoundError("USB removed"), SerialException("port not found")])
def test_never_opened_port_does_not_latch_unknown_stop(tmp_path, failure):
    controller, motion, serials = unavailable_controller(tmp_path, failure)
    try:
        with pytest.raises(AutomationError) as caught:
            controller.connect_motion(motion)
        assert caught.value.code == "motion_port_unavailable"
        assert serials == []
        assert controller._motion is None
        assert not controller.snapshot()["recovery_required"]
        assert not controller.is_busy()
        replacement = {**motion, "port": "replacement-port"}
        controller.connect_motion(replacement)
        assert controller._motion.snapshot()["connected"] is True
    finally:
        controller._stop_motion_feedback()
        if serials:
            controller._motion.close()


def test_successful_replacement_connection_tolerates_one_windows_timer_tick(tmp_path, monkeypatch):
    read = PoweredManualSerial.read
    delayed = []

    def delayed_first_reply(serial, count):
        if not delayed:
            delayed.append(True)
            time.sleep(0.02)
        return read(serial, count)

    monkeypatch.setattr(PoweredManualSerial, "read", delayed_first_reply)
    controller, motion, serials = unavailable_controller(tmp_path, FileNotFoundError("USB removed"))
    try:
        with pytest.raises(AutomationError) as caught:
            controller.connect_motion(motion)
        assert caught.value.code == "motion_port_unavailable"
        controller.connect_motion({**motion, "port": "replacement-port"})
        assert delayed
        assert controller._motion.snapshot()["connected"] is True
        assert controller._motion._timeout == 1.0
        assert not controller.snapshot()["recovery_required"]
        assert not controller.is_busy()
    finally:
        controller._stop_motion_feedback()
        if controller._motion is not None:
            controller._motion.close()


def test_opened_port_with_unknown_stop_preserves_ownership(tmp_path):
    controller, motion, serials = unavailable_controller(tmp_path, FileNotFoundError(), identity_fails=True)
    try:
        with pytest.raises(Exception):
            controller.connect_motion(motion)
        assert len(serials) == 1
        assert controller._motion is not None
        assert controller.snapshot()["recovery_required"] is True
        assert controller.is_busy()
        with pytest.raises(AutomationError):
            controller.connect_motion({**motion, "port": "replacement-port"})
    finally:
        controller._stop_motion_feedback()
        serials[0].overrides.clear()
        controller._motion.close()


def test_existing_unknown_stop_survives_another_port_open_failure(tmp_path):
    controller, motion, serials = unavailable_controller(tmp_path, FileNotFoundError("USB removed"))
    controller._recovery_required = True
    controller._owns_motion = True
    controller._recovery_motion_config = copy.deepcopy(motion)
    with pytest.raises(AutomationError):
        controller.connect_motion(motion)
    assert controller.snapshot()["recovery_required"] is True
    assert controller.is_busy()
    assert controller._recovery_motion_config == motion


def detach_old_port(controller):
    old_driver = controller._motion
    old_driver._connected = False
    old_driver._serial = None
    controller._recovery_required = True
    controller._owns_motion = True
    controller._recovery_motion_config = copy.deepcopy(controller._motion_config)
    return old_driver


def available_ports(monkeypatch, old_port=None):
    ports = [SimpleNamespace(device="replacement-port")]
    if old_port is not None:
        ports.append(SimpleNamespace(device=old_port))
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: ports)


def assert_read_stop_only(serials):
    assert not any(frame[1] in (0xF3, 0xFD, 0xF6, 0x0A, 0x9A) for serial in serials for frame in serial.writes)


def test_explicit_same_bus_port_recovery_stops_candidate_then_clears_old_points(recovery_app, monkeypatch):
    controller, service, serials = recovery_app
    original = copy.deepcopy(service.snapshot()["document"])
    assert service.snapshot()["manual"]["points"]
    detach_old_port(controller)
    available_ports(monkeypatch)
    candidate_stop_addresses = set()
    factory = controller._motion_factory
    def observe_candidate_stop(port, baud, axes, **options):
        bus = factory(port, baud, axes, **options)
        serial = serials[-1]
        write = serial.write
        def observed_write(frame):
            if frame[1] == 0xFE and frame[0] not in candidate_stop_addresses:
                assert service.snapshot()["manual"]["points"]
                candidate_stop_addresses.add(frame[0])
            return write(frame)
        serial.write = observed_write
        return bus
    monkeypatch.setattr(controller, "_motion_factory", observe_candidate_stop)
    result = dispatch("motion/reset", {"port": "replacement-port", "same_bus_confirmed": True})
    assert result["motion"]["connected"] is True
    assert result["motion"]["port"] == "replacement-port"
    assert result["state"]["stop_confirmed"] is True
    assert not result["state"]["recovery_required"]
    current = service.snapshot()
    assert current["manual"]["points"] == {}
    assert current["manual"]["active"] is False
    assert current["calibration"] is None or current["calibration"].get("usable") is not True
    expected = copy.deepcopy(original)
    expected["bindings"]["motion"]["port"] = "replacement-port"
    assert current["document"] == expected
    assert GuidedAutomation(service._path.parent, require_four_point=True).snapshot()["document"] == expected
    assert candidate_stop_addresses == {1, 2}
    assert controller._motion_config == expected["bindings"]["motion"]
    assert {frame[0] for frame in serials[-1].writes if frame[1] == 0xFE} == {1, 2}
    assert_read_stop_only(serials)


@pytest.mark.parametrize("blocked", ["missing_confirmation", "old_present", "old_connected", "active_worker"])
def test_port_migration_requires_exclusive_explicit_recovery(recovery_app, monkeypatch, blocked):
    controller, service, serials = recovery_app
    original = copy.deepcopy(service.snapshot()["document"])
    old_driver = controller._motion
    if blocked != "old_connected":
        detach_old_port(controller)
    available_ports(monkeypatch, old_driver.port if blocked == "old_present" else None)
    if blocked == "active_worker":
        monkeypatch.setattr(controller, "_thread", SimpleNamespace(is_alive=lambda: True))
    payload = {"port": "replacement-port", "same_bus_confirmed": blocked != "missing_confirmation"}
    with pytest.raises(AutomationError) as caught:
        dispatch("motion/reset", payload)
    assert caught.value.code == ("motion_port_confirmation" if blocked == "missing_confirmation" else "motion_port_replacement")
    assert controller._motion is old_driver
    assert len(serials) == 1
    assert service.snapshot()["document"] == original
    assert_read_stop_only(serials)


@pytest.mark.parametrize("failure", ["open", "stop"])
def test_failed_replacement_preserves_old_recovery_and_points(recovery_app, monkeypatch, failure):
    controller, service, serials = recovery_app
    original = copy.deepcopy(service.snapshot())
    old_driver = detach_old_port(controller)
    available_ports(monkeypatch)
    factory = controller._motion_factory
    def failed_candidate(port, baud, axes, **options):
        if failure == "open":
            bus = factory(port, baud, axes, **options)
            def fail_open(**settings):
                raise FileNotFoundError("replacement unplugged")
            bus._serial_factory = fail_open
            return bus
        bus = factory(port, baud, axes, **options)
        serials[-1].overrides[2, 0xFE] = b""
        return bus
    monkeypatch.setattr(controller, "_motion_factory", failed_candidate)
    with pytest.raises(AutomationError) as caught:
        dispatch("motion/reset", {"port": "replacement-port", "same_bus_confirmed": True})
    assert caught.value.code == ("device_reset_connection" if failure == "open" else "device_reset_stop_unknown")
    assert controller.snapshot()["recovery_required"] is True
    assert controller.is_busy()
    assert controller._recovery_motion_config["port"] == old_driver.port
    assert service.snapshot()["document"] == original["document"]
    assert service.snapshot()["manual"]["points"] == original["manual"]["points"]
    assert_read_stop_only(serials)


def test_serial_opened_history_survives_close_and_later_failed_open(tmp_path):
    controller, motion, serials = unavailable_controller(tmp_path, FileNotFoundError("missing"))
    motion["port"] = "replacement-port"
    controller.connect_motion(motion)
    controller._stop_motion_feedback()
    driver = controller._motion
    assert driver.snapshot()["serial_opened_once"] is True
    assert driver.close()["stop_confirmed"] is True
    def fail_open(**settings):
        raise FileNotFoundError("USB removed again")
    driver._serial_factory = fail_open
    with pytest.raises(Exception):
        driver.connect()
    assert driver.snapshot()["serial_opened_once"] is True
    assert not driver.snapshot()["connected"]
    assert_read_stop_only(serials)


def test_candidate_motion_stop_does_not_erase_unconfirmed_board_ownership(recovery_app, monkeypatch):
    controller, service, serials = recovery_app
    detach_old_port(controller)
    available_ports(monkeypatch)
    controller._owned_boards = ["fake-ble-board"]
    board_stops = []
    def fail_board_stop(address):
        board_stops.append(address)
        return {"stop_confirmed": False}
    controller._board_stop_factory = fail_board_stop
    with pytest.raises(AutomationError) as caught:
        dispatch("motion/reset", {"port": "replacement-port", "same_bus_confirmed": True})
    assert caught.value.code == "device_reset_stop_unknown"
    assert board_stops == ["fake-ble-board"]
    assert controller.snapshot()["recovery_required"] is True
    assert controller.snapshot()["stop_confirmed"] is False
    assert controller._owned_boards == ["fake-ble-board"]
    assert controller._motion_config["port"] == "replacement-port"
    assert controller._motion.snapshot()["stop_confirmed"] is True
    current = service.snapshot()
    assert current["document"]["bindings"]["motion"]["port"] == "replacement-port"
    assert current["manual"]["points"] == {}
    assert current["manual"]["active"] is False
    assert_read_stop_only(serials)


def test_new_external_stop_generation_prevents_candidate_adoption(recovery_app, monkeypatch):
    controller, service, serials = recovery_app
    original = copy.deepcopy(service.snapshot())
    previous = detach_old_port(controller)
    available_ports(monkeypatch)
    factory = controller._motion_factory
    external_stops = []
    def racing_candidate(port, baud, axes, **options):
        driver = factory(port, baud, axes, **options)
        serial = serials[-1]
        write = serial.write
        def stop_during_candidate(frame):
            if frame[1] == 0xFE and not external_stops:
                external_stops.append(previous.stop_all())
            return write(frame)
        serial.write = stop_during_candidate
        return driver
    monkeypatch.setattr(controller, "_motion_factory", racing_candidate)
    with pytest.raises(AutomationError) as caught:
        dispatch("motion/reset", {"port": "replacement-port", "same_bus_confirmed": True})
    assert caught.value.code == "cancelled"
    assert external_stops and external_stops[0]["stop_confirmed"] is False
    assert controller._motion is previous
    assert controller._motion_config["port"] == previous.port
    assert controller._recovery_motion_config["port"] == previous.port
    assert controller.snapshot()["recovery_required"] is True
    assert service.snapshot()["document"] == original["document"]
    assert service.snapshot()["manual"]["points"] == original["manual"]["points"]
    assert_read_stop_only(serials)
