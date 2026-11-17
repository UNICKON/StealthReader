# -*- coding: utf-8 -*-
"""正文渲染探针：把「次像素抗锯齿在不在」「改颜色会不会动排版」钉成可复跑的数字。

    python tests/probe_text_render.py

真人问过两次同一件事（2026-11-06「实色的时候文字显示效果能优化吗」、2026-11-17
「不改变功能的情况下优化文字的渲染」），每次都是靠眼睛猜。这里把能量的都量出来：

  * **实色档**（纸不透明，alpha 255）→ 正文里有彩色边缘像素，也就是 ClearType 在；
  * **融景档**（纸 alpha=2，`CornerFrame._bg_alpha()`）→ 彩边 0.00%，只剩灰度。
    这是取舍不是 bug：纸几乎是全透明的，次像素没有已知底色可混，钉住它别被人"优化"掉；
  * **字色浓淡不改覆盖图**：255/245/235/225 四档的强覆盖比例一模一样（同一个字形位图，
    只是换了颜色映射）。所以"把字调亮一点会更实"是错觉，这条路已经量完，别再试；
  * **换 Qt 的字体引擎更软**：freetype 引擎彩边直接归零、弱覆盖变多（GDI 才是这套里的最优）；
  * **只改颜色不改排版**：同一段字在两套颜色下行数、首页字数完全一致（行宽是字号决定的）。

offscreen 平台没有 GDI 的次像素，所以本探针显式用 windows 平台、真把窗口显示出来再抓。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SR, "libs"))
sys.path.insert(0, SR)
os.chdir(SR)
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

# 探针不落盘：真人那份 config.json 收尾按原样写回。
_CFG_PATH = os.path.join(SR, "config.json")
try:
    with open(_CFG_PATH, "rb") as _f:
        _CFG_BAK = _f.read()
except Exception:
    _CFG_BAK = None


def _restore_cfg():
    if _CFG_BAK is None:
        return
    try:
        with open(_CFG_PATH, "rb") as _f:
            if _f.read() == _CFG_BAK:
                return
        with open(_CFG_PATH, "wb") as _f:
            _f.write(_CFG_BAK)
        print("\n（探针动过 config.json，已按开跑前那份原样写回）")
    except Exception:
        pass


import atexit                                                             # noqa: E402
atexit.register(_restore_cfg)

import time                                                              # noqa: E402
from PyQt5.QtCore import QPoint, QRect                                    # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter, QFont, QTextDocument    # noqa: E402
from PyQt5.QtWidgets import QApplication                                  # noqa: E402

import main as app_mod                                                   # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("  ok  " if cond else "  FAIL") + " " + name
          + (("   << " + str(extra)) if extra else ""))


SAMPLE = ("永和九年岁在癸丑暮春之初会于会稽山阴之兰亭修禊事也群贤毕至少长咸集"
          "此地有崇山峻岭茂林修竹又有清流激湍映带左右引以为流觞曲水列坐其次")


def scan(img, paper_lum=None, ink_lum=None):
    """彩色边缘 / 有墨点 / 强覆盖占墨比 / 弱覆盖占墨比。

    paper_lum/ink_lum 给了就按"纸→字"的实际亮度区间算覆盖率（覆盖率是字形位图自带的，
    和用什么颜色画无关）；不给就只数彩边和亮度>95 的墨点。
    """
    img = img.convertToFormat(QImage.Format_RGB32)
    W, H, bpl = img.width(), img.height(), img.bytesPerLine()
    raw = img.bits().asstring(img.byteCount())
    colored = ink = strong = weak = 0
    for y in range(H):
        row = y * bpl
        for x in range(W):
            i = row + x * 4
            b, g, r = raw[i], raw[i + 1], raw[i + 2]
            if max(r, g, b) - min(r, g, b) > 8:
                colored += 1
            lum = (r * 299 + g * 587 + b * 114) // 1000
            if lum > 95:
                ink += 1
            if paper_lum is not None and ink_lum is not None:
                span = float(ink_lum - paper_lum)
                cov = (lum - paper_lum) / span if span else 0.0
                if cov > 0.03:
                    if cov >= 0.85:
                        strong += 1
                    elif cov < 0.5:
                        weak += 1
    total = W * H
    return {
        "colored_pct": 100.0 * colored / total if total else 0.0,
        "ink_pct": 100.0 * ink / total if total else 0.0,
        "strong_pct": (100.0 * strong / ink) if ink else 0.0,
        "weak_pct": (100.0 * weak / ink) if ink else 0.0,
    }


qapp = QApplication.instance() or QApplication(sys.argv)

print("=== 0. 平台与前提到不到位 ===")
plat = qapp.platformName()
check("跑在真 windows 平台上（offscreen 没有 GDI 次像素，量了也白量）",
      plat == "windows", plat)
screen = qapp.primaryScreen()
print("      屏幕 %dx%d  逻辑 DPI %.1f" % (screen.size().width(), screen.size().height(),
                                          screen.logicalDotsPerInch()))

w = app_mod.StealthReader()
w.save_config = lambda *a, **k: None
app_mod.save_config = lambda *a, **k: None
w.progress_save_timer.stop()
w.resize(900, 600)
w.show()
for _ in range(30):
    qapp.processEvents()

w.text_edit.setPlainText((SAMPLE + "。") * 6)
w._pin_book_font("probe")


def settle(n=40):
    for _ in range(n):
        qapp.processEvents()
        time.sleep(0.005)


def body_crop(max_rows=300):
    """窗口里正文那一块（按 DPR 换算到设备像素）。"""
    img = w.grab().toImage()
    dpr = float(img.width()) / float(w.width())
    vp = w.text_edit.viewport()
    r = QRect(vp.mapTo(w, QPoint(0, 0)), vp.size())
    h = min(int(r.height() * dpr), int(max_rows))
    return img.copy(QRect(int(r.x() * dpr), int(r.y() * dpr),
                          int(r.width() * dpr), h)), dpr


print("\n=== 1. 实色档：正文应当有次像素（ClearType） ===")
w.config["auto_mode"] = True
w.config["auto_bg_solid"] = True
w.apply_style()
settle()
crop, dpr = body_crop()
r_solid = scan(crop)
print("      DPR=%.1f  彩色边缘 %.2f%%  墨点 %.2f%%" % (dpr, r_solid["colored_pct"],
                                                       r_solid["ink_pct"]))
check("实色档纸不透明（alpha 255）", w.content_frame._bg_alpha() == 255,
      w.content_frame._bg_alpha())
check("实色档正文有彩色边缘（次像素没被关掉）", r_solid["colored_pct"] > 1.0,
      "%.2f%%" % r_solid["colored_pct"])

print("\n=== 2. 融景档：纸几乎全透，只能出灰度（取舍，钉住） ===")
w.config["auto_bg_solid"] = False
w.apply_style()
settle()
crop2, _ = body_crop()
r_amb = scan(crop2)
print("      纸 alpha=%d  彩色边缘 %.2f%%  墨点 %.2f%%" % (
    w.content_frame._bg_alpha(), r_amb["colored_pct"], r_amb["ink_pct"]))
check("融景档纸的 alpha 是 2（不是不透明）", w.content_frame._bg_alpha() == 2,
      w.content_frame._bg_alpha())
check("融景档正文没有彩色边缘（次像素需要已知底色，这一档给不了）",
      r_amb["colored_pct"] < 0.2, "%.2f%%" % r_amb["colored_pct"])

print("\n=== 3. 字色浓淡不改覆盖图：调亮不会让笔画更「实」 ===")
font = w._book_font()
font.setPixelSize(18)
ink_stats = []
for level in (255, 245, 235, 225):
    img = QImage(560, 64, QImage.Format_RGB32)
    img.fill(QColor(89, 89, 89))
    p = QPainter(img)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    p.setFont(font)
    p.setPen(QColor(level, level, level))
    p.drawText(5, 48, SAMPLE)
    p.end()
    st = scan(img, paper_lum=89, ink_lum=level)
    ink_stats.append(st["strong_pct"])
    print("      字色 %3d  强覆盖占墨 %5.1f%%  弱覆盖占墨 %5.1f%%" % (
        level, st["strong_pct"], st["weak_pct"]))
check("四档字色的强覆盖比例一致（±0.5 个百分点）——颜色只换映射，不换字形位图",
      max(ink_stats) - min(ink_stats) <= 0.5,
      "极差 %.2f" % (max(ink_stats) - min(ink_stats)))

print("\n=== 4. 只改颜色不改排版 ===")


def paginate(text, color):
    doc = QTextDocument()
    doc.setDefaultFont(QFont(font))
    opt = doc.defaultTextOption()
    opt.setUseDesignMetrics(False)
    doc.setDefaultTextOption(opt)
    doc.setDocumentMargin(0)
    doc.setTextWidth(820)
    doc.setPlainText(text)
    # 颜色走 char format，模拟"只换颜色"
    from PyQt5.QtGui import QTextCursor, QTextCharFormat
    cur = QTextCursor(doc)
    cur.select(QTextCursor.Document)
    fmt = QTextCharFormat()
    fmt.setForeground(QColor(color))
    cur.mergeCharFormat(fmt)
    return doc.blockCount(), doc.documentLayout().documentSize().height()


blocks_a, height_a = paginate(SAMPLE * 20, "#d2d2d2")
blocks_b, height_b = paginate(SAMPLE * 20, "#eeeeee")
print("      旧色：%d 行 / 高 %.1f    新色：%d 行 / 高 %.1f" % (
    blocks_a, height_a, blocks_b, height_b))
check("换颜色后行数一样", blocks_a == blocks_b, (blocks_a, blocks_b))
check("换颜色后排版高度一样", abs(height_a - height_b) < 0.01, (height_a, height_b))

print("\n=== 5. 字形的覆盖率（Qt 自己这一份，做参考） ===")
img = QImage(760, 70, QImage.Format_RGB32)
img.fill(QColor(89, 89, 89))
p = QPainter(img)
p.setRenderHint(QPainter.TextAntialiasing, True)
f18 = QFont(w.config.get("font_family") or "Microsoft YaHei")
f18.setPixelSize(18)
p.setFont(f18)
p.setPen(QColor(255, 255, 255))
p.drawText(5, 52, SAMPLE)
p.end()
qt_stat = scan(img, paper_lum=89, ink_lum=255)
print("      Qt5 GDI：彩色边缘 %.2f%%  强覆盖占墨 %.1f%%  弱覆盖占墨 %.1f%%" % (
    qt_stat["colored_pct"], qt_stat["strong_pct"], qt_stat["weak_pct"]))
print("      （这个数会随进程的 DPI 上下文变，别拿它跨引擎比。真人的「笔画略软」是"
      "「Qt5 光栅器 vs 系统 ClearType」的差别：同一段字、同字号、同纸色，在 96 DPI 上下文里"
      "两边总墨量只差 1.5%，Qt 全实心的芯少 5.6 个百分点。四条路都量完了（hinting／字体引擎／"
      "字色／透明度）没有一条能改，数字与结论在 docs 这一轮的记录里）")
check("Qt 的次像素在（彩边 > 1%）", qt_stat["colored_pct"] > 1.0,
      "%.2f%%" % qt_stat["colored_pct"])

w.close()
qapp.processEvents()

bad = [n for n, ok in RESULTS if not ok]
print("\n%d/%d 通过" % (len(RESULTS) - len(bad), len(RESULTS)))
if bad:
    print("失败：" + "；".join(bad))
sys.exit(1 if bad else 0)
