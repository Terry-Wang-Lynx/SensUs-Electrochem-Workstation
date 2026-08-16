import json
import zipfile
from http import HTTPStatus
from io import BytesIO
from unittest.mock import Mock, patch

import pytest

from pa_host.diagnostics import DiagnosticStore
from pa_host.gui_server import (
    DiagnosticHTTPServer,
    RequestHandler,
    _devices_payload_from_devices,
)


def test_diagnostic_store_rotates_redacts_and_never_leaks_secrets(tmp_path) -> None:
    store = DiagnosticStore(tmp_path / "logs", max_bytes=1024, backup_count=2)
    for index in range(30):
        store.record(
            "info", "test.event", f"event {index}",
            token="do-not-log", detail="x" * 100,
        )

    assert store.log_path.exists()
    assert store.log_path.with_name("workstation.jsonl.1").exists()
    content = b"".join(path.read_bytes() for path in store.log_files()).decode()
    assert "do-not-log" not in content
    assert "[redacted]" in content
    assert store.snapshot()["events"][-1]["context"]["token"] == "[redacted]"


def test_diagnostic_store_records_traceback_and_builds_bounded_bundle(tmp_path) -> None:
    store = DiagnosticStore(tmp_path / "logs")
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        event_id = store.exception("test.failure", "operation failed", exc)

    collector = tmp_path / "collector.log"
    collector.write_text("collector detail\n", encoding="utf-8")
    bundle = store.bundle(
        context={"transport": "serial"},
        extra_files=[("collector.log", collector)],
    )
    with zipfile.ZipFile(BytesIO(bundle)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("diagnostics.json"))
        assert "current-run/collector.log" in names
        assert any(name.startswith("logs/workstation.jsonl") for name in names)
        assert manifest["context"]["transport"] == "serial"
        event = manifest["events"][-1]
        assert event["event_id"] == event_id
        assert "RuntimeError: boom" in event["context"]["traceback"]


def test_diagnostic_write_failure_is_reported_without_raising(tmp_path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file", encoding="utf-8")
    store = DiagnosticStore(blocked)

    event_id = store.record("error", "write.failure", "still retained")

    assert event_id
    snapshot = store.snapshot()
    assert snapshot["events"][-1]["message"] == "still retained"
    assert snapshot["write_error"]


def test_http_error_response_gets_a_diagnostic_id(tmp_path) -> None:
    store = DiagnosticStore(tmp_path / "logs")
    handler = RequestHandler.__new__(RequestHandler)
    handler.path = "/api/example"
    handler.command = "POST"
    handler._request_id = "request-1"
    handler._request_body_keys = ["value"]
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = BytesIO()

    with patch("pa_host.gui_server.DIAGNOSTICS", store):
        handler._send_json({"error": "bad input"}, HTTPStatus.BAD_REQUEST)

    payload = json.loads(handler.wfile.getvalue())
    assert payload["error"] == "bad input"
    assert payload["diagnostic_id"].startswith("D-")
    event = store.snapshot()["events"][-1]
    assert event["event"] == "api.request.error"
    assert event["context"]["request_id"] == "request-1"


def test_existing_operation_diagnostic_id_is_reused_without_duplicate_event(
    tmp_path,
) -> None:
    store = DiagnosticStore(tmp_path / "logs")
    operation_id = store.record("error", "operation.failed", "deep failure")
    handler = RequestHandler.__new__(RequestHandler)
    handler.path = "/api/example"
    handler.command = "POST"
    handler._request_id = "request-2"
    handler._request_body_keys = []
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.wfile = BytesIO()

    with patch("pa_host.gui_server.DIAGNOSTICS", store):
        handler._send_json(
            {"error": "operation failed", "diagnostic_id": operation_id},
            HTTPStatus.CONFLICT,
        )

    payload = json.loads(handler.wfile.getvalue())
    assert payload["diagnostic_id"] == operation_id
    assert len(store.snapshot()["events"]) == 1


def test_unhandled_api_exception_is_logged_and_returned_as_500(tmp_path) -> None:
    store = DiagnosticStore(tmp_path / "logs")
    handler = RequestHandler.__new__(RequestHandler)
    handler.path = "/api/explode"
    handler.command = "POST"
    handler._do_POST = Mock(side_effect=ZeroDivisionError("boom"))
    handler._send_json = Mock()

    with patch("pa_host.gui_server.DIAGNOSTICS", store):
        handler.do_POST()

    payload, status = handler._send_json.call_args.args
    assert status == HTTPStatus.INTERNAL_SERVER_ERROR
    assert payload["diagnostic_id"].startswith("D-")
    assert "未预期错误" in payload["error"]
    events = store.snapshot()["events"]
    assert events[-1]["event"] == "api.request.unhandled"
    assert "ZeroDivisionError: boom" in events[-1]["context"]["traceback"]


def test_device_discovery_error_is_not_dropped_from_payload() -> None:
    payload = _devices_payload_from_devices([], error="probe failed")

    assert payload["error"] == "probe failed"


def test_http_worker_connection_reset_is_logged_without_default_traceback(
    tmp_path,
) -> None:
    store = DiagnosticStore(tmp_path / "logs")
    server = DiagnosticHTTPServer.__new__(DiagnosticHTTPServer)

    with patch("pa_host.gui_server.DIAGNOSTICS", store):
        try:
            raise ConnectionResetError("client went away")
        except ConnectionResetError:
            server.handle_error(None, ("127.0.0.1", 12345))

    event = store.snapshot()["events"][-1]
    assert event["event"] == "http.client.disconnected"
    assert event["level"] == "info"
    assert event["context"]["client_address"] == ["127.0.0.1", 12345]


def test_http_worker_unhandled_exception_keeps_traceback(tmp_path) -> None:
    store = DiagnosticStore(tmp_path / "logs")
    server = DiagnosticHTTPServer.__new__(DiagnosticHTTPServer)

    with patch("pa_host.gui_server.DIAGNOSTICS", store):
        try:
            raise LookupError("unexpected worker failure")
        except LookupError:
            server.handle_error(None, ("127.0.0.1", 12345))

    event = store.snapshot()["events"][-1]
    assert event["event"] == "http.request.unhandled"
    assert "LookupError: unexpected worker failure" in event["context"]["traceback"]


def test_post_broken_pipe_is_not_reclassified_as_bad_request() -> None:
    handler = RequestHandler.__new__(RequestHandler)
    handler.path = "/api/not-found"
    handler._body = Mock(return_value={})
    handler._send_json = Mock(side_effect=BrokenPipeError("client left"))

    with pytest.raises(BrokenPipeError, match="client left"):
        handler._do_POST()

    handler._send_json.assert_called_once_with(
        {"error": "not found"}, HTTPStatus.NOT_FOUND
    )


def test_known_operation_failure_keeps_its_diagnostic_id() -> None:
    operation_error = RuntimeError("hardware verification failed")
    operation_error.diagnostic_id = "D-operation"
    app = Mock()
    app.predict.side_effect = operation_error
    handler = RequestHandler.__new__(RequestHandler)
    handler.path = "/api/predict"
    handler._body = Mock(return_value={})
    handler._send_json = Mock()

    with patch("pa_host.gui_server.APP", app):
        handler._do_POST()

    handler._send_json.assert_called_once_with(
        {
            "error": "hardware verification failed",
            "diagnostic_id": "D-operation",
        },
        HTTPStatus.CONFLICT,
    )
