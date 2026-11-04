#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""覆盖式伪装 demo：**不透明**窗口盖住一块区域，按底下那块区域的排版和底色来写字。

真人 2026-11-04 的思路：窗口叠在文字上，窗口就显示成对应的排版、字体。
这个 demo 只做"看得见的那一半"——量出底下的行距/墨高/字色/底色/左边距/首行墨顶，
盖上、用同样的排版填字，然后**把覆盖层自己再量一遍**、按差值回头修，直到对得上。
不接社区流，填的是占位文字。

用法：
    python overlay_demo.py --rt        # 实时：窗口一直盖着，每 0.6 秒跟一次底下的排版
    python overlay_demo.py --play      # 看得见：假文档窗口 + 覆盖层盖住它下半截（不碰真屏幕）
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


def run_play(app):
    """看得见的那种 demo：开一个"假文档"窗口，再用覆盖层盖住它的下半截。

    全程不碰真实屏幕（图是从我们自己的窗口 grab 出来的），所以随便看、随便盖。
    Esc 关掉，两分钟自己退。
    """
    truth = dict(font_px=18, pitch=26, left=24, top=14, bg="#fbfbf7", fg="#262626")
    doc = FakeDoc(QRect(0, 0, 760, 520).size(), **truth)
    g = QApplication.primaryScreen().availableGeometry()
    doc.move(g.x() + 90, g.y() + 70)
    doc.setWindowTitle("下面这份是假文档（真值 18px / 行距 26px）")
    doc.show()
    doc.raise_()
    app.processEvents()
    state = {}

    def cover():
        img = doc.grab().toImage()
        y0 = 157                                   # 故意选成不是行距整数倍
        rect = QRect(0, y0, 760, 300)
        st = measure(img.copy(0, y0, rect.width(), rect.height()))
        if not st.get("ok"):
            print("量不出来：%s" % st.get("why"))
            app.quit()
            return
        view = MatchedView(rect, st)
        pad = _phase_pad(img, rect, st)
        view.pad_top = pad
        view.restyle()
        src2 = dict(st)
        src2["top"] = pad
        own, _rounds = view.calibrate(src2)
        gp = doc.mapToGlobal(doc.rect().topLeft())
        view.move(gp.x(), gp.y() + y0)
        view.setFocusPolicy(Qt.StrongFocus)
        view.show()
        view.raise_()
        view.activateWindow()
        view.setFocus()

        def on_key(e):
            if e.key() == Qt.Key_Escape:
                app.quit()
            elif e.key() == Qt.Key_R:
                view.hide()
                app.processEvents()
                QTimer.singleShot(200, cover)

        view.keyPressEvent = on_key
        state["view"] = view
        print("覆盖层已盖上：行距 %d、左边距 %d、相位外推 %d；覆盖层自量 行距=%s 墨顶=%s"
              % (st["pitch"], st["left"], pad,
                 own.get("pitch") if own else "?", own.get("top") if own else "?"))
        print("（下半截是我们的字，上半截是原文档；接缝处应该接得上。Esc 关掉，R 重来）")

    QTimer.singleShot(900, cover)
    QTimer.singleShot(180000, app.quit)
    return app.exec_()


def ring_measure(rect, pad=80):
    """**只量窗口上面那一圈**：实时刷新不能每次都把窗口藏一下（会闪）。

    窗口上沿外那条带子给出底色、字色、行距、墨高、左边距，再按行距把相位推进窗口里。
    带子不经过窗口，所以窗口盖着也不影响——"实时"能不开闪就是靠这个。
    带子里要不是"一段文字"（行距离谱、行数太少），这里直接判死，不拿垃圾去排版。
    """
    screen = QApplication.primaryScreen()
    if rect.y() - pad < 12:
        pad = max(20, rect.y() - 8)
    if pad < 24 or rect.y() - pad < 0:
        return None
    img = screen.grabWindow(0, rect.x(), rect.y() - pad, rect.width(), pad).toImage()
    m = measure(img)
    if not m.get("ok") or not m.get("tops"):
        return None
    if not (12 <= m["pitch"] <= 60) or m["ink"] < 6 or m["bands"] < 3:
        return None
    if m["left"] > rect.width() * 0.35:
        # 真桌面实跑时量到过 left=1430（窗口才 1190 宽）：那是"文字在窗口右半边之外"，
        # 照这个左边距排，字全排到窗口外面去，看着就是一块空白。这种直接判死。
        return None
    pitch = max(6, int(m["pitch"]))
    t = m["tops"][-1]
    while t < pad:
        t += pitch
    m["phase"] = t - pad            # 窗口内第一条墨线该落在窗口顶以下多少像素
    return m


def find_text_rect(size=(760, 240), step=110):
    """在屏幕上找一块"确实是文字"的地方（量它上沿那条带子，行距/墨高/行数都要像话）。

    扫描时先把图缩到 1/3 再量（逐像素量太慢，全屏要几分钟），量到的行距/墨高再乘回去；
    只是挑位置，挑中之后 `ring_measure` 仍然是全分辨率量。找不到就返回 None——不猜、不硬盖。
    """
    screen = QApplication.primaryScreen()
    g = screen.availableGeometry()
    w, h = size
    best = None
    for y in range(g.top() + 120, max(g.top() + 121, g.bottom() - h - 60), step):
        for x in range(g.left() + 80, max(g.left() + 81, g.right() - w - 80), step * 2):
            raw = screen.grabWindow(0, x, y - 80, w, 80).toImage()
            small = raw.scaled(max(9, w // 3), max(9, 80 // 3),
                               Qt.IgnoreAspectRatio, Qt.FastTransformation)
            m = measure(small)
            if not m.get("ok") or not m.get("pitch") or m["bands"] < 3:
                continue
            pitch = m["pitch"] * 3
            ink = m["ink"] * 3
            left = m["left"] * 3
            if not (12 <= pitch <= 60) or ink < 6:
                continue
            if left > w // 3:          # 文字挤在最右边那种"窄列"不要，盖上去很难看
                continue
            score = (m["bands"], ink)
            if best is None or score > best[0]:
                best = (score, QRect(x, y, w, h))
    return None if best is None else best[1]


def _median(hist, key):
    vals = sorted(int(h[key]) for h in hist)
    return vals[len(vals) // 2]


def median_style(hist):
    """连着量几次取中位数：光标闪、滚动动画、抗锯齿的抖动都不该让窗口跟着抖。"""
    st = dict(hist[-1])
    for key in ("pitch", "left", "phase", "ink"):
        st[key] = _median(hist, key)
    if st.get("ink"):
        st["size"] = max(6, int(round(st["ink"] / INK_RATIO)))
    for key in ("bg", "fg"):
        names = [h[key].name() for h in hist]
        st[key] = QColor(max(set(names), key=names.count))
    return st


def style_changed(a, b):
    if a is None:
        return True
    for key in ("pitch", "left", "phase"):
        if abs(int(a.get(key, 0)) - int(b.get(key, 0))) > 1:
            return True
    for key in ("bg", "fg"):
        if _diff(a[key], b[key]) > 2:
            return True
    return False


def run_rt(app, args):
    """实时版：窗口一直盖着，每 0.6 秒从**窗口上面那条带子**重量一遍，变了就跟。

    压在别的应用上时，它换页、换主题、甚至底下换了个应用，窗口都跟着变；
    全程不用把窗口藏起来取图，所以不闪。
    刚起来时它不知道下面是什么，先按"白底黑字 26px"占位——**拖着它走到有字的地方**，
    下一个 0.6 秒就跟上了。
    """
    rect = None
    for i, a in enumerate(args):
        if a == "--rect" and i + 1 < len(args):
            x, y, w, h = [int(v) for v in args[i + 1].split(",")]
            rect = QRect(x, y, w, h)
    if rect is None and "--auto" in args:
        rect = find_text_rect()
        if rect is None:
            print("屏幕上没找到一块像文字的地方（图/表格/空白），改用默认位置。")
    if rect is None:
        g = QApplication.primaryScreen().availableGeometry()
        rect = QRect(g.x() + g.width() // 6, g.y() + g.height() // 4,
                     int(g.width() * 0.62), 240)
    hist = []
    box = {}
    fallback = {"bg": QColor("#ffffff"), "fg": QColor("#262626"), "pitch": 26,
                "left": 60, "phase": 8, "ink": 18, "size": 18, "top": 8}

    def make_view(st):
        view = MatchedView(rect, st)
        view.pad_left = int(st["left"])
        view.pad_top = int(st["phase"])
        view.restyle()
        view.setFocusPolicy(Qt.StrongFocus)
        view.show()
        view.raise_()
        view.activateWindow()
        view.setFocus()

        def on_key(e):
            if e.key() == Qt.Key_Escape:
                app.quit()
            elif e.key() == Qt.Key_R:
                st2 = median_style(hist) if hist else fallback
                view.style = dict(st2)
                view.pad_left = int(st2["left"])
                view.pad_top = int(st2["phase"])
                view.restyle()

        view.keyPressEvent = on_key

        # 拖着走：松手之后下一个 0.6 秒的 tick 就按新位置重量、重新排版
        drag = {}

        def on_press(e):
            if e.button() == Qt.LeftButton:
                drag["off"] = e.globalPos() - rect.topLeft()

        def on_move(e):
            off = drag.get("off")
            if off is not None:
                rect.moveTo(e.globalPos() - off)
                view.setGeometry(rect)
                box["moved"] = True

        def on_release(_e):
            drag.pop("off", None)

        view.mousePressEvent = on_press
        view.mouseMoveEvent = on_move
        view.mouseReleaseEvent = on_release
        return view

    def tick():
        m = ring_measure(rect)
        view = box.get("view")
        if m is None:
            if view is None:
                box["view"] = make_view(fallback)
                print("这块上面没量到字，先按默认样式占位；拖着它走到有文字的地方就跟着变。")
            return
        hist.append(m)
        while len(hist) > 3:
            hist.pop(0)
        st = median_style(hist)
        if view is None:
            box["view"] = make_view(st)
            print("跟上：行距 %d 左边距 %d 相位 %d 墨高 %d 字号≈%d 底 %s 字 %s"
                  % (st["pitch"], st["left"], st["phase"], st["ink"], st["size"],
                     st["bg"].name(), st["fg"].name()))
        elif style_changed(view.style, st):
            view.style = dict(st)
            view.pad_left = int(st["left"])
            view.pad_top = int(st["phase"])
            view.restyle()
            print("跟到变化：行距 %d 左边距 %d 相位 %d 底 %s 字 %s"
                  % (st["pitch"], st["left"], st["phase"],
                     st["bg"].name(), st["fg"].name()))
        elif abs(view.pad_top - int(st["phase"])) > 1 and box.get("moved"):
            # 相位**不每帧都改**：实测它会在 4~22 之间跳（带子下沿常把最后一行切半行），
            # 跟着改就成了"第一条墨线上下抖"。只在窗口被拖动过之后才对一次。
            view.pad_top = int(st["phase"])
            view.restyle()
            box["moved"] = False

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(600)
    QTimer.singleShot(300, tick)
    QTimer.singleShot(600000, app.quit)      # 十分钟自退
    print("实时模式：拖着窗口走到有文字的地方，每 0.6 秒跟着变；Esc 退出。")
    return app.exec_()


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
    if "--play" in argv:
        return run_play(app)
    if "--rt" in argv:
        return run_rt(app, argv)
    return run_live(app, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
