"""Signed-by-release-digest whole-application updates for portable builds."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlparse


LATEST_RELEASE_API = (
    "https://api.github.com/repos/Terry-Wang-Lynx/"
    "SensUs-Electrochem-Workstation/releases/latest"
)
CHECK_TTL_S = 6 * 60 * 60

# 🔴 冻结体(PyInstaller)里没有 CA 证书:包内既没有 certifi,也没有 cacert.pem,
# 而 OpenSSL 的默认 CA 路径是构建机上的路径,到用户机器上并不存在。
# 于是 ssl.create_default_context() 建出来的上下文**信任库是空的**,
# 每次更新检查都以
#   SSL: CERTIFICATE_VERIFY_FAILED - unable to get local issuer certificate
# 失败。2026-08-21 现场实测:某台机器上 156 次 app_update.* 事件**全是 check_failed,
# 成功事件 0 条**,持续三天;用户因此一直停在旧版本,而软件从未提示过有新版。
# 排查时已用四项证据排除网络因素(curl 通、证书签发者是 Sectigo 无中间人、无代理、
# 系统 python3 访问同一 URL 正常),确认是我们自己的缺陷。
#
# 判据用 cert_store_stats():信任库为空才回落,所以在 CA 本来就正常的平台
# (例如 Windows 会从系统证书库加载)上**不改变任何行为**。
_CA_FALLBACKS = (
    "/etc/ssl/cert.pem",            # macOS 自带,128 张根证书
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


def _ssl_context() -> ssl.SSLContext:
    """带 CA 回落的默认上下文;绝不降级为不校验。"""
    context = ssl.create_default_context()
    try:
        if context.cert_store_stats().get("x509_ca", 0) > 0:
            return context
    except (AttributeError, OSError):
        return context
    candidates: list[str] = []
    env_file = os.environ.get("SSL_CERT_FILE")
    if env_file:
        candidates.append(env_file)
    try:
        import certifi  # noqa: PLC0415 — 可选依赖,装了就用
        candidates.append(certifi.where())
    except Exception:  # noqa: BLE001 — 没装 certifi 是正常情况
        pass
    candidates.extend(_CA_FALLBACKS)
    for candidate in candidates:
        try:
            if candidate and Path(candidate).is_file():
                context.load_verify_locations(cafile=candidate)
                if context.cert_store_stats().get("x509_ca", 0) > 0:
                    return context
        except (OSError, ssl.SSLError):
            continue
    # 一个都没找到:照原样返回。校验仍会失败,但**失败比静默不校验安全**,
    # 且调用方会把错误上报成可见事件。
    return context

MAX_RELEASE_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 700 * 1024 * 1024
_ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class AppUpdateError(RuntimeError):
    """An application update was rejected without changing the installed app."""


def _version_key(value: object) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(value).strip())
    if match is None:
        raise AppUpdateError("发布版本号格式不受支持")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _release_version(value: object) -> str:
    version = str(value).strip().removeprefix("v")
    _version_key(version)
    return version


def _safe_zip_extract(archive: Path, destination: Path) -> None:
    total_size = 0
    with zipfile.ZipFile(archive) as handle:
        members = handle.infolist()
        for info in members:
            path = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            total_size += max(0, info.file_size)
            if (
                not info.filename
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in info.filename
                or (path.parts and ":" in path.parts[0])
                or stat.S_ISLNK(mode)
                or total_size > 2 * MAX_ASSET_BYTES
            ):
                raise AppUpdateError("Windows 更新包结构不安全")
        handle.extractall(destination)


class AppUpdateManager:
    """Check, stage, and hand off a portable app update to an external helper."""

    def __init__(
        self,
        current_version: str,
        state_root: Path,
        *,
        package_kind: str | None = None,
        target_path: Path | None = None,
        app_pid: int | None = None,
        server_port: int | None = None,
        frozen: bool | None = None,
        event_callback: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.current_version = _release_version(current_version)
        self.root = Path(state_root) / "app-update"
        self.package_kind, self.target_path = self._detect_package(
            package_kind, target_path, frozen
        )
        raw_app_pid = str(os.environ.get("SENSUS_APP_PID", "")).strip()
        try:
            detected_pid = int(raw_app_pid) if raw_app_pid else 0
        except ValueError:
            detected_pid = 0
        self.app_pid = int(app_pid or detected_pid or 0)
        raw_server_port = str(os.environ.get("SENSUS_SERVER_PORT", "")).strip()
        try:
            detected_server_port = int(raw_server_port) if raw_server_port else 0
        except ValueError:
            detected_server_port = 0
        selected_server_port = int(server_port or detected_server_port or 8765)
        self.server_port = (
            selected_server_port if 1 <= selected_server_port <= 65535 else 8765
        )
        self._event_callback = event_callback
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._state = "idle"
        self._latest_version = ""
        self._release: dict[str, Any] | None = None
        self._available = False
        self._progress = 0.0
        self._downloaded_bytes = 0
        self._total_bytes = 0
        self._error = ""
        self._last_checked_at = 0.0
        self._staged_path: Path | None = None

    @staticmethod
    def _detect_package(
        package_kind: str | None,
        target_path: Path | None,
        frozen: bool | None,
    ) -> tuple[str, Path | None]:
        if package_kind is not None:
            return package_kind, Path(target_path).resolve() if target_path else None
        is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        if not is_frozen:
            return "source", None
        machine = platform.machine().lower()
        if sys.platform == "darwin" and machine in {"arm64", "aarch64"}:
            configured = os.environ.get("SENSUS_APP_BUNDLE", "").strip()
            return "macos-arm64", Path(configured).resolve() if configured else None
        if sys.platform == "win32" and machine in {"amd64", "x86_64"}:
            configured = os.environ.get("SENSUS_APP_ROOT", "").strip()
            return "windows-x64", Path(configured).resolve() if configured else None
        return "unsupported", None

    @property
    def supported(self) -> bool:
        return bool(
            self.package_kind in {"macos-arm64", "windows-x64"}
            and self.target_path is not None
            and self.app_pid > 0
        )

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._state in {"downloading", "preparing", "applying"}

    def _event(self, event: str, message: str, **context: Any) -> None:
        callback = self._event_callback
        if callback is not None:
            try:
                callback(event, message, context)
            except Exception:
                pass

    def status(self, *, trigger_check: bool = False) -> dict[str, Any]:
        if trigger_check:
            self.ensure_check()
        with self._lock:
            return {
                "supported": self.supported,
                "package_kind": self.package_kind,
                "current_version": self.current_version,
                "latest_version": self._latest_version,
                "available": bool(self.supported and self._available),
                "state": self._state,
                "progress": round(self._progress, 4),
                "downloaded_bytes": self._downloaded_bytes,
                "total_bytes": self._total_bytes,
                "error": self._error,
                "last_checked_at": self._last_checked_at or None,
            }

    def ensure_check(self, *, force: bool = False) -> bool:
        if not self.supported or self._stop.is_set():
            return False
        with self._lock:
            if self._state in {"checking", "downloading", "preparing", "applying"}:
                return False
            retry_after = 15 * 60 if self._state == "error" else CHECK_TTL_S
            if not force and time.time() - self._last_checked_at < retry_after:
                return False
            self._state = "checking"
            self._error = ""
            worker = threading.Thread(
                target=self._check_worker, name="app-update-check", daemon=True
            )
            self._worker = worker
            worker.start()
        return True

    @staticmethod
    def _read_json(url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "SensUs-Workstation-App-Updater/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(
            request, timeout=15, context=_ssl_context()
        ) as response:
            data = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
        if len(data) > MAX_RELEASE_RESPONSE_BYTES:
            raise AppUpdateError("更新信息响应过大")
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppUpdateError("更新信息格式错误") from exc
        if not isinstance(payload, dict):
            raise AppUpdateError("更新信息格式错误")
        return payload

    def _expected_asset_name(self, version: str) -> str:
        if self.package_kind == "macos-arm64":
            return f"SensUs-Workstation-macOS-arm64-{version}.dmg"
        if self.package_kind == "windows-x64":
            return f"SensUs-Workstation-Windows-x64-{version}.zip"
        raise AppUpdateError("当前平台不支持整包更新")

    def check_once(self) -> dict[str, Any]:
        release = self._read_json(LATEST_RELEASE_API)
        if release.get("draft") or release.get("prerelease"):
            raise AppUpdateError("最新发布不是稳定版")
        version = _release_version(release.get("tag_name", ""))
        expected_name = self._expected_asset_name(version)
        assets = release.get("assets")
        if not isinstance(assets, list):
            raise AppUpdateError("发布页缺少安装包列表")
        asset = next(
            (item for item in assets
             if isinstance(item, dict) and item.get("name") == expected_name),
            None,
        )
        available = _version_key(version) > _version_key(self.current_version)
        if available and asset is None:
            raise AppUpdateError(f"新版本缺少当前平台安装包：{expected_name}")
        if asset is not None:
            digest = str(asset.get("digest") or "").lower()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise AppUpdateError("新版本安装包缺少 SHA-256 校验值")
            download_url = str(asset.get("browser_download_url") or "")
            parsed = urlparse(download_url)
            if parsed.scheme != "https" or parsed.hostname != "github.com":
                raise AppUpdateError("新版本下载地址不可信")
            size = int(asset.get("size") or 0)
            if size <= 0 or size > MAX_ASSET_BYTES:
                raise AppUpdateError("新版本安装包大小异常")
        with self._lock:
            self._latest_version = version
            self._release = dict(asset) if available and asset is not None else None
            self._available = available
            self._last_checked_at = time.time()
            self._state = "available" if available else "idle"
            self._error = ""
        return self.status()

    def _check_worker(self) -> None:
        try:
            self.check_once()
        except Exception as exc:
            with self._lock:
                self._last_checked_at = time.time()
                self._state = "error"
                self._error = str(exc) or "检查更新失败"
            self._event("app_update.check_failed", "Application update check failed",
                        error=str(exc))

    def start_download(self) -> dict[str, Any]:
        with self._lock:
            if not self.supported:
                raise AppUpdateError("当前启动方式不支持软件内更新")
            if not self._available or self._release is None:
                raise AppUpdateError("当前没有可安装的新版本")
            if self._state in {"downloading", "preparing", "applying"}:
                return self.status()
            self._state = "downloading"
            self._progress = 0.0
            self._downloaded_bytes = 0
            self._total_bytes = int(self._release["size"])
            self._error = ""
            worker = threading.Thread(
                target=self._download_worker, name="app-update-download", daemon=True
            )
            self._worker = worker
            worker.start()
        return self.status()

    def _download_worker(self) -> None:
        try:
            with self._lock:
                release = dict(self._release or {})
                version = self._latest_version
            self.root.mkdir(parents=True, exist_ok=True)
            suffix = ".dmg" if self.package_kind == "macos-arm64" else ".zip"
            archive = self.root / f"SensUs-{version}{suffix}"
            temporary = archive.with_suffix(archive.suffix + ".part")
            temporary.unlink(missing_ok=True)
            request = urllib.request.Request(
                str(release["browser_download_url"]),
                headers={"User-Agent": "SensUs-Workstation-App-Updater/1"},
            )
            digest = hashlib.sha256()
            total = int(release["size"])
            with urllib.request.urlopen(
                request, timeout=30, context=_ssl_context()
            ) as response, temporary.open("wb") as handle:
                final = urlparse(response.geturl())
                if final.scheme != "https" or final.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
                    raise AppUpdateError("安装包下载跳转到了不可信地址")
                downloaded = 0
                while True:
                    if self._stop.is_set():
                        raise AppUpdateError("软件正在退出，更新已取消")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_ASSET_BYTES:
                        raise AppUpdateError("安装包超过允许大小")
                    handle.write(chunk)
                    digest.update(chunk)
                    with self._lock:
                        self._downloaded_bytes = downloaded
                        self._progress = min(0.9, 0.9 * downloaded / total)
            if downloaded != total:
                raise AppUpdateError("安装包下载不完整")
            expected = str(release["digest"]).split(":", 1)[1]
            if digest.hexdigest() != expected:
                raise AppUpdateError("安装包 SHA-256 校验失败")
            os.replace(temporary, archive)
            with self._lock:
                self._state = "preparing"
                self._progress = 0.92
            staged = self._prepare_archive(archive, version)
            with self._lock:
                self._staged_path = staged
                self._state = "ready"
                self._progress = 1.0
            self._event("app_update.ready", "Application update is ready",
                        version=version, package_kind=self.package_kind)
        except Exception as exc:
            with self._lock:
                self._state = "error"
                self._error = str(exc) or "准备更新失败"
            self._event("app_update.prepare_failed", "Application update preparation failed",
                        error=str(exc))

    def _prepare_archive(self, archive: Path, version: str) -> Path:
        destination = self.root / f"staged-{version}"
        shutil.rmtree(destination, ignore_errors=True)
        if self.package_kind == "windows-x64":
            destination.mkdir(parents=True)
            _safe_zip_extract(archive, destination)
            entries = list(destination.iterdir())
            root = entries[0] if len(entries) == 1 and entries[0].is_dir() else destination
            if not (
                (root / "SensUsBackend.exe").is_file()
                and (root / "workstation" / "PORTABLE_RESOURCES.txt").is_file()
            ):
                raise AppUpdateError("Windows 更新包内容不完整")
            return root
        if self.package_kind != "macos-arm64":
            raise AppUpdateError("当前平台不支持准备整包更新")
        mount = Path(tempfile.mkdtemp(prefix="mount-", dir=self.root))
        app_destination = destination / "SensUs Workstation.app"
        try:
            subprocess.run(
                ["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse",
                 "-mountpoint", str(mount), str(archive)],
                check=True, capture_output=True, text=True, timeout=60,
            )
            source = mount / "SensUs Workstation.app"
            if not source.is_dir():
                raise AppUpdateError("macOS 更新包中找不到应用")
            destination.mkdir(parents=True)
            subprocess.run(
                ["/usr/bin/ditto", str(source), str(app_destination)],
                check=True, capture_output=True, text=True, timeout=180,
            )
        finally:
            subprocess.run(
                ["/usr/bin/hdiutil", "detach", str(mount), "-force"],
                capture_output=True, text=True, timeout=30,
            )
            shutil.rmtree(mount, ignore_errors=True)
        if not (
            (app_destination / "Contents" / "MacOS" / "SensUsWorkstation").is_file()
            and (app_destination / "Contents" / "Resources" / "backend"
                 / "SensUsBackend" / "SensUsBackend").is_file()
        ):
            raise AppUpdateError("macOS 更新包内容不完整")
        subprocess.run(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_destination)],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return app_destination

    def begin_install(self) -> dict[str, Any]:
        with self._lock:
            staged = self._staged_path
            target = self.target_path
            if self._state != "ready" or staged is None or not staged.exists():
                raise AppUpdateError("新版本尚未准备完成")
            if target is None or not target.exists():
                raise AppUpdateError("找不到当前安装位置")
            normalized_target = str(target).replace("\\", "/")
            on_macos_volume = bool(
                re.match(r"^(?:[A-Za-z]:)?/Volumes/", normalized_target)
            )
            if self.package_kind == "macos-arm64" and (
                target.suffix != ".app" or on_macos_volume
            ):
                raise AppUpdateError("请先将软件拖入“应用程序”文件夹后再更新")
            if not os.access(target.parent, os.W_OK):
                raise AppUpdateError("当前安装位置不可写，无法自动更新")
            helper = self._write_helper()
            log_path = self.root / "update-helper.log"
            launch_token = secrets.token_hex(24)
            command = self._helper_command(
                helper, target, staged, log_path, launch_token,
            )
            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "close_fds": True,
            }
            if self.package_kind == "windows-x64":
                kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(command, **kwargs)
            self._state = "applying"
            self._error = ""
        self._event("app_update.applying", "Application update handoff started",
                    version=self._latest_version, target=target)
        return self.status()

    def _write_helper(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.package_kind == "macos-arm64":
            path = self.root / "install-update.zsh"
            path.write_text(_MACOS_HELPER, encoding="utf-8")
            path.chmod(0o700)
            return path
        path = self.root / "install-update.ps1"
        path.write_text(_WINDOWS_HELPER, encoding="utf-8-sig")
        return path

    def _helper_command(
        self, helper: Path, target: Path, staged: Path, log_path: Path,
        launch_token: str,
    ) -> list[str]:
        arguments = [
            str(target), str(staged), str(self.app_pid), str(os.getpid()),
            str(log_path), self._latest_version, str(self.server_port),
            launch_token,
        ]
        if self.package_kind == "macos-arm64":
            return ["/bin/zsh", str(helper), *arguments]
        return [
            "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(helper), *arguments,
        ]

    def stop(self) -> None:
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive() and self._state != "applying":
            worker.join(timeout=2)


_MACOS_HELPER = r'''#!/bin/zsh
set -u
target="$1"
staged="$2"
app_pid="$3"
backend_pid="$4"
log_path="$5"
expected_version="$6"
server_port="$7"
launch_token="$8"
exec >>"$log_path" 2>&1
echo "[$(/bin/date -u +%FT%TZ)] update helper started"
/bin/sleep 1
/bin/kill -TERM "$app_pid" 2>/dev/null || true
for pid in "$app_pid" "$backend_pid"; do
  for _ in {1..120}; do
    /bin/kill -0 "$pid" 2>/dev/null || break
    /bin/sleep 0.25
  done
  /bin/kill -KILL "$pid" 2>/dev/null || true
done
backup="${target}.previous"
if [[ -e "$backup" ]]; then /bin/rm -rf -- "$backup"; fi
if ! /bin/mv -- "$target" "$backup"; then
  echo "cannot move current application"
  exit 1
fi
if ! /bin/mv -- "$staged" "$target"; then
  /bin/mv -- "$backup" "$target" || true
  /usr/bin/open "$target" || true
  echo "cannot install staged application; restored previous version"
  exit 1
fi
/usr/bin/xattr -cr "$target" 2>/dev/null || true
ready=0
expected_project="$(/usr/bin/dirname "$target")/$(/usr/bin/basename "$target")/Contents/Resources/workstation"
expected_binary="$target/Contents/MacOS/SensUsWorkstation"
pid_file="${log_path}.launcher-pid"
/bin/rm -f -- "$pid_file"
new_app_pid=""
if /usr/bin/open -n \
    --env "SENSUS_SERVER_PORT=${server_port}" \
    --env "SENSUS_LAUNCH_TOKEN=${launch_token}" \
    --env "SENSUS_LAUNCH_PID_FILE=${pid_file}" "$target"; then
  for _ in {1..360}; do
    if [[ -z "$new_app_pid" && -f "$pid_file" ]]; then
      candidate_pid="$(/usr/bin/tr -cd '0-9' < "$pid_file")"
      if [[ "$candidate_pid" == <-> ]]; then new_app_pid="$candidate_pid"; fi
    fi
    health="$(/usr/bin/curl -fsS --max-time 1 "http://127.0.0.1:${server_port}/api/health" 2>/dev/null || true)"
    health_version="$(echo "$health" | /usr/bin/plutil -extract version raw -o - - 2>/dev/null || true)"
    health_product="$(echo "$health" | /usr/bin/plutil -extract product raw -o - - 2>/dev/null || true)"
    health_token="$(echo "$health" | /usr/bin/plutil -extract launch_token raw -o - - 2>/dev/null || true)"
    health_project="$(echo "$health" | /usr/bin/plutil -extract project raw -o - - 2>/dev/null || true)"
    health_launcher_pid="$(echo "$health" | /usr/bin/plutil -extract launcher_pid raw -o - - 2>/dev/null || true)"
    normalized_expected="$(cd "$expected_project" 2>/dev/null && /bin/pwd -P || true)"
    normalized_project="$(cd "$health_project" 2>/dev/null && /bin/pwd -P || true)"
    launcher_command=""
    if [[ "$health_launcher_pid" == <-> ]]; then
      launcher_command="$(/bin/ps -p "$health_launcher_pid" -o command= 2>/dev/null || true)"
    fi
    if [[ "$health_version" == "$expected_version" \
          && "$health_product" == "SensUs-Electrochem-Workstation" \
          && "$health_token" == "$launch_token" \
          && -n "$normalized_expected" \
          && "$normalized_project" == "$normalized_expected" \
          && "$health_launcher_pid" == "$new_app_pid" \
          && "$launcher_command" == "$expected_binary"* ]]; then
      ready=1
      break
    fi
    /bin/sleep 0.5
  done
fi
if [[ "$ready" == "1" ]]; then
  /bin/rm -f -- "$pid_file"
  /bin/rm -rf -- "$backup"
  echo "update installed"
  exit 0
fi
if [[ "$new_app_pid" == <-> ]]; then
  /bin/kill -TERM "$new_app_pid" 2>/dev/null || true
fi
/bin/rm -f -- "$pid_file"
/bin/sleep 1
/bin/rm -rf -- "$target"
/bin/mv -- "$backup" "$target" || true
/usr/bin/open "$target" || true
echo "new application did not launch; restored previous version"
exit 1
'''


_WINDOWS_HELPER = r'''param(
  [Parameter(Mandatory=$true)][string]$Target,
  [Parameter(Mandatory=$true)][string]$Staged,
  [Parameter(Mandatory=$true)][int]$AppPid,
  [Parameter(Mandatory=$true)][int]$BackendPid,
  [Parameter(Mandatory=$true)][string]$LogPath,
  [Parameter(Mandatory=$true)][string]$ExpectedVersion,
  [Parameter(Mandatory=$true)][int]$ServerPort,
  [Parameter(Mandatory=$true)][string]$LaunchToken
)
$ErrorActionPreference = "Stop"
Start-Transcript -Path $LogPath -Append | Out-Null
try {
  Start-Sleep -Seconds 1
  Stop-Process -Id $AppPid -Force -ErrorAction SilentlyContinue
  foreach ($ProcessId in @($AppPid, $BackendPid)) {
    for ($Attempt = 0; $Attempt -lt 120; $Attempt++) {
      if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { break }
      Start-Sleep -Milliseconds 250
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
  }
  $Backup = "$Target.previous"
  if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Recurse -Force }
  Move-Item -LiteralPath $Target -Destination $Backup
  try {
    Move-Item -LiteralPath $Staged -Destination $Target
    $env:SENSUS_SERVER_PORT = [string]$ServerPort
    $env:SENSUS_LAUNCH_TOKEN = $LaunchToken
    $NewProcess = Start-Process -FilePath (Join-Path $Target "SensUsBackend.exe") -PassThru
    $ExpectedProject = [IO.Path]::GetFullPath((Join-Path $Target "workstation"))
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 180; $Attempt++) {
      try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$ServerPort/api/health" -TimeoutSec 2
        $ReportedProject = [IO.Path]::GetFullPath([string]$Health.project)
        $ProjectMatches = [StringComparer]::OrdinalIgnoreCase.Equals(
          $ReportedProject, $ExpectedProject
        )
        if (
          $Health.product -eq "SensUs-Electrochem-Workstation" -and
          $Health.version -eq $ExpectedVersion -and
          $Health.launch_token -eq $LaunchToken -and
          [string]$Health.launcher_pid -eq [string]$NewProcess.Id -and
          $ProjectMatches
        ) { $Ready = $true; break }
      } catch {}
      Start-Sleep -Seconds 1
    }
    if (-not $Ready) {
      Start-Process -FilePath "taskkill.exe" -ArgumentList @("/PID", $NewProcess.Id, "/T", "/F") -Wait -WindowStyle Hidden
      throw "updated application did not report the expected version"
    }
    Remove-Item -LiteralPath $Backup -Recurse -Force
  } catch {
    if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Recurse -Force }
    Move-Item -LiteralPath $Backup -Destination $Target
    $env:SENSUS_LAUNCH_TOKEN = ""
    $env:SENSUS_SERVER_PORT = [string]$ServerPort
    Start-Process -FilePath (Join-Path $Target "SensUsBackend.exe")
    throw
  }
} finally {
  Stop-Transcript | Out-Null
}
'''
