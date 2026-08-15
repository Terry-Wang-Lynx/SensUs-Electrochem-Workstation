#!/bin/bash
# 经 USB 上传 app(全程不需要调试器)。
# 用法:03-usb-upload.sh [镜像.bin]     默认 ../images/app.signed.bin
#
# 窗口规则:slot0 为空 ⇒ 永久等待;已有可引导 app ⇒ 只有上电后 5 秒。
# 本脚本会**轮询等口出现**,所以你不用掐时间 —— 需要重上电时拔插一次 USB-C 即可。
set -e
source "$(dirname "$0")/00-env.sh"
IMG=${1:-$IMG_DIR/app.signed.bin}
[ -x "$SMPMGR" ] || { echo "🔴 找不到 smpmgr($SMPMGR)。见 README「主机侧准备」"; exit 1; }
echo "[等待] 板端 CDC 口(需要时请拔插一次 USB-C)..."
for i in $(seq 1 600); do p=$(board_cdc); [ -n "$p" ] && break; sleep 0.2; done
[ -n "$p" ] || { echo "🔴 120s 没等到板端口。检查:① macOS 是否弹窗问过「允许配件连接」并已允许"; echo "   ② USB-C 方向(只有 CC1 有 Rd、D+/D- 只在 A 侧 ⇒ 插反了没数据)"; exit 1; }
echo "[捕获] $p"
"$SMPMGR" --port "$p" --timeout 10 upgrade "$IMG" 2>&1 | grep -vE "MCUMgr parameters|__init__|header=|smp_data|rsn=|version=|sequence="
echo
echo "✅ 期望最后一行是 'Upgrade complete.'"
echo "   之后板端口应**消失**(app 接管)—— 端口消失本身就是镜像通过校验的证据:"
echo "   若镜像无效,BOOT_SERIAL_NO_APPLICATION=y 会让 MCUboot 永久留在 recovery、口不会消失。"
