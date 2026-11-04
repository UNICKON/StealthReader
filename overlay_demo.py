#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""覆盖式伪装 demo：**不透明**窗口盖住一块区域，按底下那块区域的排版和底色来写字。

真人 2026-11-04 的思路：窗口叠在文字上，窗口就显示成对应的排版、字体。
这个 demo 只做"看得见的那一半"——量出底下的行距/墨高/字色/底色/左边距/首行墨顶，
盖上、用同样的排版填字，然后**把覆盖层自己再量一遍**、按差值回头修，直到对得上。
不接社区流，填的是占位文字。

用法：
    python overlay_demo.py --self      # 零屏幕：造一块假文档 → 采样 → 覆盖 → 量误差
    python overlay_demo.py             # 真桌面：盖在窗口那块位置，采它下面的像素
    python overlay_demo.py --rect 100,80,900,220
    python overlay_demo.py --show      # 真桌面 + 只打印量到的值（不盖窗口）

热键（真桌面模式）：R 重新采样（会先藏一下窗口取图）、Esc 退出。

两点先说清：
* `--self` **别在离屏平台下跑**（`QT_QPA_PLATFORM=offscreen`）：那套 Qt 没有字体目录，
  字画不出来，量出来永远是"这块里几乎没有字"。
* 字号只能估（从墨迹高度反推，±2px）；行距和左边距能直接量到像素。
"""

import os
import sys

SR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SR, "libs"))

from PyQt5.QtCore import QRect, Qt, QTimer                      # noqa: E402
from PyQt5.QtGui import (QColor, QFont, QFontDatabase, QPainter,  # noqa: E402
                         QTextBlockFormat, QTextCursor)
from PyQt5.QtWidgets import QApplication, QTextEdit, QWidget     # noqa: E402

# CJK 字的墨迹高度 ≈ 1.0 em（雅黑 12/14/16/18/20/24/30px 实测墨高 13/14/16/19/20/24/29）。
# 只用来从"行内黑带高度"反推字号；字号是估值，行距才是准的。
INK_RATIO = 1.0
FILLER = ("窗口盖住的这一块，行距和左边距都是照着下面那段文字量出来的，"
          "看起来应该像同一份文档里接着写下去的一段。")


def lum(c):
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()


def measure(img):
    """量一张图里的排版：底色、字色、行距、墨高、左边距、首个墨顶、字号估值。

    `img` 是"要盖住的那块区域"的截图。量不出来时返回 `ok=False` 并说明原因。
    """
    w, h = img.width(), img.height()
    if w < 12 or h < 12:
        return {"ok": False, "why": "区域太小 (%dx%d)" % (w, h)}

    # 底色 = 量化到 4bit 后的众数（抗噪），再回原图求均值
    buckets = {}
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            c = img.pixelColor(x, y)
            k = (c.red() >> 4, c.green() >> 4, c.blue() >> 4)
            b = buckets.get(k)
            if b is None:
                b = buckets[k] = [0, 0, 0, 0]
            b[0] += c.red()
            b[1] += c.green()
            b[2] += c.blue()
            b[3] += 1
    _k, b = max(buckets.items(), key=lambda kv: kv[1][3])
    bg = QColor(b[0] // b[3], b[1] // b[3], b[2] // b[3])
    bgl = lum(bg)

    # 字色 = 最暗那一撮像素的均值；顺手记每行暗像素数（行投影）和每行第一个暗像素
    darks = []
    prof = [0] * h
    first_dark_x = [None] * h
    cut = bgl - 45
    for y in range(h):
        for x in range(w):
            c = img.pixelColor(x, y)
            if lum(c) < cut:
                prof[y] += 1
                if first_dark_x[y] is None:
                    first_dark_x[y] = x
                darks.append((lum(c), c))
    if len(darks) < max(60, w * h // 900):
        return {"ok": False, "why": "这块里几乎没有字（暗像素 %d 个）" % len(darks),
                "bg": bg}

    darks.sort(key=lambda t: t[0])
    take = darks[:max(40, len(darks) // 40)]
    fg = QColor(sum(c.red() for _l, c in take) // len(take),
                sum(c.green() for _l, c in take) // len(take),
                sum(c.blue() for _l, c in take) // len(take))

    # 行距：对行投影做自相关，取第一个"够高"的 lag（避免取到它的整数倍）
    mean = sum(prof) / float(h)
    sig = [p - mean for p in prof]
    scores = {}
    for lag in range(10, max(11, min(120, h // 2))):
        s = 0.0
        for y in range(h - lag):
            s += sig[y] * sig[y + lag]
        scores[lag] = s / (h - lag)
    if not scores:
        return {"ok": False, "why": "区域太矮，量不出行距", "bg": bg, "fg": fg}
    top_score = max(scores.values())
    pitch = None
    for lag in sorted(scores):
        if scores[lag] >= 0.85 * top_score:
            pitch = lag
            break
    if not pitch or pitch < 10:
        return {"ok": False, "why": "行距量不准（自相关没找到周期）", "bg": bg, "fg": fg}

    # 行带：连续有暗像素的行 → 一段墨迹，高度就是字高（不含行间空白）
    bands = []
    y = 0
    while y < h:
        if prof[y] > 0:
            y0 = y
            while y < h and prof[y] > 0:
                y += 1
            bands.append((y0, y))
        else:
            y += 1
    ink = 0
    if len(bands) >= 2:
        inks = sorted(b1 - b0 for b0, b1 in bands[:-1])   # 最后一段可能被下边缘切掉
        ink = inks[len(inks) // 2]
    elif bands:
        ink = bands[0][1] - bands[0][0]
    ink_top = bands[0][0] if bands else 0
    lefts = sorted(x for x in first_dark_x if x is not None)
    left = lefts[len(lefts) // 2] if lefts else 0

    size = int(round(ink / INK_RATIO)) if ink else max(8, int(round(pitch / 1.45)))
    return {"ok": True, "bg": bg, "fg": fg, "pitch": pitch, "ink": ink,
            "left": left, "top": ink_top, "size": size, "bands": len(bands),
            "tops": [b0 for b0, _b1 in bands]}


class MatchedView(QWidget):
    """不透明窗口：整块刷成量到的底色，里面那层文字按量到的排版排。"""

    def __init__(self, rect, style, parent=None):
        super(MatchedView, self).__init__(parent)
        self.style = dict(style)
        self.pad_left = int(style["left"])
        self.pad_top = int(style["top"])
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setGeometry(rect)
        self.edit = QTextEdit(self)
        self.edit.setFrameShape(QTextEdit.NoFrame)
        self.edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.edit.setGeometry(0, 0, rect.width(), rect.height())
        self.restyle()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), self.style["bg"])
        p.end()

    def restyle(self):
        st = self.style
        self.edit.setStyleSheet(
            "QTextEdit { background: transparent; border: none; padding: 0; margin: 0;"
            " color: %s; }" % st["fg"].name())
        f = QFont("Microsoft YaHei")
        f.setPixelSize(max(6, int(st["size"])))
        self.edit.setFont(f)
        doc = self.edit.document()
        doc.setDocumentMargin(0)
        try:      # 左/上留白用视口边距顶（文档边距是四边同值，会带偏）
            self.edit.setViewportMargins(self.pad_left, self.pad_top, 0, 0)
        except Exception:
            doc.setDocumentMargin(self.pad_left)
        # 一整段用 <br> 换行：分成多个 <p> 会带段落间距，行距就不等于 pitch 了
        cw = max(4.0, float(st["size"]))
        per_line = max(6, int((self.width() - self.pad_left - 2) / cw))
        rows = max(1, int((self.height() - self.pad_top) / float(st["pitch"])) + 1)
        line = (FILLER * (per_line // len(FILLER) + 2))[:per_line]
        self.edit.setHtml("<p>" + "<br>".join([line] * rows) + "</p>")
        bf = QTextBlockFormat()
        bf.setLineHeight(int(st["pitch"]), QTextBlockFormat.FixedHeight)
        bf.setAlignment(Qt.AlignLeft)
        bf.setTextIndent(0)
        cur = QTextCursor(doc)
        cur.select(QTextCursor.Document)
        cur.mergeBlockFormat(bf)
        self.update()

    def calibrate(self, src, rounds=3):
        """把覆盖层自己渲染出来再量一遍，按差值回头修左边距/行距/字号/顶上留白。

        这是这个 demo 唯一"能证明自己对得上"的地方：不是我说像，是量出来的数字对齐。
        """
        own = None
        for i in range(rounds):
            QApplication.processEvents()
            own = measure(self.grab().toImage())
            if not own.get("ok"):
                return own, i + 1
            d_left = src["left"] - own["left"]
            d_top = src["top"] - own["top"]
            d_pitch = src["pitch"] - own["pitch"]
            d_ink = src["ink"] - own["ink"]
            if max(abs(d_left), abs(d_top), abs(d_pitch), abs(d_ink)) <= 1:
                break
            self.pad_left += d_left
            self.pad_top += d_top
            if d_pitch:
                self.style["pitch"] += d_pitch
            if abs(d_ink) >= 2:
                self.style["size"] = max(6, int(round(self.style["size"] + d_ink)))
            self.restyle()
        return own, rounds


class FakeDoc(QWidget):
    """--self 用的"底下那份文档"：字号/行距/边距/颜色都是已知真值。"""

    def __init__(self, size, font_px=18, pitch=26, left=24, top=14,
                 bg="#fbfbf7", fg="#262626"):
        super(FakeDoc, self).__init__()
        self.resize(size)
        self.font_px = font_px
        self.pitch = pitch
        self.left = left
        self.top = top
        self.bg = QColor(bg)
        self.fg = QColor(fg)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), self.bg)
        f = QFont("Microsoft YaHei")
        f.setPixelSize(self.font_px)
        p.setFont(f)
        p.setPen(self.fg)
        text = FILLER * 20
        per = max(8, int((self.width() - self.left - 4) / float(self.font_px)))
        line = text[:per]
        y = self.top + self.font_px          # 基线：让第一行墨迹落在 top 附近
        while y < self.height() + self.pitch:
            p.drawText(self.left, y, line)
            y += self.pitch
        p.end()


def _diff(a, b):
    return max(abs(a.red() - b.red()), abs(a.green() - b.green()), abs(a.blue() - b.blue()))


def _phase_pad(base, rect, style, strip_px=60):
    """用窗口**上面那条还没被盖住**的带子，把行网格外推进窗口。

    这是"盖上去看不出来"的关键一步：风格照抄窗口正下方那块就行，但**相位**得靠窗口外面
    的行来定——否则窗口里第一条墨线可能比原文档差半行，接缝处一眼就能看出错位。
    做法：取一条不带窗口的上沿，量出各行墨顶，按行距往窗口里推，推出窗口内第一条墨线
    该落在哪，返回它相对窗口顶的偏移（窗口的 pad_top 就用它）。
    """
    y0 = max(0, rect.y() - strip_px)
    if rect.y() - y0 < 12:
        return 0
    m = measure(base.copy(0, y0, base.width(), rect.y() - y0))
    if not m.get("ok") or not m.get("tops"):
        return 0
    pitch = max(6, int(style["pitch"]))
    t = y0 + m["tops"][-1]
    while t < rect.y():
        t += pitch
    return t - rect.y()


def run_self(qapp):
    """零屏幕自检：造文档 → 量它 → 覆盖 → 再量覆盖层 → 回头修 → 报误差。

    **不 show 任何窗口**：`grab()` 对没显示的控件也能渲染，桌面上一个像素都不会出现。
    """
    if not QFontDatabase().families():
        print("这个 Qt 平台没有字体（离屏模式就会这样），字画不出来也就量不出来。\n"
              "  去掉 QT_QPA_PLATFORM=offscreen 再跑一次 --self。")
        return 2
    rect = QRect(0, 0, 760, 300)
    truth = dict(font_px=18, pitch=26, left=24, top=14, bg="#fbfbf7", fg="#262626")
    doc = FakeDoc(rect.size(), **truth)
    qapp.processEvents()
    out_dir = os.environ.get("TEMP") or "."
    doc.grab().save(os.path.join(out_dir, "overlay_src.png"))
    m1 = measure(doc.grab().toImage())
    if not m1.get("ok"):
        print("这块量不出来：%s" % m1.get("why"))
        return 2
    print("[原图] 底色 %s 字色 %s 行距 %d 墨高 %d 左边距 %d 墨顶 %d 字号≈%d"
          % (m1["bg"].name(), m1["fg"].name(), m1["pitch"], m1["ink"],
             m1["left"], m1["top"], m1["size"]))

    view = MatchedView(rect, m1)
    m2, rounds = view.calibrate(m1)
    qapp.processEvents()
    view.grab().save(os.path.join(out_dir, "overlay_dst.png"))
    if not m2.get("ok"):
        print("覆盖层量不出来：%s" % m2.get("why"))
        return 2
    print("[覆盖] 底色 %s 字色 %s 行距 %d 墨高 %d 左边距 %d 墨顶 %d 字号≈%d（来回修了 %d 次）"
          % (m2["bg"].name(), m2["fg"].name(), m2["pitch"], m2["ink"],
             m2["left"], m2["top"], m2["size"], rounds))

    print("\n[对真值算误差]")
    gates = [
        ("行距", m1["pitch"], truth["pitch"], 1),
        ("字号（估）", m1["size"], truth["font_px"], 2),
        ("左边距", m1["left"], truth["left"], 2),
        ("底色", m1["bg"], QColor(truth["bg"]), 2),
        ("字色", m1["fg"], QColor(truth["fg"]), 16),
    ]
    bad = 0
    for name, got, want, tol in gates:
        if isinstance(want, QColor):
            err = _diff(got, want)
            ok = err <= tol
            print("  %-10s 量到 %s / 真值 %s  色差 %d %s"
                  % (name, got.name(), want.name(), err, "ok" if ok else "FAIL"))
        else:
            err = abs(int(got) - int(want))
            ok = err <= tol
            print("  %-10s 量到 %s / 真值 %s  差 %d px %s"
                  % (name, got, want, err, "ok" if ok else "FAIL"))
        bad += 0 if ok else 1

    print("\n[覆盖层 vs 原图]（这才是\"看着像不像\"的硬指标）")
    for name, key, tol in (("行距", "pitch", 1), ("左边距", "left", 1),
                           ("墨顶", "top", 1), ("墨高", "ink", 2)):
        err = abs(m2[key] - m1[key])
        ok = err <= tol
        print("  %-6s 原图 %d / 覆盖层 %d  差 %d px %s"
              % (name, m1[key], m2[key], err, "ok" if ok else "FAIL"))
        bad += 0 if ok else 1
    for name, key in (("底色", "bg"), ("字色", "fg")):
        err = _diff(m2[key], m1[key])
        ok = err <= 2
        print("  %-6s 原图 %s / 覆盖层 %s  色差 %d %s"
              % (name, m1[key].name(), m2[key].name(), err, "ok" if ok else "FAIL"))
        bad += 0 if ok else 1

    # 接缝：把覆盖层贴回**原文档**下半截，而且起点故意**不是**行距整数倍——
    # 这样"接得上"就只能靠 `_phase_pad` 从窗口上沿外推出的相位，蒙不出来。
    big = FakeDoc(QRect(0, 0, 760, 520).size(), **truth)
    qapp.processEvents()
    base_img = big.grab().toImage()
    m3 = measure(base_img)
    y0 = 157
    rect2 = QRect(0, y0, 760, 300)
    view2 = MatchedView(rect2, m1)
    pad = _phase_pad(base_img, rect2, m1)
    view2.pad_top = pad
    view2.restyle()
    src2 = dict(m1)
    src2["top"] = pad                      # 让覆盖层第一条墨线刚好落在推出来的那一行
    view2.calibrate(src2)
    cover = view2.grab().toImage()
    comp = base_img.copy()
    p = QPainter(comp)
    p.drawImage(0, y0, cover)
    p.end()
    comp.save(os.path.join(out_dir, "overlay_seam.png"))
    m4 = measure(comp)
    print("\n[接缝] 覆盖层贴回原文档 y=%d（**不是**行距整数倍），相位靠窗口上沿外推=%d："
          % (y0, pad))
    if not (m3.get("ok") and m4.get("ok")):
        print("  量不出来：%s" % (m3.get("why") or m4.get("why")))
        return 3
    tail_a = [t for t in m3["tops"] if t > y0]
    tail_b = [t for t in m4["tops"] if t > y0]
    same = tail_a == tail_b
    print("  接缝以下本来有 %d 行，覆盖后还是 %d 行；行顶逐个比：%s"
          % (len(tail_a), len(tail_b), "一模一样" if same else "对不上"))
    if not same:
        print("  原：%s\n  覆盖：%s" % (tail_a[:8], tail_b[:8]))
    print("  （图：%s）" % os.path.join(out_dir, "overlay_seam.png"))
    bad += 0 if same else 1

    print("\n自检：%s" % ("全过" if not bad else "%d 项没过" % bad))
    return 0 if not bad else 1


def run_live(qapp, args):
    rect = None
    for i, a in enumerate(args):
        if a == "--rect" and i + 1 < len(args):
            x, y, w, h = [int(v) for v in args[i + 1].split(",")]
            rect = QRect(x, y, w, h)
    screen = QApplication.primaryScreen()
    if rect is None:
        g = screen.availableGeometry()
        rect = QRect(g.x() + g.width() // 8, g.y() + g.height() // 4,
                     int(g.width() * 0.6), min(280, int(g.height() * 0.4)))
    img = screen.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height()).toImage()
    m = measure(img)
    if not m.get("ok"):
        print("这块量不出来：%s" % m.get("why"))
        print("（压在图片/表格/空白上就会这样；换个有文字的位置再试）")
        return 2
    print("量到：底色 %s 字色 %s 行距 %d 墨高 %d 左边距 %d 墨顶 %d 字号≈%d"
          % (m["bg"].name(), m["fg"].name(), m["pitch"], m["ink"], m["left"],
             m["top"], m["size"]))
    if "--show" in args:
        return 0

    view = MatchedView(rect, m)
    view.show()
    view.raise_()
    qapp.processEvents()
    own, rounds = view.calibrate(m)
    if own and own.get("ok"):
        print("盖上后自量：行距 %d 左边距 %d 墨顶 %d（修了 %d 次）"
              % (own["pitch"], own["left"], own["top"], rounds))

    def again():
        view.hide()
        QApplication.processEvents()
        QTimer.singleShot(150, do_sample)

    def do_sample():
        im = QApplication.primaryScreen().grabWindow(
            0, rect.x(), rect.y(), rect.width(), rect.height()).toImage()
        mm = measure(im)
        if mm.get("ok"):
            print("重新量到：底色 %s 行距 %d 左边距 %d 字号≈%d"
                  % (mm["bg"].name(), mm["pitch"], mm["left"], mm["size"]))
            view.style = dict(mm)
            view.pad_left = int(mm["left"])
            view.pad_top = int(mm["top"])
            view.restyle()
            view.calibrate(mm)
        else:
            print("这次没量出来：%s" % mm.get("why"))
        view.show()
        view.raise_()

    view.keyPressEvent = lambda e: (again() if e.key() == Qt.Key_R else
                                    (qapp.quit() if e.key() == Qt.Key_Escape else None))
    print("窗口已盖上：R 重新采样、Esc 退出。")
    return 0


def main(argv):
    app = QApplication(argv)
    if "--self" in argv:
        return run_self(app)
    return run_live(app, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
