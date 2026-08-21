import ast
import hashlib
import io
import os
import re
import ssl
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pa_host import app_update as app_update_module
from pa_host.app_update import (
    AppUpdateError,
    AppUpdateManager,
    _safe_zip_extract,
    _ssl_context,
)


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
        target_path=target, app_pid=123, server_port=54321,
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
    assert "54321" in command
    assert re.fullmatch(r"[0-9a-f]{48}", command[-1])
    assert "SENSUS_SERVER_PORT" in helper_text
    assert "SENSUS_LAUNCH_TOKEN" in helper_text
    assert "SensUs-Electrochem-Workstation" in helper_text
    assert "launch_token" in helper_text
    assert "project" in helper_text
    assert "127.0.0.1:8765/api/health" not in helper_text
    if package_kind == "macos-arm64":
        assert "/usr/bin/open -n" in helper_text
        assert "SENSUS_LAUNCH_PID_FILE" in helper_text
        assert 'health_launcher_pid" == "$new_app_pid' in helper_text
    else:
        assert "launcher_pid" in helper_text
        assert "$NewProcess.Id" in helper_text


def test_update_helper_inherits_dynamic_server_port_from_launcher(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("SENSUS_SERVER_PORT", "51234")
    manager = AppUpdateManager(
        "0.4.6", tmp_path / "state", package_kind="windows-x64",
        target_path=tmp_path / "installed", app_pid=123,
    )

    assert manager.server_port == 51234


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


def test_ssl_context_never_disables_verification() -> None:
    """回落逻辑只允许**补** CA,绝不允许降级成不校验。"""
    context = _ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ssl_context_loads_a_fallback_when_the_bundle_has_no_ca(tmp_path: Path) -> None:
    """冻结体里没有 CA 时必须回落到系统证书包。

    这是 2026-08-21 的现场缺陷:PyInstaller 包内既无 certifi 也无 cacert.pem,
    OpenSSL 的默认 CA 路径又是构建机上的路径 ⇒ 信任库为空,
    每次更新检查都以 CERTIFICATE_VERIFY_FAILED 失败(实测 156 次全败、成功 0 次),
    用户因此完全收不到新版本提示。
    """
    empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    assert empty.cert_store_stats()["x509_ca"] == 0
    loaded: list[str] = []
    real_load = ssl.SSLContext.load_verify_locations

    def fake_load(self: ssl.SSLContext, cafile: str | None = None, **kwargs: object) -> None:
        if cafile:
            loaded.append(str(cafile))
        real_load(self, cafile=cafile, **kwargs)  # type: ignore[arg-type]

    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(Path(ssl.get_default_verify_paths().openssl_cafile).read_bytes()
                        if Path(str(ssl.get_default_verify_paths().openssl_cafile)).is_file()
                        else b"")
    if not ca_file.stat().st_size:
        pytest.skip("本机没有可用的系统 CA 包,无法构造回落素材")

    with patch("pa_host.app_update.ssl.create_default_context", return_value=empty), \
            patch.dict(os.environ, {"SSL_CERT_FILE": str(ca_file)}), \
            patch.object(ssl.SSLContext, "load_verify_locations", fake_load):
        context = _ssl_context()

    assert loaded, "信任库为空时应当尝试加载回落 CA"
    assert context.cert_store_stats()["x509_ca"] > 0
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_ssl_context_leaves_a_healthy_store_untouched() -> None:
    """CA 本来就正常的平台(如 Windows 从系统证书库加载)行为必须完全不变。"""
    healthy = ssl.create_default_context()
    if healthy.cert_store_stats()["x509_ca"] == 0:
        pytest.skip("本机默认信任库为空,该分支无法在此验证")
    with patch("pa_host.app_update.ssl.create_default_context", return_value=healthy), \
            patch.object(
                ssl.SSLContext, "load_verify_locations",
                side_effect=AssertionError("信任库正常时不应加载任何回落 CA"),
            ):
        assert _ssl_context() is healthy


# 🔴 中文 Windows 上 locale 默认编码是 cp936(GBK),macOS 上是 UTF-8;两边都是
# strict。`subprocess.run(..., text=True)` 不写 `encoding=` 时子进程输出就按这个
# locale 编码 strict 解码 —— 工具吐出任何不合该编码的字节都会抛
# UnicodeDecodeError,把一次正在进行的硬件操作/整包更新直接打断。
# 下面两条测试是这一族缺陷的门禁:一条喂真字节走真解码路径,一条做 AST 静态扫描。

_GBK_NOISE = "低压警告".encode("gb18030")
"""GBK 编码的中文:字节以 0xB5 开头,不是合法 UTF-8 序列 ⇒ strict UTF-8 解码必抛。"""


def test_macos_prepare_tolerates_non_utf8_tool_output(tmp_path: Path) -> None:
    """🔴 hdiutil/ditto/codesign 吐出非 UTF-8 字节时,整包更新不许被解码异常打断。

    这里不 mock 掉解码:替换的只有**命令本身**,生产代码给 ``subprocess.run``
    的 kwargs 原样转发给真子进程,由真的 ``TextIOWrapper`` 去解码真的 GBK 字节。
    撤掉 ``_prepare_archive`` 里任意一处的 ``encoding=`` / ``errors=``,
    这条测试会以 ``UnicodeDecodeError`` 变红。
    """

    state_root = tmp_path / "state"
    manager = AppUpdateManager(
        "0.4.6", state_root, package_kind="macos-arm64",
        target_path=tmp_path / "SensUs Workstation.app",
    )
    manager.root.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "update.dmg"
    archive.write_bytes(b"stand-in for the disk image")

    real_run = subprocess.run
    emitter = (
        "import sys;"
        f"sys.stdout.buffer.write({_GBK_NOISE!r});"
        f"sys.stderr.buffer.write({_GBK_NOISE!r})"
    )
    seen: dict[str, dict[str, object]] = {}
    decoded: list[str] = []

    def fake_run(command: list[str], **kwargs: object):
        tool = Path(str(command[0])).name
        label = f"{tool} {command[1]}" if tool == "hdiutil" else tool
        seen[label] = dict(kwargs)
        if label == "hdiutil attach":
            mount = Path(str(command[command.index("-mountpoint") + 1]))
            (mount / "SensUs Workstation.app").mkdir(parents=True, exist_ok=True)
        elif label == "ditto":
            bundle = Path(str(command[2]))
            (bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
            (bundle / "Contents" / "MacOS" / "SensUsWorkstation").write_bytes(b"app")
            backend = bundle / "Contents" / "Resources" / "backend" / "SensUsBackend"
            backend.mkdir(parents=True, exist_ok=True)
            (backend / "SensUsBackend").write_bytes(b"backend")
        completed = real_run([sys.executable, "-c", emitter], **kwargs)
        decoded.append(str(completed.stdout) + str(completed.stderr))
        return completed

    with patch("pa_host.app_update.subprocess.run", side_effect=fake_run):
        staged = manager._prepare_archive(archive, "0.4.7")

    assert staged == manager.root / "staged-0.4.7" / "SensUs Workstation.app"
    # 四处调用点全部走到,缺一处就说明这条测试没覆盖到它。
    assert set(seen) == {"hdiutil attach", "ditto", "hdiutil detach", "codesign"}
    for label, kwargs in seen.items():
        assert kwargs.get("encoding") == "utf-8", f"{label} 没把编码钉成 utf-8"
        assert kwargs.get("errors") == "replace", f"{label} 没放宽 errors"
    # 坏字节变成 U+FFFD,而不是抛异常;也不是按 locale 猜成了 "低压警告"。
    assert decoded and all("\ufffd" in chunk for chunk in decoded)
    assert all("低压警告" not in chunk for chunk in decoded)


_LOCALE_DECODE_GUARDED_MODULES = (
    "collect.py", "windows_jlink.py", "app_update.py", "jlink_usb.py",
)


def _subprocess_text_offenders(path: Path) -> list[str]:
    """挑出一个模块里 ``text=True`` 却没写 ``encoding=``/``errors=`` 的 subprocess 调用。

    ``windows_jlink.py`` 把公共 kwargs 提成 ``common = {...}`` 再 ``**common``
    展开,只看字面 keyword 会漏,所以先把模块内的 dict 字面量 / ``dict(拷贝)`` /
    下标赋值收成"名字 → 键集合"。已知局限:``**`` 后面跟无法静态求值的表达式
    (如 ``**runtime.hidden_subprocess_kwargs()``)时按"没提供额外键"处理。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    dict_keys: dict[str, set[str]] = {}

    def literal_keys(node: ast.expr) -> set[str] | None:
        if isinstance(node, ast.Dict):
            keys: set[str] = set()
            for key, value in zip(node.keys, node.values):
                if key is None:  # {**other}
                    inherited = literal_keys(value)
                    if inherited is None:
                        return None
                    keys |= inherited
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
                else:
                    return None
            return keys
        if isinstance(node, ast.Name):
            return dict_keys.get(node.id)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "dict" and len(node.args) == 1
                and not node.keywords):
            return literal_keys(node.args[0])
        return None

    assignments = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    # 走三遍取不动点:`launcher_common = dict(common)` 这类链式拷贝的顺序
    # 不由 ast.walk 保证。
    for _ in range(3):
        for node in assignments:
            resolved = literal_keys(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if resolved is not None:
                        dict_keys.setdefault(target.id, set()).update(resolved)
                elif (isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    dict_keys.setdefault(target.value.id, set()).add(
                        target.slice.value
                    )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "subprocess"
                and function.attr in {
                    "run", "Popen", "call", "check_call", "check_output",
                }):
            continue
        explicit = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        present = set(explicit)
        for kw in node.keywords:
            if kw.arg is None:
                present |= literal_keys(kw.value) or set()
        text_like = {"text", "universal_newlines"} & present

        def enables_text(key: str) -> bool:
            value = explicit.get(key)
            if isinstance(value, ast.Constant):
                return value.value is True
            return True  # 变量/表达式/来自 ** 展开 → 保守当成开了文本模式

        if not any(enables_text(key) for key in text_like):
            continue
        missing = [key for key in ("encoding", "errors") if key not in present]
        if missing:
            offenders.append(
                f"{path.name}:{node.lineno} subprocess.{function.attr} "
                f"开了文本模式但缺 {'/'.join(missing)}="
            )
    return offenders


def test_hardware_subprocess_calls_never_decode_with_the_locale_encoding() -> None:
    """机器门禁:这四个模块里任何 subprocess 文本模式调用都必须自带 encoding/errors。

    🔴 作用域只限这四个模块(硬件操作 + 自动更新链路),不扫全包 —— 其余文件由
    各自的测试负责,在这里扫会把别处的改动误报到这条断言上。
    """

    package = Path(app_update_module.__file__).resolve().parent
    offenders: list[str] = []
    for module in _LOCALE_DECODE_GUARDED_MODULES:
        offenders += _subprocess_text_offenders(package / module)
    assert offenders == []


def test_the_subprocess_text_gate_actually_catches_a_missing_encoding(
    tmp_path: Path,
) -> None:
    """门禁自检:上面那条断言恒为真才是最坏情况,这里证明它抓得住缺陷。"""

    sample = tmp_path / "sample.py"
    sample.write_text(
        "import subprocess\n"
        "common = {'capture_output': True, 'text': True}\n"
        "fixed = {**common, 'encoding': 'utf-8', 'errors': 'replace'}\n"
        "subprocess.run(['a'], text=True)\n"
        "subprocess.run(['b'], **common)\n"
        "subprocess.run(['c'], universal_newlines=True)\n"
        "subprocess.run(['d'], **fixed)\n"
        "subprocess.run(['e'], text=True, encoding='utf-8', errors='replace')\n"
        "subprocess.run(['f'], capture_output=True)\n"
        "subprocess.run(['g'], text=False)\n",
        encoding="utf-8",
    )

    offenders = _subprocess_text_offenders(sample)

    assert [line.split(":")[1].split(" ")[0] for line in offenders] == ["4", "5", "6"]
