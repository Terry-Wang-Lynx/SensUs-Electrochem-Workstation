import hashlib
import io
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pa_host.app_update import AppUpdateError, AppUpdateManager, _safe_zip_extract


def _release(version: str, package: bytes) -> dict[str, object]:
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [{
            "name": f"SensUs-Workstation-Windows-x64-{version}.zip",
            "size": len(package),
            "digest": f"sha256:{hashlib.sha256(package).hexdigest()}",
            "browser_download_url": (
                "https://github.com/Terry-Wang-Lynx/"
                "SensUs-Electrochem-Workstation/releases/download/"
                f"v{version}/SensUs-Workstation-Windows-x64-{version}.zip"
            ),
        }],
    }


def _windows_package() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SensUsBackend.exe", b"exe")
        archive.writestr(
            "workstation/PORTABLE_RESOURCES.txt", b"portable resources"
        )
    return output.getvalue()


class _Download(io.BytesIO):
    def geturl(self) -> str:
        return "https://release-assets.githubusercontent.com/download/update.zip"

    def __enter__(self) -> "_Download":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def test_source_build_never_advertises_self_update(tmp_path: Path) -> None:
    manager = AppUpdateManager("0.4.6", tmp_path, frozen=False)

    assert manager.status(trigger_check=True) == {
        "supported": False,
        "package_kind": "source",
        "current_version": "0.4.6",
        "latest_version": "",
        "available": False,
        "state": "idle",
        "progress": 0.0,
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "error": "",
        "last_checked_at": None,
    }


def test_release_check_requires_exact_platform_asset_and_digest(tmp_path: Path) -> None:
    package = _windows_package()
    target = tmp_path / "installed"
    target.mkdir()
    manager = AppUpdateManager(
        "0.4.6", tmp_path / "state", package_kind="windows-x64",
        target_path=target, app_pid=123,
    )

    with patch.object(manager, "_read_json", return_value=_release("0.4.7", package)):
        result = manager.check_once()

    assert result["available"] is True
    assert result["latest_version"] == "0.4.7"
    assert result["state"] == "available"

    invalid = _release("0.4.8", package)
    invalid["assets"][0]["digest"] = ""  # type: ignore[index]
    with patch.object(manager, "_read_json", return_value=invalid), pytest.raises(
        AppUpdateError, match="SHA-256"
    ):
        manager.check_once()


def test_macos_release_check_selects_the_dmg_and_hides_current_version(
    tmp_path: Path,
) -> None:
    package = b"dmg"
    target = tmp_path / "SensUs Workstation.app"
    target.mkdir()
    manager = AppUpdateManager(
        "0.4.6", tmp_path / "state", package_kind="macos-arm64",
        target_path=target, app_pid=123,
    )
    release = {
        "tag_name": "v0.4.6", "draft": False, "prerelease": False,
        "assets": [{
            "name": "SensUs-Workstation-macOS-arm64-0.4.6.dmg",
            "size": len(package),
            "digest": f"sha256:{hashlib.sha256(package).hexdigest()}",
            "browser_download_url": (
                "https://github.com/Terry-Wang-Lynx/"
                "SensUs-Electrochem-Workstation/releases/download/v0.4.6/"
                "SensUs-Workstation-macOS-arm64-0.4.6.dmg"
            ),
        }],
    }

    with patch.object(manager, "_read_json", return_value=release):
        result = manager.check_once()

    assert result["available"] is False
    assert result["state"] == "idle"


def test_windows_update_download_is_verified_and_staged(tmp_path: Path) -> None:
    package = _windows_package()
    target = tmp_path / "installed"
    target.mkdir()
    manager = AppUpdateManager(
        "0.4.6", tmp_path / "state", package_kind="windows-x64",
        target_path=target, app_pid=123,
    )
    with patch.object(manager, "_read_json", return_value=_release("0.4.7", package)):
        manager.check_once()

    with patch("pa_host.app_update.urllib.request.urlopen",
               return_value=_Download(package)):
        manager.start_download()
        assert manager._worker is not None
        manager._worker.join(timeout=5)

    status = manager.status()
    assert status["state"] == "ready"
    assert status["progress"] == 1
    assert status["downloaded_bytes"] == len(package)
    assert manager._staged_path is not None
    assert (manager._staged_path / "SensUsBackend.exe").is_file()


def test_windows_zip_path_traversal_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../outside.txt", b"no")

    with pytest.raises(AppUpdateError, match="不安全"):
        _safe_zip_extract(archive, tmp_path / "extract")
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize(
    ("package_kind", "target_name", "helper_name", "launcher"),
    [
        ("macos-arm64", "SensUs Workstation.app", "install-update.zsh", "/bin/zsh"),
        ("windows-x64", "SensUs-Workstation", "install-update.ps1", "powershell.exe"),
    ],
)
def test_install_handoff_uses_external_helper_after_process_exit(
    tmp_path: Path, package_kind: str, target_name: str,
    helper_name: str, launcher: str,
) -> None:
    target = tmp_path / target_name
    target.mkdir()
    staged = tmp_path / "staged"
    staged.mkdir()
    manager = AppUpdateManager(
        "0.4.6", tmp_path / "state", package_kind=package_kind,
        target_path=target, app_pid=123,
    )
    manager._state = "ready"
    manager._available = True
    manager._latest_version = "0.4.7"
    manager._staged_path = staged

    with patch("pa_host.app_update.subprocess.Popen") as launch:
        result = manager.begin_install()

    assert result["state"] == "applying"
    command = launch.call_args.args[0]
    assert command[0] == launcher
    helper = manager.root / helper_name
    assert helper.is_file()
    helper_text = helper.read_text(encoding="utf-8-sig")
    assert "previous" in helper_text
    assert "/api/health" in helper_text
    assert "123" in command
    assert str(os.getpid()) in command
    assert "0.4.7" in command


def test_macos_update_rejects_running_from_downloaded_disk_image(
    tmp_path: Path,
) -> None:
    target = Path("/Volumes/SensUs Workstation/SensUs Workstation.app")
    manager = AppUpdateManager(
        "0.4.6", tmp_path / "state", package_kind="macos-arm64",
        target_path=target, app_pid=123,
    )
    manager._state = "ready"
    manager._staged_path = tmp_path

    with patch.object(Path, "exists", return_value=True), pytest.raises(
        AppUpdateError, match="应用程序"
    ):
        manager.begin_install()
