"""读取**用户数据**文件时的编码容错。

🔴 为什么必须有这个模块
    现场(2026-08-21,同事的中文 Windows):
        已保存的工作区当前不可用：'utf-8' codec can't decode bytes in
        position 320-321: invalid continuation byte

    因果链有个反直觉的点:**写盘当时一切正常,重启之后才坏**。
    原因不是重启改变了什么,而是 —— 同一次会话里 `self.records` 一直在内存里
    (`_append_record` 写盘的同时也 append 到内存),GUI 显示的历史来自内存,
    **文件从来没有被读回来过**;重启后 `AppState.__init__` 才第一次真的去读它。

    而那些文件是**旧版本**写的:历史上若干处漏写 `encoding=`,于是中文样品名/
    中文路径按 locale 默认编码(中文 Windows = cp936/GBK)落盘。写入端现在已经
    全部锁成 UTF-8,但**旧文件还躺在现场磁盘上,而且已经积攒了一批**。
    ⇒ 光修写入端救不了他们,读取端必须能吃下旧编码。

回退次序
    1. `utf-8-sig` —— 现行格式;`-sig` 顺手吃掉 Excel 往复后留下的 BOM
    2. `gb18030`  —— 旧的中文 Windows 落盘。它是 GBK/cp936 的超集,能把中文
       **正确还原**,而不是像 `errors="replace"` 那样烧成一串 U+FFFD
    3. `utf-8` + `errors="replace"` —— 两者都不成时的兜底,保证**永不抛**

⚠️ 已知取舍:`gb18030` 几乎能解码任意字节序列,所以一个真正损坏的 UTF-8 文件
    会被静默解成乱码中文而不是报错。对用户数据这是对的取舍(能用但有警告 >
    整个工作区不可用),而返回的编码名让调用方可以提示"这份文件建议重新导出"。

🔴 只用于**用户数据**。随包资源(index.html / styles.css / prebuilt 元数据)
    一律保持严格 UTF-8 —— 那些是我们自己产的,坏了就该立刻暴露,不该被容错掩盖。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 依次尝试的编码;最后一档带 errors="replace",保证 read_text_tolerant 不抛
FALLBACK_ENCODINGS = ("utf-8-sig", "gb18030")


def read_text_tolerant(path: str | Path) -> tuple[str, str]:
    """读取文本,返回 ``(文本, 真正用到的编码名)``。永不抛 UnicodeDecodeError。"""
    target = Path(path)
    for encoding in FALLBACK_ENCODINGS:
        try:
            return target.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    return target.read_text(encoding="utf-8", errors="replace"), "utf-8/replace"


def read_json_tolerant(path: str | Path) -> tuple[Any, str]:
    """读取 JSON,返回 ``(对象, 编码名)``。

    解码由 `read_text_tolerant` 兜住;JSON 本身语法错仍会抛
    `json.JSONDecodeError` —— 调用方本来就在处理它,不该被这层吞掉。
    """
    text, encoding = read_text_tolerant(path)
    return json.loads(text), encoding


def read_csv_lines(path: str | Path) -> tuple[list[str], str]:
    """读取 CSV 的行列表,返回 ``(行, 编码名)``。供 `csv.DictReader` 直接消费。"""
    text, encoding = read_text_tolerant(path)
    return text.splitlines(), encoding


def is_legacy_encoding(encoding: str) -> bool:
    """True 表示这份文件不是现行 UTF-8,值得提示用户重新导出。"""
    return encoding not in ("utf-8-sig", "utf-8")
