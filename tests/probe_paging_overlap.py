# -*- coding: utf-8 -*-
"""翻页重叠探针：验证"翻一整页也保留上一页末尾几行"。

    python tests/probe_paging_overlap.py

不打网络、不落盘，只造一个 QTextEdit 当排版面，直接调 main.py 里的
StealthReader.with_page_overlap。四种边界必须成立：

1. 默认（2 行）——退出起点确实在原始落点之上，且退回的量是整数个视觉行；
2. page_overlap_lines = 0 —— 完整退化成"下一页从上一页的下一行开始"；
3. 非法值（字符串 / 超范围）—— 分别回落默认与夹到上限，不能抛异常；
4. 退无可退（落点靠近缓冲区开头）—— 返回原值，绝不返回负数。

第 2 条是最要紧的：重叠一旦把步长吃成 0，翻页就原地不动。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SR, "libs"))
sys.path.insert(0, SR)
os.chdir(SR)

from PyQt5.QtCore import QPoint
from PyQt5.QtGui import QFont, QTextCursor
from PyQt5.QtWidgets import QApplication, QTextEdit

import main as app_main

PASS = []
FAIL = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("%s %-34s %s" % ("ok  " if ok else "FAIL", name, detail))


class Shim:
    """with_page_overlap 只碰这两样东西，壳子给到就够了。"""

    def __init__(self, widget, config):
        self.text_edit = widget
        self.config = config


def build_editor():
    edit = QTextEdit()
    edit.setFont(QFont("Microsoft YaHei", 14))
    edit.resize(390, 260)
    paras = ["第%d段 " % i + "这是一段用来测试折行的中文文字，长度足够在窄窗口里折成好几行。" * 2
             for i in range(12)]
    edit.setPlainText("\n".join(paras))
    edit.show()                       # 要真实行盒才能量出视觉行，离屏没有字体
    QApplication.processEvents()
    return edit


def main():
    app = QApplication(sys.argv)
    edit = build_editor()

    cursor = edit.cursorForPosition(QPoint(0, edit.viewport().height() + 2))
    raw = cursor.position()
    check("能量出一页", raw > 0, "raw=%d" % raw)

    call = lambda cfg, pos: app_main.StealthReader.with_page_overlap(Shim(edit, cfg), pos)

    for lines, expect_back in ((1, 1), (2, 2), (3, 3)):
        got = call({"page_overlap_lines": lines}, raw)
        back = raw - got
        # 退回去之后，往下走 lines 步应当正好回到原始落点
        probe = edit.textCursor()
        probe.setPosition(got)
        for _ in range(lines):
            probe.movePosition(QTextCursor.Down, QTextCursor.MoveAnchor, 1)
        check("lines=%d 退 %d 行" % (lines, expect_back),
              probe.position() == raw and got > 0,
              "back=%d chars, down=%d" % (back, probe.position()))

    off = call({"page_overlap_lines": 0}, raw)
    check("lines=0 不退", off == raw, "raw=%d -> %d" % (raw, off))

    bad = call({"page_overlap_lines": "abc"}, raw)
    check("非法值回落默认", bad == call({}, raw), "%d" % bad)

    huge = call({"page_overlap_lines": 99}, raw)
    check("超范围不抛异常且不倒退", 0 <= huge <= raw, "%d" % huge)

    edge = call({"page_overlap_lines": 5}, 10)
    check("退无可退回原值", edge == 10, "%d" % edge)

    zero = call({"page_overlap_lines": 2}, 0)
    check("落点 0 原样返回", zero == 0, "%d" % zero)

    print("\n%d 项通过, %d 项失败" % (len(PASS), len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
