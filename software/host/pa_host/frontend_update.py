"""Signed, restart-only frontend updates for the local workstation server."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable


BACKEND_API_MAJOR = 2
CHECK_INTERVAL_S = 30 * 60
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 64 * 1024 * 1024
REQUIRED_FILES = ("index.html", "styles.css", "app.js", "compact.html", "compact.js")


class FrontendUpdateError(RuntimeError):
    pass


class FrontendUpdateDeferred(FrontendUpdateError):
    pass


def _canonical_manifest(payload: dict[str, Any]) -> bytes:
    signed = {
        "schema_version": int(payload["schema_version"]),
        "channel": str(payload["channel"]),
        "version": str(payload["version"]),
        "api_major": int(payload["api_major"]),
        "zip_url": str(payload["zip_url"]),
        "sha256": str(payload["sha256"]).lower(),
    }
    return json.dumps(
        signed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _safe_version(value: object) -> str:
    version = str(value).strip()
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}", version):
        raise FrontendUpdateError("invalid frontend version")
    return version


def _version_key(version: str) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.lower())
        for token in re.findall(r"[0-9]+|[A-Za-z]+", version)
    )


def _decode_public_key(value: str) -> bytes:
    text = value.strip()
    try:
        raw = bytes.fromhex(text) if re.fullmatch(r"[0-9a-fA-F]{64}", text) else base64.b64decode(text, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise FrontendUpdateError("invalid Ed25519 public key") from exc
    if len(raw) != 32:
        raise FrontendUpdateError("Ed25519 public key must be 32 bytes")
    return raw


def _verify_signature(public_key: str, payload: dict[str, Any]) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise FrontendUpdateError("cryptography is required for frontend updates") from exc
    try:
        signature = base64.b64decode(str(payload["signature"]), validate=True)
        Ed25519PublicKey.from_public_bytes(_decode_public_key(public_key)).verify(
            signature, _canonical_manifest(payload)
        )
    except FrontendUpdateError:
        raise
    except (KeyError, ValueError, base64.binascii.Error, InvalidSignature) as exc:
        raise FrontendUpdateError("frontend signature verification failed") from exc


def _validate_frontend_dir(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in REQUIRED_FILES)


def _extract_archive(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        if sum(info.file_size for info in bundle.infolist()) > MAX_EXTRACTED_BYTES:
            raise FrontendUpdateError("frontend archive expands beyond the size limit")
        for info in bundle.infolist():
            member = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (
                not info.filename
                or member.is_absolute()
                or ".." in member.parts
                or "\\" in info.filename
                or stat.S_ISLNK(mode)
            ):
                raise FrontendUpdateError("unsafe path in frontend archive")
            target = destination.joinpath(*member.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                with bundle.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


class FrontendUpdater:
    def __init__(
        self,
        bundled_dir: Path,
        state_dir: Path,
        resource_dir: Path,
        *,
        manifest_url: str = "",
        public_key: str = "",
        channel: str = "stable",
    ) -> None:
        self.bundled_dir = bundled_dir
        self.root = state_dir / "frontend-updates"
        self.resource_dir = resource_dir
        config = self._load_config()
        self.manifest_url = (
            os.environ.get("SENSUS_UPDATE_MANIFEST_URL", "").strip()
            or manifest_url
            or str(config.get("manifest_url", "")).strip()
        )
        self.public_key = (
            os.environ.get("SENSUS_UPDATE_PUBLIC_KEY", "").strip()
            or public_key
            or str(config.get("public_key", "")).strip()
        )
        self.channel = str(config.get("channel", channel) or channel)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def _load_config(self) -> dict[str, Any]:
        path = self.resource_dir / "config" / "frontend-update.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    @property
    def enabled(self) -> bool:
        return bool(self.manifest_url and self.public_key and self.channel == "stable")

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _save_state(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _version_dir(self, version: str) -> Path:
        return self.root / "versions" / _safe_version(version)

    def prepare_startup(self) -> Path:
        """Activate pending content or roll back an unconfirmed prior boot."""
        if not self.enabled and not self.state_path.exists():
            return self.bundled_dir
        with self._lock:
            state = self._load_state()
            failed = str(state.get("boot_pending", ""))
            if failed:
                previous = str(state.get("previous_version", ""))
                state["failed_version"] = failed
                state["active_version"] = previous
                state["previous_version"] = ""
                state["boot_pending"] = ""
            pending = str(state.get("pending_version", ""))
            if pending and _validate_frontend_dir(self._version_dir(pending)):
                state["previous_version"] = str(state.get("active_version", ""))
                state["active_version"] = pending
                state["pending_version"] = ""
                state["boot_pending"] = pending
            active = str(state.get("active_version", ""))
            active_dir = self._version_dir(active) if active else self.bundled_dir
            if active and not _validate_frontend_dir(active_dir):
                state["active_version"] = ""
                state["boot_pending"] = ""
                active_dir = self.bundled_dir
            self._save_state(state)
            return active_dir if _validate_frontend_dir(active_dir) else self.bundled_dir

    def mark_ready(self) -> dict[str, Any]:
        with self._lock:
            state = self._load_state()
            active = str(state.get("active_version", ""))
            if state.get("boot_pending") == active:
                state["boot_pending"] = ""
                state["last_good_version"] = active
                self._save_state(state)
            return {
                "enabled": self.enabled,
                "channel": self.channel,
                "active_version": active or "bundled",
                "pending_version": state.get("pending_version", ""),
            }

    @staticmethod
    def _read_url(
        url: str, maximum: int, continue_check: Callable[[], bool] | None = None
    ) -> bytes:
        request = urllib.request.Request(
            url, headers={"User-Agent": "SensUs-Workstation-Updater/1"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            length = int(response.headers.get("Content-Length") or 0)
            if length > maximum:
                raise FrontendUpdateError("frontend download is too large")
            chunks: list[bytes] = []
            total = 0
            while True:
                if continue_check is not None and not continue_check():
                    raise FrontendUpdateDeferred("hardware became busy")
                chunk = response.read(min(64 * 1024, maximum + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise FrontendUpdateError("frontend download is too large")
            data = b"".join(chunks)
        if len(data) > maximum:
            raise FrontendUpdateError("frontend download is too large")
        return data

    def check_once(self, hardware_idle: Callable[[], bool]) -> bool:
        if not self.enabled or not hardware_idle():
            return False
        raw_manifest = self._read_url(self.manifest_url, MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrontendUpdateError("invalid frontend manifest") from exc
        if not isinstance(manifest, dict):
            raise FrontendUpdateError("invalid frontend manifest")
        if (
            int(manifest.get("schema_version", 0)) != 1
            or str(manifest.get("channel", "")) != "stable"
            or int(manifest.get("api_major", -1)) != BACKEND_API_MAJOR
        ):
            raise FrontendUpdateError("incompatible frontend manifest")
        version = _safe_version(manifest.get("version"))
        expected_hash = str(manifest.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise FrontendUpdateError("invalid frontend SHA256")
        _verify_signature(self.public_key, manifest)
        with self._lock:
            state = self._load_state()
            active_version = str(state.get("active_version", ""))
            if active_version and _version_key(version) <= _version_key(active_version):
                return False
            if version in {
                active_version,
                str(state.get("pending_version", "")),
                str(state.get("failed_version", "")),
            }:
                return False
        if not hardware_idle():
            return False
        try:
            archive_bytes = self._read_url(
                str(manifest["zip_url"]), MAX_ARCHIVE_BYTES, hardware_idle
            )
        except FrontendUpdateDeferred:
            return False
        if hashlib.sha256(archive_bytes).hexdigest() != expected_hash:
            raise FrontendUpdateError("frontend SHA256 mismatch")
        if not hardware_idle():
            return False
        self.root.mkdir(parents=True, exist_ok=True)
        versions = self.root / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="frontend-", dir=self.root) as temporary:
            temporary_dir = Path(temporary)
            archive = temporary_dir / "frontend.zip"
            archive.write_bytes(archive_bytes)
            extracted = temporary_dir / "extracted"
            extracted.mkdir()
            _extract_archive(archive, extracted)
            if not _validate_frontend_dir(extracted):
                raise FrontendUpdateError("frontend bundle is incomplete")
            destination = self._version_dir(version)
            shutil.rmtree(destination, ignore_errors=True)
            os.replace(extracted, destination)
        with self._lock:
            state = self._load_state()
            state["pending_version"] = version
            self._save_state(state)
        return True

    def start(self, hardware_idle: Callable[[], bool]) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()

        def monitor() -> None:
            while not self._stop.is_set():
                try:
                    self.check_once(hardware_idle)
                except (OSError, ValueError, FrontendUpdateError):
                    pass
                self._stop.wait(CHECK_INTERVAL_S)

        self._thread = threading.Thread(
            target=monitor, name="frontend-updater", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
