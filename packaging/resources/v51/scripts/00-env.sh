#!/bin/bash
# V5.1 交接脚本的公共环境。其他脚本用 `source 00-env.sh`。
#
# 🔴 JLinkExe 必须是 **V8.80**(STM32CubeIDE 自带)。
#    /usr/local/bin/JLinkExe 是 V9.46,**连不上这支克隆探头** ——
#    根因见 docs/troubleshooting/jlink-v9克隆-swd-turnaround不松线.md
JLINK=/Applications/STM32CubeIDE.app/Contents/Eclipse/plugins/com.st.stm32cube.ide.mcu.externaltools.jlink.macos64_2.5.100.202509120932/tools/bin/JLinkExe

# 🔴 探头自己也会枚举成一个 CDC 口,必须按序列号排除,否则会把它当成板子
JLINK_CDC_SERIAL=0000297345691

DEVICE=nRF52833_xxAA
SPEED=1000

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMG_DIR="$HERE/../images"
SMPMGR="${SMPMGR:-/tmp/smpvenv/bin/smpmgr}"   # 见 README「主机侧准备」

# 🔴 每次 J-Link 调用都开 -log:`Device will be unsecured now.`(=静默整片擦除)
#    这行**只在 log 文件里**,终端完全看不到。见 troubleshooting §0.6。
JLOG=${JLOG:-/tmp/jlink-v51.log}

jlink_run() {   # 用法:jlink_run <<'EOF' ...命令... EOF
    local f; f=$(mktemp)
    { echo "si SWD"; echo "speed $SPEED"; echo "device $DEVICE"; echo "connect"; cat; echo "q"; } > "$f"
    "$JLINK" -CommanderScript "$f" -log "$JLOG" 2>&1
    if grep -q "unsecured" "$JLOG"; then
        echo "🔴🔴 J-Link 刚刚自动整片擦除了(Device will be unsecured now.)!"
        echo "     ⇒ UICR 也被擦了,REGOUT0/APPROTECT 都要重烧。跑 01-burn-uicr.sh。"
    fi
    rm -f "$f"
}

board_cdc() {   # 打印板端 CDC 口(排除探头自己的);没有就空
    ls /dev/cu.usbmodem* 2>/dev/null | grep -v "$JLINK_CDC_SERIAL" | head -1
}
