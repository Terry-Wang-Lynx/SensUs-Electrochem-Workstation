import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_selfcheck_uses_real_scipy(capsys):
    import json

    assert load_script("portable_entry").main(["verify-runtime"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scipy_welch"] is True
    assert report["protocol"] == "v52-0.1"
    assert report["endpoint_phases"] == [[9.0, 2.0], [10.0, 0.5]]


def test_clean_environment_discards_developer_settings(tmp_path):
    module = load_script("smoke_portable")
    result = module.clean_environment(tmp_path, tmp_path / "resources", {
        "PATH": "original", "PYTHONPATH": "bad", "PYTHONHOME": "bad",
        "VIRTUAL_ENV": "bad", "SENSUS_SERVER_PORT": "8870", "SENSUS_FAULT": "bad",
    })
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "SENSUS_FAULT", "SENSUS_SERVER_PORT"} & result.keys()
    assert result["SENSUS_STATE_DIR"] == str(tmp_path / "state")


def test_manifest_verification_rejects_tampering(tmp_path):
    import hashlib
    import json
    import pytest

    module = load_script("smoke_portable")
    raw = tmp_path / "raw-samples.jsonl"
    raw.write_text(json.dumps({"synthetic": True, "column": 3, "visit_index": 1}) + "\n")
    manifest = {"state": "completed", "mode": "simulate", "synthetic": True,
                "progress": {"percent": 100}, "visits": [{"column": 3, "visit_index": 1}],
                "files": {raw.name: hashlib.sha256(raw.read_bytes()).hexdigest()}}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    module.verify_manifest(path, [3])
    raw.write_text("tampered")
    with pytest.raises(AssertionError, match="checksum"):
        module.verify_manifest(path, [3])


def test_failure_is_reported_and_returns_nonzero(tmp_path):
    import json

    module = load_script("smoke_portable")
    report = tmp_path / "report.json"
    assert module.main(["--backend", str(tmp_path / "missing"), "--resources", str(tmp_path),
                        "--report", str(report)]) == 1
    saved = json.loads(report.read_text())
    assert saved["ok"] is False
    assert "backend/resources missing" in saved["error"]


def test_runtime_selfcheck_rejects_arguments():
    import pytest

    with pytest.raises(ValueError, match="accepts no arguments"):
        load_script("portable_entry").main(["verify-runtime", "arbitrary-code"])


def test_smoke_decodes_all_cli_output_as_utf8_independent_of_locale(tmp_path, monkeypatch):
    import locale
    from types import SimpleNamespace
    import pytest

    module = load_script("smoke_portable")
    backend = tmp_path / "backend"
    backend.touch()
    commands = []

    class RuntimeChecked(Exception):
        pass

    def run(command, **kwargs):
        commands.append(command[1])
        assert kwargs.get("encoding") == "utf-8"
        decoded = "usage: 离线分析".encode("utf-8").decode(kwargs["encoding"])
        assert "离线分析" in decoded
        if command[1] == "verify-runtime":
            raise RuntimeChecked
        return SimpleNamespace(returncode=0, stdout=decoded, stderr="")

    monkeypatch.setattr(locale, "getpreferredencoding", lambda *args: "cp1252")
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(RuntimeChecked):
        module.smoke(backend, tmp_path, "0.5.0", {})
    assert commands == ["collect", "collect-ble", "analyze", "verify-runtime"]
