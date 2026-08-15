import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from pa_host import frontend_update
from pa_host.frontend_update import FrontendUpdateError, FrontendUpdater


def _frontend(path: Path, marker: str = "ok") -> None:
    path.mkdir(parents=True)
    for name in frontend_update.REQUIRED_FILES:
        (path / name).write_text(f"{name}:{marker}", encoding="utf-8")


def _archive(marker: str = "update") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name in frontend_update.REQUIRED_FILES:
            bundle.writestr(name, f"{name}:{marker}")
    return output.getvalue()


def test_pending_frontend_activates_only_on_restart(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    _frontend(bundled, "bundled")
    updater = FrontendUpdater(bundled, tmp_path / "state", tmp_path / "resources")
    staged = updater._version_dir("1.2.3")
    _frontend(staged, "new")
    updater._save_state({"pending_version": "1.2.3"})

    assert updater.prepare_startup() == staged
    state = updater._load_state()
    assert state["active_version"] == "1.2.3"
    assert state["boot_pending"] == "1.2.3"

    updater.mark_ready()
    assert updater._load_state()["boot_pending"] == ""


def test_unconfirmed_frontend_rolls_back_on_next_start(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    _frontend(bundled)
    updater = FrontendUpdater(bundled, tmp_path / "state", tmp_path / "resources")
    previous = updater._version_dir("1.0.0")
    pending = updater._version_dir("1.1.0")
    _frontend(previous, "previous")
    _frontend(pending, "pending")
    updater._save_state({"active_version": "1.0.0", "pending_version": "1.1.0"})

    assert updater.prepare_startup() == pending
    assert updater.prepare_startup() == previous
    assert updater._load_state()["failed_version"] == "1.1.0"


def test_update_download_is_deferred_and_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "bundled"
    _frontend(bundled)
    archive = _archive()
    manifest = {
        "schema_version": 1,
        "channel": "stable",
        "version": "2.0.0",
        "api_major": frontend_update.BACKEND_API_MAJOR,
        "zip_url": "https://example.invalid/frontend.zip",
        "sha256": hashlib.sha256(archive).hexdigest(),
        "signature": "ignored-by-test",
    }
    updater = FrontendUpdater(
        bundled,
        tmp_path / "state",
        tmp_path / "resources",
        manifest_url="https://example.invalid/stable.json",
        public_key="00" * 32,
    )
    monkeypatch.setattr(frontend_update, "_verify_signature", lambda *_: None)
    monkeypatch.setattr(
        updater,
        "_read_url",
        lambda url, _maximum, *_args: (
            json.dumps(manifest).encode("utf-8") if url.endswith("stable.json") else archive
        ),
    )

    assert updater.check_once(lambda: False) is False
    assert updater.check_once(lambda: True) is True
    assert updater._load_state()["pending_version"] == "2.0.0"
    assert updater.prepare_startup() == updater._version_dir("2.0.0")


def test_archive_rejects_parent_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "bad")
    with pytest.raises(FrontendUpdateError, match="unsafe path"):
        frontend_update._extract_archive(archive, tmp_path / "extract")
    assert not (tmp_path / "outside.txt").exists()
