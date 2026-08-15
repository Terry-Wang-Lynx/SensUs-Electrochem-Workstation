# 上位机分发与前端热更新

## 分发模型

Windows 与 macOS 使用同一套 Python LTS 后端和本地 HTTP API。后端随安装包冻结，
不会从网页执行硬件控制代码；前端仍由 `127.0.0.1` 本地服务提供，因此浏览器同源
限制和现有控制接口校验继续有效。

- macOS：Apple Silicon、macOS 13+，输出 ad-hoc 签名 DMG。
- Windows：Windows 10/11 x64，输出包含 WebView2 窗口的 ZIP。
- 源码开发：`make app`、`make run`、现有烧录和测量命令保持不变。
- 便携版数据：配置和缓存放系统用户目录，实验数据默认放 `Documents/SensUs Measurements`。

本机构建：

```bash
make portable-macos
make dmg
```

Windows 原生构建：

```powershell
./windows/build_portable.ps1
```

产物只写入 `artifacts/releases/<version>/`。Windows EXE 必须在 Windows 或对应 CI
runner 上生成，不能从 macOS 交叉打包。

## 稳定前端更新

安装包内始终保留一份前端。配置了稳定频道后，后端会在启动时和之后每 30 分钟
检查一次签名 manifest，但只有在没有测量、自动任务或固件操作时才会联网下载。
下载完成只标记为 pending，重启应用后才启用；新前端没有调用 ready 接口时，下次
启动自动回退到前一个成功版本。更新 ZIP 经过 SHA256、Ed25519、API major、必需文件
和路径穿越检查。

两个平台同时热更新的关键是让它们在构建时写入相同的：

- `SENSUS_FRONTEND_MANIFEST_URL`
- `SENSUS_FRONTEND_PUBLIC_KEY`

GitHub Actions 从同名 repository variables 注入。未配置这两项时更新器完全禁用，
软件只使用安装包内前端。

建议使用独立的公开、仅含 `gui/` 静态资源和 release 文件的仓库。私钥只保存在发布
机器或 GitHub Actions secret，不进入任一仓库。生成稳定 ZIP 与 manifest：

```bash
.venv/bin/python -m pip install -e '.[portable]'
PYTHONPATH=software/host .venv/bin/python packaging/sign_frontend.py \
  software/host/pa_host/gui \
  --version 2026.08.1 \
  --zip-url https://github.com/ORG/FRONTEND/releases/download/2026.08.1/frontend-2026.08.1.zip \
  --private-key /secure/path/frontend-ed25519.pem \
  --output /tmp/sensus-frontend-release
```

上传 ZIP 后，把 `stable.json` 放到固定 URL。客户端只接受 `channel=stable`，不会跟随
测试频道。不要把 GitHub Pages 直接嵌入 App，也不要为远程网页开放硬件 API CORS。

## 固件资源边界

便携包包含当前受版本控制的 V4 预编译固件，以及已归档到
`packaging/resources/v51/` 的 V5.1 MCUboot/签名镜像。便携版只允许烧录与随包元数据
完全一致的稳定条件；自定义条件仍从源码版和现有 NCS 工具链编译，避免 App 包内
写源码或覆盖开发构建。打包和测试过程不会连接、复位或烧录任何板卡。
