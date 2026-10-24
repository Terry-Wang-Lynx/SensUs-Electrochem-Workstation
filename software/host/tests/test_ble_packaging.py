import plistlib
from pathlib import Path
from unittest.mock import patch

from pa_host import __version__, runtime


ROOT = Path(__file__).resolve().parents[3]


def test_v050_version_metadata_agrees():
    assert __version__ == "0.5.0"
    assert 'version = "0.5.0"' in (ROOT / "pyproject.toml").read_text()
    info = plistlib.loads((ROOT / "macos/Info.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == "0.5.0"
    assert info["CFBundleVersion"] == "0.5.0"
    assert info["NSBluetoothAlwaysUsageDescription"]


def test_ble_frozen_collector_dispatch_and_dependency():
    with patch.object(runtime, "is_frozen", return_value=True):
        assert runtime.module_command("pa_host.collect_ble", "--address", "device")[1:] == [
            "collect-ble", "--address", "device"]
    source = (ROOT / "packaging/portable_entry.py").read_text()
    assert '"collect-ble"' in source
    assert "from pa_host.collect_ble import main" in source
    spec = (ROOT / "packaging/portable.spec").read_text()
    assert '"pa_host.collect_ble"' in spec
    assert 'collect_submodules("bleak")' in spec
    assert '"bleak>=1.0,<4"' in (ROOT / "pyproject.toml").read_text()


def test_automation_source_distribution_includes_agent_examples_and_local_assets():
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "recursive-include docs/examples *.json" in manifest
    assert "recursive-include software/host/pa_host/gui *.html *.js *.css *.png *.json *.txt" in manifest
    assert "recursive-include software/host/tests/fixtures *.json" in manifest


def test_portable_includes_guided_template_and_calibration_service():
    spec = (ROOT / "packaging/portable.spec").read_text()
    assert '"pa_host.guided_automation"' in spec


def test_portable_includes_independent_absolute_limit_service():
    spec = (ROOT / "packaging/portable.spec").read_text()
    assert '"pa_host.motion_limits"' in spec


def test_portable_includes_feedback_status_and_twin_services():
    spec = (ROOT / "packaging/portable.spec").read_text()
    assert '"pa_host.automation_devices"' in spec
    assert '"pa_host.digital_twin"' in spec
