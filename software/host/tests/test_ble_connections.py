import time
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pa_host.ble_connections import BleConnections
from test_v52_ble import FakeClient, fake_bleak


@pytest.fixture
def connections(fake_bleak):
    pool = BleConnections()
    yield pool
    pool.close()


def test_connecting_second_board_keeps_first_connected(connections):
    connections.connect("first", "front")
    connections.connect("second", "rear")
    assert all(client.is_connected for client in FakeClient.instances)
    assert {entry["state"] for entry in connections.snapshot().values()} == {"connected"}
    assert [client.commands for client in FakeClient.instances] == [["INFO", "STATUS"]] * 2
    connections.connect("FIRST")
    assert len(FakeClient.instances) == 2


def test_second_board_failure_preserves_first(connections):
    connections.connect("first")
    FakeClient.responses = {"INFO": ["ERR invalid"]}
    with pytest.raises(RuntimeError):
        connections.connect("second")
    assert FakeClient.instances[0].is_connected
    assert not FakeClient.instances[1].is_connected
    assert connections.snapshot()["first"]["state"] == "connected"
    assert connections.snapshot()["second"]["state"] == "error"


def test_handoff_releases_only_requested_board(connections):
    connections.connect("first")
    connections.connect("second")
    connections.disconnect("FIRST")
    assert not FakeClient.instances[0].is_connected
    assert FakeClient.instances[1].is_connected
    assert connections.snapshot()["first"]["state"] == "disconnected"


def test_recovery_stop_is_sent_even_when_info_fails(connections):
    FakeClient.responses = {"INFO": ["ERR invalid"]}
    assert connections.stop("first")["stop_confirmed"]
    assert FakeClient.instances[0].commands == ["STOP", "STATUS"]
    assert connections.snapshot()["first"]["protocol_verified"] is False


def test_reconnect_after_command_error_revalidates_without_dropping_other_board(connections):
    connections.connect("first")
    connections.connect("second")
    FakeClient.responses = {"STOP": ["ERR failed"]}
    with pytest.raises(RuntimeError):
        connections.stop("first")
    FakeClient.responses = {}
    assert connections.connect("first")["state"] == "connected"
    assert FakeClient.instances[-1].commands == ["INFO", "STATUS"]
    assert FakeClient.instances[1].is_connected


def test_configuring_one_board_keeps_other_connected(connections):
    connections.connect("first")
    connections.connect("second")
    assert connections.configure("first", {})["config_confirmed"]
    assert FakeClient.instances[1].commands == ["INFO", "STATUS"]
    assert all(client.is_connected for client in FakeClient.instances)


def test_radio_loss_changes_cached_state_without_commands(connections):
    connections.connect("first")
    client = FakeClient.instances[0]
    client.is_connected = False
    deadline = time.monotonic() + 2
    while connections.snapshot()["first"]["state"] == "connected" and time.monotonic() < deadline:
        time.sleep(.02)
    assert connections.snapshot()["first"]["state"] == "error"
    assert client.commands == ["INFO", "STATUS"]
    connections.connect("first")
    assert connections.snapshot()["first"]["state"] == "connected"


def test_unconfirmed_disconnect_blocks_handoff_but_not_other_board(connections, monkeypatch):
    connections.connect("first")
    connections.connect("second")
    original = FakeClient.__aexit__

    async def broken_exit(client, *args):
        if client.address == "first":
            raise OSError("disconnect failed")
        return await original(client, *args)

    with monkeypatch.context() as patch:
        patch.setattr(FakeClient, "__aexit__", broken_exit)
        with pytest.raises(OSError):
            connections.disconnect("first")
        assert connections.snapshot()["first"]["state"] == "error"
        assert FakeClient.instances[1].is_connected
    connections.disconnect("first")


def test_binding_is_atomic_distinct_and_preserves_motor_calibration(tmp_path):
    from pa_host.guided_automation import GuidedAutomation
    from pa_host.automation import AutomationError

    service = GuidedAutomation(tmp_path)
    original = service.snapshot()["document"]
    service._manual.update(active=True)
    result = service.bind_boards({"A": "first", "B": "second"})
    assert result["manual"]["active"]
    assert result["document"]["bindings"]["motion"] == original["bindings"]["motion"]
    assert result["document"]["bindings"]["boards"]["A"]["settings"] == original["bindings"]["boards"]["A"]["settings"]
    with pytest.raises(AutomationError, match="同一"):
        service.bind_boards({"A": "first", "B": "FIRST"})
    assert service.snapshot()["document"] == result["document"]
    swapped = service.bind_boards({"A": "second", "B": "first"})
    loaded = GuidedAutomation(tmp_path).snapshot()
    assert loaded["document"] == swapped["document"]


@pytest.fixture
def board_app(connections, tmp_path, monkeypatch):
    from pa_host import gui_server
    from pa_host.automation import AutomationController
    from pa_host.guided_automation import GuidedAutomation

    controller = AutomationController(tmp_path / "state", tmp_path / "runs", tmp_path,
                                      board_release_factory=connections.disconnect)
    app = SimpleNamespace(ble_connections=connections, automation=controller,
        guided_automation=GuidedAutomation(tmp_path / "guided"), operation_lock=threading.RLock(),
        measurement=SimpleNamespace(is_busy=lambda: False),
        schedule=SimpleNamespace(snapshot=lambda: {"active": False}),
        settings=SimpleNamespace(snapshot=lambda: {"state": "idle"}, restore_for_transport=Mock()),
        hardware_idle=lambda: not controller.is_busy())
    monkeypatch.setattr(gui_server, "APP", app)
    monkeypatch.setattr(gui_server, "APP_UPDATER", SimpleNamespace(busy=False))
    monkeypatch.setattr(gui_server, "SHUTDOWN_INTENT", threading.Event())
    monkeypatch.setattr(gui_server, "JLINK_DRIVER_INSTALL_LOCK", threading.Lock())
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [
        {"id": "ble:" + address, "address": address, "name": "same-name", "kind": "ble", "selectable": True}
        for address in ("first", "second")])
    monkeypatch.setattr(gui_server, "SELECTED_DEVICE", None)
    monkeypatch.setattr(gui_server, "HARDWARE_TRANSPORT", "ble")
    monkeypatch.setattr(gui_server, "_refresh_usb_transport", lambda **kwargs: None)
    return app


def test_real_device_selection_path_does_not_disconnect_previous_board(board_app):
    from pa_host import gui_server

    handler = gui_server.RequestHandler.__new__(gui_server.RequestHandler)
    handler.path = "/api/devices/select"
    handler._send_json = Mock()
    for address in ("first", "second"):
        handler._body = lambda: {"device_id": "ble:" + address}
        handler._do_POST()
        assert "error" not in handler._send_json.call_args.args[0]
    result = handler._send_json.call_args.args[0]
    assert result["selected_device_id"] == "ble:second"
    assert [row["connection_state"] for row in result["devices"]] == ["connected", "connected"]
    assert all(client.is_connected for client in FakeClient.instances)


def test_board_endpoints_allow_motor_calibration_and_preserve_partial_success(board_app):
    from pa_host import gui_server

    board_app.automation._manual_calibration_active = True
    board_app.guided_automation._manual["active"] = True
    result = gui_server._automation_dispatch("POST", "boards/bind", {"addresses": {"A": "first", "B": "second"}}, {})
    assert result["busy"] is False
    assert result["guided"]["manual"]["active"]
    gui_server._automation_dispatch("POST", "boards/connect", {"addresses": ["first"]}, {})
    FakeClient.responses = {"INFO": ["ERR invalid"]}
    result = gui_server._automation_dispatch("POST", "boards/connect", {"addresses": ["first", "second"]}, {})
    assert list(result["errors"]) == ["second"]
    assert result["connections"]["first"]["state"] == "connected"
    assert gui_server._high_throughput_devices()["boards"]["A"]["state"] == "connected"
    assert gui_server._high_throughput_devices()["boards"]["B"]["state"] == "connection_error"


def test_board_get_is_cached_and_active_collectors_block_binding(board_app, monkeypatch):
    from pa_host import gui_server
    from pa_host.automation import AutomationError

    monkeypatch.setattr(gui_server.v52_ble, "scan_devices", Mock(side_effect=AssertionError("GET scanned")))
    board_app.automation._thread = SimpleNamespace(is_alive=lambda: True)
    assert gui_server._automation_dispatch("GET", "boards", {}, {})["busy"]
    with pytest.raises(AutomationError) as caught:
        gui_server._automation_dispatch("POST", "boards/connect", {"addresses": ["first"]}, {})
    assert caught.value.code == "boards_busy"
    with pytest.raises(AutomationError):
        gui_server._automation_dispatch("POST", "boards/bind", {"addresses": {"A": "first", "B": "second"}}, {})
    assert FakeClient.instances == []


def test_connected_board_stays_listed_when_not_advertising(board_app, monkeypatch):
    from pa_host import gui_server

    board_app.ble_connections.connect("first", "same-name")
    monkeypatch.setattr(gui_server, "DEVICE_DISCOVERY_CACHE", [])
    payload = gui_server._automation_dispatch("GET", "boards", {}, {})
    assert [(row["address"], row["connection_state"]) for row in payload["devices"]] == [("first", "connected")]
    assert FakeClient.instances[0].commands == ["INFO", "STATUS"]


def test_normal_shutdown_closes_both_standby_connections(board_app):
    from pa_host import gui_server

    board_app.ble_connections.connect("first")
    board_app.ble_connections.connect("second")
    assert gui_server._release_hardware_for_shutdown(timeout_s=1)["ok"]
    assert all(not client.is_connected for client in FakeClient.instances)


def test_agent_schema_lists_binding_and_independent_connection_endpoints():
    from pa_host.automation import openapi_document

    paths = openapi_document()["paths"]
    assert "get" in paths["/api/automation/v1/boards"]
    for operation in ("scan", "bind", "connect", "disconnect"):
        assert "post" in paths["/api/automation/v1/boards/" + operation]
    binding = paths["/api/automation/v1/boards/bind"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert binding["properties"]["addresses"]["required"] == ["A", "B"]


def test_dual_collectors_release_each_standby_before_spawn(board_app, tmp_path):
    from pa_host import automation
    from test_automation import CollectorFactory, live_document, wait_finished

    for address in ("board-a", "board-b"):
        board_app.ble_connections.connect(address)
    factory = CollectorFactory()

    def spawn(command, **kwargs):
        address = command[command.index("--address") + 1]
        assert board_app.ble_connections.snapshot()[address]["state"] == "disconnected"
        return factory(command, **kwargs)

    controller = board_app.automation
    controller._process_factory = spawn
    document = live_document(automation)
    controller.start(document, "live", automation.compile_experiment(document)["plan_hash"], "dual-handoff")
    assert wait_finished(controller)["state"] == "completed"
    assert len(factory.children) == 2
    assert not factory.gate_violation
    assert all(not client.is_connected for client in FakeClient.instances)
