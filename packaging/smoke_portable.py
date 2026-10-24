#!/usr/bin/env python3
"""Verify an installed frozen backend using isolated, synthetic experiments."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid


API = "/api/automation/v1/"


def clean_environment(root, resources, inherited=None):
    environment = {key: value for key, value in (os.environ if inherited is None else inherited).items()
                   if not key.startswith("SENSUS_") and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
    if sys.platform == "darwin":
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    environment.update(SENSUS_PROJECT_DIR=str(resources), SENSUS_RESOURCE_DIR=str(resources),
                       SENSUS_STATE_DIR=str(root / "state"), SENSUS_LOG_DIR=str(root / "logs"),
                       SENSUS_MEASUREMENTS_DIR=str(root / "measurements"))
    return environment


def request(base, path, payload=None, *, text=False):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    query = urllib.request.Request(base + path, data=body,
                                   headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(query, timeout=5) as response:
        content = response.read().decode("utf-8")
    return content if text else json.loads(content)


def verify_manifest(path, route):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["state"] == "completed" and manifest["mode"] == "simulate"
    assert manifest["synthetic"] is True and manifest["progress"]["percent"] == 100
    visits = manifest["visits"]
    assert [visit["column"] for visit in visits] == route, "manifest visit order"
    assert len({visit["visit_index"] for visit in visits}) == len(route), "unique visits"
    assert manifest["files"], "empty manifest"
    for relative, expected in manifest["files"].items():
        artifact = (path.parent / relative).resolve()
        assert artifact.is_relative_to(path.parent.resolve()), "manifest path escapes run"
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == expected, f"checksum: {relative}"
    raw_path = path.parent / "raw-samples.jsonl"
    assert raw_path.name in manifest["files"], "raw samples absent from manifest"
    samples = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert samples and all(sample["synthetic"] is True for sample in samples), "non-synthetic samples"
    assert {sample["visit_index"] for sample in samples} == {visit["visit_index"] for visit in visits}
    columns = {visit["visit_index"]: visit["column"] for visit in visits}
    assert all(sample["column"] == columns[sample["visit_index"]] for sample in samples)
    return {"manifest": manifest, "raw_sample_count": len(samples)}


def smoke(backend, resources, expected_version, report):
    backend, resources = backend.resolve(), resources.resolve()
    assert backend.is_file() and resources.is_dir(), "backend/resources missing"
    with tempfile.TemporaryDirectory(prefix="sensus-frozen-smoke-") as temporary:
        root = Path(temporary)
        environment = clean_environment(root, resources)
        for command in ("collect", "collect-ble", "analyze"):
            completed = subprocess.run([str(backend), command, "--help"], cwd=root, env=environment,
                                       capture_output=True, text=True, encoding="utf-8", timeout=90)
            assert completed.returncode == 0, f"{command} help: {completed.stderr}"
            assert "usage:" in completed.stdout.lower(), f"{command} help missing"
        runtime = subprocess.run([str(backend), "verify-runtime"], cwd=root, env=environment,
                                 capture_output=True, text=True, encoding="utf-8", timeout=90)
        assert runtime.returncode == 0, f"runtime selfcheck: {runtime.stderr}"
        report["runtime"] = json.loads(runtime.stdout)
        assert report["runtime"]["version"] == expected_version
        assert report["runtime"]["scipy_welch"] is True
        assert report["runtime"]["protocol"] == "v52-0.1"
        assert report["runtime"]["endpoint_phases"] == [[9.0, 2.0], [10.0, 0.5]]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        report["port"] = port
        with (root / "backend.log").open("w", encoding="utf-8") as output:
            process = subprocess.Popen([str(backend), "gui", "--host", "127.0.0.1", "--port", str(port),
                                        "--transport", "ble"], cwd=root, env=environment,
                                       stdout=output, stderr=subprocess.STDOUT)
            ready = False
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    assert process.poll() is None, "backend exited before health"
                    try:
                        health = request(base, "/api/health")
                        ready = True
                        break
                    except OSError:
                        time.sleep(0.2)
                assert ready, "backend cold start exceeded 90 seconds"
                assert health["version"] == expected_version and health["ok"] is True
                assert Path(health["project"]).resolve() == resources
                report["health"] = health
                capabilities = request(base, API + "capabilities")
                assert capabilities["guided_execution"]["preserve_order_and_duplicates"] is True
                assert capabilities["guided_execution"]["modes"] == ["once", "repeat", "custom"]
                assert capabilities["automatic_motion_bus"]["endpoint_approach"]["revision"] == "20261024-endpoint-approach-r1"
                report["capabilities"] = capabilities
                html = request(base, "/", text=True)
                assert "<html" in html.lower()
                cad = request(base, "/assets/ht-apparatus.json")
                assert isinstance(cad, dict) and cad, "CAD JSON missing"
                javascript = request(base, "/assets/high_throughput.js", text=True)
                assert "endpoint-approach-r1" in javascript, "frontend endpoint revision missing"
                report["frontend"] = request(base, "/api/frontend")
                initial_state = request(base, API + "state")
                assert not initial_state.get("error") and not initial_state["motion"].get("connected")
                assert not initial_state["motion"].get("faulted")
                assert not initial_state["motion"].get("serial_opened_once")
                saved = request(base, API + "guided")["document"]
                report["runs"] = []
                for mode, count, route, expected in (("custom", 1, [3, 1, 3], [3, 1, 3]),
                                                      ("repeat", 3, None, [1, 3, 1, 3, 1, 3])):
                    document = copy.deepcopy(saved)
                    document.update(schema_version="guided-1.1", output_dir=str(root / "measurements"),
                                    execution={"mode": mode, "count": count, "route": route})
                    document["defaults"]["dwell_s"] = 5
                    for column in document["columns"]:
                        column.update(enabled=column["index"] in (1, 3), duration_s=5, windows={})
                    assert all(not board.get("address") for board in document["bindings"]["boards"].values())
                    assert not document["bindings"]["motion"]["port"]
                    payload = {"document": document, "mode": "simulate"}
                    preview = request(base, API + "guided/preview", payload)
                    dwells = [step for step in preview["steps"] if step["op"] == "dwell"]
                    assert [step["column"] for step in dwells] == expected
                    assert all(step["resolved_params"]["seconds"] == 5 for step in dwells)
                    assert len({step["visit_index"] for step in dwells}) == len(expected)
                    request(base, API + "guided/run", {**payload, "plan_hash": preview["plan_hash"],
                                                      "idempotency_key": str(uuid.uuid4())})
                    deadline = time.monotonic() + 90
                    while True:
                        state = request(base, API + "state")
                        if state["state"] in {"completed", "error", "stopped"}:
                            break
                        assert time.monotonic() < deadline, "simulation exceeded 90 seconds"
                        time.sleep(0.1)
                    assert state["state"] == "completed", str(state.get("error"))
                    assert state["progress"]["percent"] == 100
                    assert not state["motion"].get("connected"), "simulation connected motion"
                    assert not state["motion"].get("serial_opened_once"), "simulation opened serial"
                    assert not state["motion"].get("faulted"), "simulation faulted motion"
                    manifest_path = Path(state["manifest_path"])
                    deadline = time.monotonic() + 5
                    while not manifest_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    report["runs"].append(verify_manifest(manifest_path, expected))
                    assert request(base, API + "guided")["document"] == saved, "saved default changed"
            finally:
                if process.poll() is None:
                    if ready:
                        try:
                            request(base, "/api/shutdown", {})
                            process.wait(timeout=25)
                        except Exception as error:
                            report["shutdown_error"] = str(error)
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                        report["forced_cleanup"] = True
                report["backend_exit_code"] = process.returncode
                report["backend_log"] = (root / "backend.log").read_text(encoding="utf-8", errors="replace")
                assert not report.get("shutdown_error") and not report.get("forced_cleanup"), "backend did not shut down cleanly"
                assert process.returncode == 0, "backend exit failed"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--expected-version", default="0.5.0")
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = {"ok": False, "hardware_operations": False}
    try:
        smoke(arguments.backend, arguments.resources, arguments.expected_version, report)
        report["ok"] = True
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(arguments.report), "error": report.get("error")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
